"""The reconnect skill's SSH bootstrap is safe, idempotent and concurrent."""
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys

import pytest

SCRIPT = (Path(__file__).parents[1] / '.agents/skills/managing-cam-tui/scripts/authorize_own_key.py')


@pytest.fixture
def setup(tmp_path):
    if not shutil.which('ssh-keygen'):
        pytest.skip('OpenSSH key generator required')
    private = tmp_path / 'identity'
    subprocess.check_call(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(private)])
    public = Path(str(private) + '.pub')
    return runpy.run_path(str(SCRIPT))['prepare'], private, public, tmp_path / 'ssh'


def test_authorize_twice_preserves_keys_permissions_and_private_key(setup):
    prepare, private, public, directory = setup
    original_private = private.read_bytes()
    directory.mkdir()
    authorized = directory / 'authorized_keys'
    prior = 'ssh-rsa unrelated-key-data existing-user\n'
    authorized.write_text(prior)
    first = prepare(public, directory)
    content = authorized.read_bytes()
    second = prepare(public, directory)
    assert first['ok'] and first['changed']
    assert second['ok'] and not second['changed']
    assert authorized.read_bytes() == content
    assert content.startswith(prior.encode())
    assert private.read_bytes() == original_private
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(authorized.stat().st_mode) == 0o600
    fingerprint = subprocess.check_output(['ssh-keygen', '-lf', str(public)], universal_newlines=True).split()[1]
    assert first['fingerprint'] == fingerprint


def test_check_is_read_only_then_confirms_ready(setup):
    prepare, _, public, directory = setup
    assert not prepare(public, directory, check=True)['ok']
    assert not directory.exists()
    prepare(public, directory)
    assert prepare(public, directory, check=True)['ok']


def test_existing_restricted_authorization_is_not_weakened(setup):
    prepare, _, public, directory = setup
    directory.mkdir(mode=0o700)
    key = ' '.join(public.read_text().split()[:2])
    original = 'from="host.example",command="echo restricted" ' + key + ' old-comment\n'
    authorized = directory / 'authorized_keys'
    authorized.write_text(original)
    authorized.chmod(0o600)
    result = prepare(public, directory)
    assert result['ok'] and not result['changed']
    assert authorized.read_text() == original


def test_owned_symlink_target_is_preserved(setup, tmp_path):
    prepare, _, public, directory = setup
    directory.mkdir(mode=0o700)
    target = tmp_path / 'shared-authorized-keys'
    target.write_text('# shared NFS target\n')
    (directory / 'authorized_keys').symlink_to(target)
    assert prepare(public, directory)['ok']
    assert (directory / 'authorized_keys').is_symlink()
    assert target.read_text().startswith('# shared NFS target\n')
    assert public.read_text().strip() in target.read_text()


def test_refuses_private_keys_and_foreign_ownership(setup, monkeypatch):
    prepare, private, public, directory = setup
    with pytest.raises(ValueError, match='private key'):
        prepare(private, directory)
    uid = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: uid + 1)
    with pytest.raises(ValueError, match='owned by this user'):
        prepare(public, directory)
    assert not directory.exists()


def test_concurrent_bootstrap_does_not_duplicate_or_truncate(setup):
    _, _, public, directory = setup
    command = [sys.executable, str(SCRIPT), '--public-key', str(public), '--ssh-dir', str(directory)]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
    for process in processes:
        out, err = process.communicate(timeout=15)
        assert process.returncode == 0, err.decode() + out.decode()
        assert json.loads(out)['ok']
    assert (directory / 'authorized_keys').read_text().splitlines() == [public.read_text().strip()]


def test_broken_managed_link_is_not_replaced(setup):
    prepare, _, public, directory = setup
    directory.mkdir(mode=0o700)
    authorized = directory / 'authorized_keys'
    authorized.symlink_to(directory / 'missing-target')
    with pytest.raises(ValueError, match='broken'):
        prepare(public, directory)
    assert authorized.is_symlink() and not authorized.exists()
