"""Owner-only, Python 3.6+ Server deployment. Embedded in Server and Client.

SSH runs a single-line Python command; the verified release arrives on stdin.
No login credentials or CAM tokens belong in release files or diagnostics.
"""
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError('unsafe_directory')
    return path


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('unsafe_config')
        return json.load(stream)


def write_json(path, data):
    private_dir(os.path.dirname(path))
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def version(binary):
    try:
        value = subprocess.check_output([binary, 'version'], stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        match = re.fullmatch(r'cam-server (\d+)\.(\d+)\.(\d+)', value)
        return tuple(int(n) for n in match.groups()) if match else (0, 0, 0)
    except (OSError, subprocess.SubprocessError):
        return (0, 0, 0)


def status(binary, path):
    try:
        return json.loads(subprocess.check_output([binary, 'status', '--socket', path],
                         stderr=subprocess.DEVNULL, timeout=3).decode())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_identity(pid, config):
    """Match owner, exact config and process start time before signalling."""
    try:
        root = '/proc/%d' % pid
        if os.stat(root).st_uid != os.getuid():
            return None
        with open(root + '/cmdline', 'rb') as stream:
            args = [os.fsdecode(s) for s in stream.read().split(b'\0') if s]
        if 'serve' not in args or '--config' not in args or args[args.index('--config') + 1] != config:
            return None
        executable = os.readlink(root + '/exe')
        # An installed executable may be a script (test fixture/custom install).
        if not any(os.path.basename(a) in ('cam-server', 'router_test_node') for a in args[:2]):
            return None
        with open(root + '/stat') as stream:
            state = stream.read().rsplit(')', 1)[1].split()
        if state[0] == 'Z':
            return None
        return (executable, state[19])
    except (OSError, IndexError, ValueError):
        return None


def managed_process(config):
    found = [(int(p), process_identity(int(p), config)) for p in os.listdir('/proc') if p.isdigit()]
    found = [(p, identity) for p, identity in found if identity]
    if len(found) > 1:
        raise ValueError('multiple_servers')
    return found[0] if found else (None, None)


def stop(pid, identity, config):
    if process_identity(pid, config) != identity:
        raise ValueError('server_identity_changed')
    os.kill(pid, signal.SIGTERM)
    for unused in range(80):
        if process_identity(pid, config) != identity:
            return
        time.sleep(.05)
    raise ValueError('server_stop_timeout')


def link(target, path):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = path + '.new-' + str(os.getpid())
    os.symlink(target, temporary)
    try:
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def install(req, root):
    size = req['size']
    if not isinstance(size, int) or not 0 < size <= 32 * 1024 * 1024:
        raise ValueError('invalid_bundle')
    data = sys.stdin.buffer.read(size + 1) if not req.get('bundle') else open(req['bundle'], 'rb').read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != req['sha256']:
        raise ValueError('bundle_checksum')
    releases = private_dir(os.path.join(root, 'releases', req['target']))
    release = os.path.join(releases, 'v%s-%s' % (req['version'], req['sha256'][:12]))
    staging = tempfile.mkdtemp(prefix='.upload-', dir=releases)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
            members = archive.getmembers()
            if len(members) > 2048 or sum(m.size for m in members) > 64 * 1024 * 1024:
                raise ValueError('invalid_bundle')
            names = set()
            for item in members:
                parts = item.name.split('/')
                if item.name in names or item.name.startswith('/') or '..' in parts or not item.isfile():
                    raise ValueError('invalid_bundle')
                names.add(item.name)
                if item.name != 'cam-server' and not item.name.startswith('sdk/'):
                    raise ValueError('invalid_bundle')
                destination = os.path.join(staging, item.name)
                os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
                with open(destination, 'xb') as output:
                    shutil.copyfileobj(archive.extractfile(item), output)
                os.chmod(destination, 0o700 if item.name == 'cam-server' else 0o600)
        if version(os.path.join(staging, 'cam-server')) != tuple(map(int, req['version'].split('.'))):
            raise ValueError('bundle_version')
        sdk = os.path.join(staging, 'sdk')
        subprocess.check_call([sys.executable, '-c', 'import sys;sys.path.insert(0,sys.argv[1]);import camc_pkg.server_worker', sdk],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if os.path.exists(release):
            # Never trust a half-written release left by another uploader.
            for base, dirs, files in os.walk(staging):
                for name in files:
                    relative = os.path.relpath(os.path.join(base, name), staging)
                    existing = os.path.join(release, relative)
                    if os.path.islink(existing) or not os.path.isfile(existing) or open(existing, 'rb').read() != open(os.path.join(base, name), 'rb').read():
                        raise ValueError('release_conflict')
        else:
            os.rename(staging, release)
        for name, payload in [('cam-server.bundle', data), ('cam-server.bundle.json',
                              json.dumps({k: req[k] for k in ('version', 'target', 'size', 'sha256')}).encode())]:
            path = os.path.join(release, name)
            if not os.path.exists(path):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
        return release
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def run(req):
    if sys.version_info < (3, 6) or platform.system() != 'Linux':
        raise ValueError('requires_linux_python36')
    host = socket.gethostname()
    home = os.path.expanduser('~')
    root = private_dir(os.path.join(home, '.cam', 'server'))
    lock_path = os.path.join(root, 'setup-' + host + '.lock')
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'r+') as guard:
        if os.fstat(guard.fileno()).st_uid != os.getuid() or os.fstat(guard.fileno()).st_mode & 0o077:
            raise ValueError('unsafe_lock')
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('setup_busy')
        return ensure(req, root, home, host)


def ensure(req, root, home, host):
    config = req.get('config') or os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(home, '.config'), 'cam-server', host + '.json')
    service_path = os.path.join(root, 'service-' + host + '.json')
    if not req.get('config') and os.path.exists(service_path):
        config = read_json(service_path)['config']
    config = os.path.abspath(config)
    cfg = read_json(config) if os.path.lexists(config) else None
    if req.get('expected_node') and (not cfg or cfg['node_id'] != req['expected_node']):
        raise ValueError('node_identity_mismatch')
    entry = os.path.join(home, '.local', 'bin', 'cam-server')
    binary = entry if os.access(entry, os.X_OK) else shutil.which('cam-server')
    current = version(binary) if binary else (0, 0, 0)
    minimum = tuple(map(int, req['version'].split('.')))
    runtime = (cfg.get('runtime_dir') or '/tmp/cam-server-%d-%s/runtime' %
               (os.getuid(), hashlib.sha256(cfg['node_id'].encode()).hexdigest()[:24])) if cfg else ''
    sock = os.path.join(runtime, 'server.sock')
    ready = status(binary, sock) if binary and cfg else None
    if ready and ready.get('node_id') != cfg['node_id']:
        raise ValueError('node_identity_mismatch')
    backend = cfg.get('backend') if cfg else None
    sdk_ready = backend and os.path.isfile(os.path.join(backend['module_path'], 'camc_pkg', 'server_worker.py'))
    pid, identity = managed_process(config) if cfg else (None, None)
    running_version = tuple(map(int, ready['server_version'].split('.'))) if ready and ready.get('server_version') else (version('/proc/%d/exe' % pid) if pid else ((0, 0, 0) if ready else current))
    need = current < minimum or (ready and running_version < minimum) or not sdk_ready or bool(ready and not ready.get('backend'))
    target = platform.machine() + '-unknown-linux-musl'
    if req['mode'] == 'probe' and need:
        return dict(need_bundle=True, target=target)
    if need and target != req.get('target'):
        raise ValueError('unsupported_architecture')
    release = install(req, root) if need else None
    selected = os.path.join(release, 'cam-server') if current < minimum else binary
    if not selected:
        raise ValueError('missing_binary')
    original = dict(cfg) if cfg else None
    if not cfg:
        subprocess.check_call([selected, 'init', '--config', config], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        cfg = read_json(config)
        runtime = '/tmp/cam-server-%d-%s/runtime' % (os.getuid(), hashlib.sha256(cfg['node_id'].encode()).hexdigest()[:24])
        sock = os.path.join(runtime, 'server.sock')
    changed_backend = release is not None
    if changed_backend:
        cfg['backend'] = dict(python=sys.executable, module_path=os.path.join(release, 'sdk'))
    # An old running process must also be restarted after a shared-home binary
    # update. Query the actual executable, not only today's symlink target.
    restart = bool(ready and (running_version < minimum or changed_backend))
    old_binary = binary
    if restart:
        if not pid:
            raise ValueError('unmanaged_server_restart')
        if original:
            write_json(config + '.before-update', original)
        # Preserve the executable inode before replacing a regular-file entry
        # or stopping a process whose binary has already been unlinked.
        backup = private_dir(os.path.join(root, 'previous-' + host))
        old_binary = os.path.join(backup, 'cam-server')
        fd, temporary = tempfile.mkstemp(dir=backup)
        os.close(fd)
        try:
            shutil.copyfile('/proc/%d/exe' % pid, temporary)
            os.chmod(temporary, 0o700)
            os.replace(temporary, old_binary)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    started = None
    stopped = False
    current_link = os.path.join(root, 'current-' + host)
    previous_link = os.readlink(current_link) if os.path.islink(current_link) else None
    published = False
    try:
        if restart:
            stop(pid, identity, config)
            stopped = True
            ready = None
        if cfg != original:
            write_json(config, cfg)
        if not ready:
            logs = private_dir(os.path.join(root, 'logs'))
            fd = os.open(os.path.join(logs, host + '.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab', buffering=0) as log:
                if os.fstat(log.fileno()).st_uid != os.getuid() or os.fstat(log.fileno()).st_mode & 0o077:
                    raise ValueError('unsafe_log')
                started = subprocess.Popen([selected, 'serve', '--config', config], stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, start_new_session=True)
            for unused in range(80):
                ready = status(selected, sock)
                if ready:
                    break
                if started.poll() is not None:
                    raise ValueError('server_start_failed')
                time.sleep(.1)
            if not ready or not ready.get('backend') or ready.get('node_id') != cfg['node_id']:
                raise ValueError('server_not_ready')
            write_json(service_path, dict(pid=started.pid, node_id=cfg['node_id'], config=config, socket=sock,
                                         release_dir=release or os.path.dirname(selected)))
        info = None
        if req.get('client_id'):
            info = json.loads(subprocess.check_output([selected, 'pair', '--config', config, '--client-id', req['client_id']],
                                                     stderr=subprocess.DEVNULL, timeout=8).decode())
        if release:
            # Publish the new entry only after setup and pairing succeed.
            if os.path.lexists(current_link) and not os.path.islink(current_link):
                raise ValueError('unsafe_release_link')
            link(release, current_link)
            published = True
            if current < minimum:
                link(selected, entry)
        if info:
            info['deployment'] = 'updated' if release else ('started' if started else 'current')
            return info
        return dict(ok=True, node_id=cfg['node_id'], version='.'.join(map(str, version(selected))),
                    deployment='updated' if release else ('started' if started else 'current'))
    except Exception:
        if published:
            if previous_link:
                link(previous_link, current_link)
            else:
                os.unlink(current_link)
        if started and started.poll() is None:
            started.terminate()
            started.wait(timeout=5)
        if original and (restart or cfg != original):
            write_json(config, original)
        if stopped and old_binary and os.path.isfile(old_binary):
            with open(os.devnull, 'wb') as log:
                recovered = subprocess.Popen([old_binary, 'serve', '--config', config], stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=log, start_new_session=True)
            write_json(service_path, dict(pid=recovered.pid, node_id=original['node_id'], config=config,
                                         socket=sock, release_dir=os.path.dirname(old_binary)))
        raise


def main():
    try:
        req = json.loads(sys.argv[1])
        if req.get('bundle'):
            with open(req['bundle'] + '.json') as stream:
                manifest = json.load(stream)
            req.update({k: manifest[k] for k in ('version', 'target', 'size', 'sha256')})
            req['mode'] = 'install'
        if not re.fullmatch(r'\d+\.\d+\.\d+', req['version']):
            raise ValueError('invalid_version')
        if req['mode'] not in ('probe', 'install'):
            raise ValueError('invalid_mode')
        print(json.dumps(run(req)))
    except Exception as exc:
        # Only controlled error codes cross the UI boundary. Exception text,
        # subprocess stdout and configuration may contain private material.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z_]+', str(exc)) else 'setup_failed'
        print(json.dumps(dict(setup_error=code)))
        sys.exit(2)


if __name__ == '__main__':
    main()
