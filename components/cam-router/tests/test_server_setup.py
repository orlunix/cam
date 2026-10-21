"""Real SSH deployment, version fast path and guarded Server-only upgrades."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import unittest
import uuid

import paramiko
import test_router_e2e as routing
import test_tui_e2e as baseline
from test_tui_ssh_nodes import SSHService

SCRIPTS = routing.ROOT / 'components/cam-router/scripts'


class ServerSetupTests(unittest.TestCase):
    spawn = baseline.TuiTests.spawn
    until = baseline.TuiTests.until
    node = baseline.TuiTests.node
    rpc = baseline.TuiTests.rpc
    echo = baseline.TuiTests.echo

    def setUp(self):
        baseline.TuiTests.setUp(self)
        self.setup_cleanups = []

    def tearDown(self):
        try:
            for cleanup in reversed(self.setup_cleanups):
                cleanup()
        finally:
            baseline.TuiTests.tearDown(self)
    def prepare(self, state, shell='/bin/sh'):
        node = self.node('a', 'b', native=True)
        config_dir = node.home / '.config/cam-server'
        config_dir.mkdir(mode=0o700, parents=True)
        node.env['XDG_CONFIG_HOME'] = str(node.home / '.config')
        node.config_path = config_dir / (socket.gethostname() + '.json')
        routing.private_json(node.config_path, node.config)
        if state == 'missing':
            node.config_path.unlink()
        local = self.base / 'bins'
        local.mkdir()
        shutil.copy2(baseline.DAEMON, local / 'cam-clientd')
        # Isolated test Node has the production protocol, without competing
        # for this user's one real machine-wide production Server lock.
        wrapper = local / 'cam-server'
        wrapper.write_text('#!/bin/sh\nif [ "$1" = serve ]; then exec %s "$@"; fi\nexec %s "$@"\n' %
                           (shlex.quote(str(routing.TEST_NODE)), shlex.quote(str(routing.SERVER))))
        wrapper.chmod(0o700)
        subprocess.check_call([sys.executable, str(SCRIPTS / 'package-server.py'), str(wrapper),
                               str(local / 'cam-server.bundle'), platform.machine() + '-unknown-linux-musl'])
        remote = node.home / '.local/bin'
        remote.mkdir(parents=True)
        if state != 'missing':
            shutil.copy2(wrapper, remote / 'cam-server')
            if state in ('old', 'newer'):
                text = (remote / 'cam-server').read_text()
                text = text.replace('if [ "$1" = serve ]',
                    'if [ "$1" = version ]; then echo "cam-server %s"; exit; fi\nif [ "$1" = serve ]' %
                    ('0.1.1' if state == 'old' else '9.0.0'))
                (remote / 'cam-server').write_text(text)
            if state != 'stopped':
                node.start()
        # Prevent a missing-binary test accidentally finding a real Server
        # elsewhere on the host's PATH.
        node.env['PATH'] = '/usr/bin:/bin'
        key = paramiko.RSAKey.generate(2048)
        key_path = self.base / 'key'
        key.write_private_key_file(str(key_path))
        key_path.chmod(0o600)
        service = SSHService(node, 'fixture', key=key, shell=shell)
        self.addCleanup(service.close)
        known = self.base / 'known-hosts'
        known.write_text('[127.0.0.1]:%d %s %s\n' %
                         (service.port, service.hostkey.get_name(), service.hostkey.get_base64()))
        ssh_config = self.base / 'ssh-config'
        ssh_config.write_text('Host *\n  UserKnownHostsFile %s\n  StrictHostKeyChecking yes\n' % known)
        (local / 'ssh').write_text('#!/bin/sh\nexec /usr/bin/ssh -F %s "$@"\n' % shlex.quote(str(ssh_config)))
        (local / 'ssh').chmod(0o700)
        self.env['PATH'] = str(local) + ':' + self.env['PATH']
        routing.private_json(self.client_config, dict(client_id='setup-' + uuid.uuid4().hex,
            runtime_dir=str(self.runtime), endpoints=[], poll_seconds=1))
        self.daemon = self.spawn([str(local / 'cam-clientd'), 'serve', '--config', str(self.client_config)], self.env)
        self.until(self.client_socket.exists)
        connection = dict(host='127.0.0.1', port=service.port, user='fixture', key=str(key_path))
        # Servers launched by the deployment helper are intentionally detached;
        # explicitly collect those fixture-only processes, even on assertion failure.
        def cleanup():
            path = node.home / '.cam/server' / ('service-' + socket.gethostname() + '.json')
            if path.exists():
                metadata = json.loads(path.read_text())
                cfg = json.loads(Path(metadata['config']).read_text())
                runtime = cfg.get('runtime_dir') or '/tmp/cam-server-%d-%s/runtime' % (
                    os.getuid(), hashlib.sha256(cfg['node_id'].encode()).hexdigest()[:24])
                try:
                    os.kill(metadata['pid'], 15)
                except ProcessLookupError:
                    pass
                for unused in range(60):
                    if not Path('/proc/%d' % metadata['pid']).exists():
                        break
                    time.sleep(.05)
                if cfg['node_id'] != node.node_id:
                    shutil.rmtree(str(Path(runtime).parent), ignore_errors=True)
        self.setup_cleanups.append(cleanup)
        return node, local, connection, service

    def connect(self, connection):
        result = self.rpc('node.ssh_add', connection)
        self.until(lambda: any(n['node_id'] == result['node_id'] and n['ready']
                              for n in self.rpc('snapshot')['nodes']), timeout=25)
        return result

    def test_missing_server_deploys_sdk_sets_up_and_connects_under_csh(self):
        shell = '/bin/tcsh' if Path('/bin/tcsh').exists() else '/bin/sh'
        node, local, connection, service = self.prepare('missing', shell)
        result = self.connect(connection)
        cfg = json.loads(node.config_path.read_text())
        self.assertEqual(result['node_id'], cfg['node_id'])
        self.assertTrue(Path(cfg['backend']['module_path'], 'camc_pkg/server_worker.py').is_file())
        self.assertTrue((node.home / '.local/bin/cam-server').is_symlink())
        self.assertTrue(all('\n' not in command for command in service.commands))
        self.assertTrue(self.rpc('snapshot')['nodes'][0]['management'])

    def test_old_server_upgrade_preserves_identity_grants_queues_and_agents(self):
        node, local, connection, service = self.prepare('old')
        before = node.config_path.read_bytes()
        original = node.status()['server_epoch']
        pending = self.echo()
        self.assertEqual(node.publish(pending)['type'], 'accepted')
        # A real tool in tmux must survive Server replacement.
        tool = self.base / 'survivor-tool'
        tool.write_text("#!/bin/sh\necho SETUP_SURVIVOR_READY\nexec /bin/cat\n")
        tool.chmod(0o700)
        result = node.cli('run', '--tool', str(tool), '--name', 'setup-survivor', '--path', str(node.home),
                          '--tool-launcher', 'none')
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = json.loads((node.home / '.cam/agents.json').read_text())
        agent = next(iter(agents.values())) if isinstance(agents, dict) else agents[0]
        agent_id = agent['id']
        self.agents_created.append((node, agent_id))
        self.until(lambda: 'SETUP_SURVIVOR_READY' in node.cli('capture', agent_id).stdout)
        agent = json.loads((node.home / '.cam/agents.json').read_text())[0]
        tmux_socket = agent.get('tmux_socket') or agent['runtime']['tmux']['socket']
        pane_command = [agent.get('tmux_bin') or 'tmux', '-S', tmux_socket,
                        'display-message', '-p', '-t', agent['tmux_session'] + ':0.0', '#{pane_pid}']
        pane_pid = subprocess.check_output(pane_command, env=node.env)
        self.connect(connection)
        cfg = json.loads(node.config_path.read_text())
        for key in ('node_id', 'clients', 'peers', 'runtime_dir'):
            self.assertEqual(cfg[key], node.config[key])
        self.assertNotEqual(node.status()['server_epoch'], original)
        self.assertEqual(node.query(pending['message_id'])['state'], 'queued')
        self.assertEqual(node.cli('status', agent_id).returncode, 0)
        self.assertEqual(subprocess.check_output(pane_command, env=node.env), pane_pid)
        self.assertEqual(Path(str(node.config_path) + '.before-update').read_bytes(),
                         json.dumps(json.loads(before), indent=2).encode())

    def fast_path(self, state):
        node, local, connection, service = self.prepare(state)
        before, epoch = node.config_path.read_bytes(), node.status()['server_epoch']
        (local / 'cam-server.bundle').unlink()
        self.connect(connection)
        self.assertEqual(node.status()['server_epoch'], epoch)
        self.assertEqual(node.config_path.read_bytes(), before)
        self.assertFalse((node.home / '.cam/server/releases').exists())

    def test_current_server_fast_path_does_not_need_bundle_or_restart(self):
        self.fast_path('current')

    def test_newer_server_is_not_downgraded_or_restarted(self):
        self.fast_path('newer')

    def test_stopped_current_server_starts_without_copying_bundle(self):
        node, local, connection, service = self.prepare('stopped')
        (local / 'cam-server.bundle').unlink()
        self.connect(connection)
        self.assertEqual(node.status()['node_id'], node.node_id)
        self.assertFalse((node.home / '.cam/server/releases').exists())

    def test_corrupt_bundle_and_wrong_identity_leave_old_server_untouched(self):
        node, local, connection, service = self.prepare('old')
        epoch, before = node.status()['server_epoch'], node.config_path.read_bytes()
        connection['expected_node'] = 'wrong-node'
        with self.assertRaisesRegex(Exception, 'node_identity_mismatch'):
            self.rpc('node.ssh_add', connection)
        connection.pop('expected_node')
        with (local / 'cam-server.bundle').open('ab') as stream:
            stream.write(b'bad')
        with self.assertRaisesRegex(Exception, 'checksum'):
            self.rpc('node.ssh_add', connection)
        self.assertEqual(node.status()['server_epoch'], epoch)
        self.assertEqual(node.config_path.read_bytes(), before)

    def test_wrong_cpu_and_unsafe_archive_do_not_replace_running_server(self):
        node, local, connection, service = self.prepare('old')
        epoch, before = node.status()['server_epoch'], node.config_path.read_bytes()
        bundle = local / 'cam-server.bundle'
        manifest_path = Path(str(bundle) + '.json')
        manifest = json.loads(manifest_path.read_text())
        manifest['target'] = 'unsupported-unknown-linux-musl'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(Exception, 'CPU'):
            self.rpc('node.ssh_add', connection)
        with tarfile.open(str(bundle), 'w') as archive:
            info = tarfile.TarInfo('../outside')
            info.size = 4
            archive.addfile(info, io.BytesIO(b'evil'))
        data = bundle.read_bytes()
        manifest.update(target=platform.machine() + '-unknown-linux-musl', size=len(data), sha256=hashlib.sha256(data).hexdigest())
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(Exception, 'invalid_bundle'):
            self.rpc('node.ssh_add', connection)
        self.assertFalse((node.home / '.cam/server/releases/outside').exists())
        self.assertEqual(node.status()['server_epoch'], epoch)
        self.assertEqual(node.config_path.read_bytes(), before)

    def test_manual_setup_update_and_status_share_the_same_flow(self):
        node, local, connection, service = self.prepare('stopped')
        for action in ('setup', 'update'):
            output = subprocess.check_output([str(routing.SERVER), action, '--bundle', str(local / 'cam-server.bundle')], env=node.env)
            self.assertEqual(json.loads(output)['node_id'], node.node_id)
        current = json.loads(subprocess.check_output([str(routing.SERVER), 'status'], env=node.env))
        self.assertEqual(current['node_id'], node.node_id)
        self.assertEqual(current['server_version'], '0.1.3')

    def test_failed_upgrade_restores_original_config_and_old_service(self):
        node, local, connection, service = self.prepare('old')
        before = node.config_path.read_bytes()
        wrapper = local / 'cam-server'
        wrapper.write_text(wrapper.read_text().replace('then exec ', 'then exit 7; exec ', 1))
        subprocess.check_call([sys.executable, str(SCRIPTS / 'package-server.py'), str(wrapper),
                               str(local / 'cam-server.bundle'), platform.machine() + '-unknown-linux-musl'])
        with self.assertRaisesRegex(Exception, 'server_start_failed'):
            self.rpc('node.ssh_add', connection)
        self.until(lambda: node.status()['node_id'] == node.node_id)
        self.assertEqual(json.loads(node.config_path.read_text()), json.loads(before))
        self.assertFalse((node.home / '.local/bin/cam-server').is_symlink())
        # The recovered service has a new PID and still needs fixture cleanup.
        import importlib.util
        spec = importlib.util.spec_from_file_location('setup_fixture', str(SCRIPTS / 'server_setup.py'))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        pid, identity = helper.managed_process(str(node.config_path))
        self.setup_cleanups.append(lambda: helper.stop(pid, identity, str(node.config_path)))


class ServerVersionTests(unittest.TestCase):
    def test_probe_checks_running_version_when_shared_home_binary_is_new(self):
        import importlib.util
        import tempfile
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('server_setup_probe', str(SCRIPTS / 'server_setup.py'))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / 'server.json'
            sdk = home / 'sdk/camc_pkg'
            sdk.mkdir(parents=True)
            (sdk / 'server_worker.py').touch()
            binary = home / '.local/bin/cam-server'
            binary.parent.mkdir(parents=True)
            binary.touch(mode=0o700)
            routing.private_json(config, dict(node_id='existing-node', runtime_dir='/tmp/unused-setup-test',
                                             backend=dict(module_path=str(sdk.parent), python=sys.executable)))
            req = dict(mode='probe', version='0.1.3', config=str(config))
            with patch.object(helper, 'version', return_value=(0, 1, 3)), \
                 patch.object(helper, 'managed_process', return_value=(123, ('exe', 'start'))), \
                 patch.object(helper, 'status', return_value=dict(node_id='existing-node', backend=True, server_version='0.1.2')):
                self.assertTrue(helper.ensure(req, str(home), str(home), 'fixture')['need_bundle'])

    def test_plain_json_and_alias_versions(self):
        plain = subprocess.check_output([str(routing.SERVER), 'version']).decode().strip()
        self.assertEqual(subprocess.check_output([str(routing.SERVER), '--version']).decode().strip(), plain)
        info = json.loads(subprocess.check_output([str(routing.SERVER), 'version', '--json']))
        self.assertEqual(plain, 'cam-server ' + info['version'])
        self.assertEqual(info['protocol'], 1)
