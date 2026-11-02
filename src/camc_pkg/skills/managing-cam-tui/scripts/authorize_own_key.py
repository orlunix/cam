#!/usr/bin/env python3
"""Idempotently authorize this user's public SSH key; never copy private keys."""
import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat


def key_identity(line):
    try:
        words = shlex.split(line, comments=True)
        for index, word in enumerate(words[:-1]):
            if not word.startswith(('ssh-', 'ecdsa-', 'sk-')):
                continue
            data = base64.b64decode(words[index + 1].encode('ascii'), validate=True)
            size = int.from_bytes(data[:4], 'big')
            if len(data) > 4 + size and data[4:4 + size] == word.encode('ascii'):
                return word, words[index + 1], data
    except (ValueError, binascii.Error, UnicodeError):
        pass
    return None


def prepare(public_key, ssh_dir, check=False):
    public_key, ssh_dir = Path(public_key).expanduser(), Path(ssh_dir).expanduser()
    if public_key.suffix != '.pub':
        raise ValueError('Use the .pub file, never a private key')
    info = public_key.stat()
    if info.st_uid != os.getuid() or info.st_size > 16384:
        raise ValueError('Public key must be a small file owned by this user')
    lines = [line.strip() for line in public_key.read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    identity = key_identity(lines[0]) if len(lines) == 1 else None
    if not identity:
        raise ValueError('Expected exactly one valid SSH public key')
    if not ssh_dir.exists() and not check:
        ssh_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if ssh_dir.exists():
        info = ssh_dir.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('SSH directory must belong to this user')
    original = ssh_dir / 'authorized_keys'
    if original.is_symlink() and not original.exists():
        raise ValueError('Preserve the broken authorized_keys link; repair its target first')
    authorized = original.resolve()
    if authorized.exists():
        info = authorized.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('authorized_keys must be a regular file owned by this user')
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(identity[2]).digest()).decode().rstrip('=')
    result = {'stage': 'self_authorization', 'public_key': str(public_key),
              'authorized_keys': str(original), 'fingerprint': fingerprint}

    def contains(text):
        return any((key_identity(line) or (None, None))[:2] == identity[:2]
                   for line in text.splitlines())

    if check:
        present = authorized.exists() and contains(authorized.read_text(encoding='utf-8', errors='surrogateescape'))
        private = (ssh_dir.exists() and authorized.exists() and
                   stat.S_IMODE(ssh_dir.stat().st_mode) == 0o700 and
                   stat.S_IMODE(authorized.stat().st_mode) == 0o600)
        result.update(ok=bool(present and private), changed=False,
                      result='ready' if present and private else 'needs_setup')
        return result
    private_before = (stat.S_IMODE(ssh_dir.stat().st_mode) == 0o700 and
                      authorized.exists() and
                      stat.S_IMODE(authorized.stat().st_mode) == 0o600)
    os.chmod(str(ssh_dir), 0o700)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(authorized), flags, 0o600)
    with os.fdopen(fd, 'a+', encoding='utf-8', errors='surrogateescape') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Refusing to change another user\'s authorization file')
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.seek(0)
            existing = stream.read()
            added = not contains(existing)
            if added:
                if existing and not existing.endswith('\n'):
                    stream.write('\n')
                stream.write(lines[0] + '\n')
            os.fchmod(stream.fileno(), 0o600)
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    result.update(ok=True, changed=bool(added or not private_before),
                  result='authorized' if added else 'already_authorized')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-key', required=True)
    parser.add_argument('--ssh-dir', default='~/.ssh')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        result = prepare(args.public_key, args.ssh_dir, args.check)
    except (OSError, ValueError) as exc:
        result = {'stage': 'self_authorization', 'ok': False, 'error': str(exc)}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
