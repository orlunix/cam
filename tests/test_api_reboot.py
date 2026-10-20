"""Reboot starts a fresh session through run, retaining only launch settings."""
import argparse
import json
from pathlib import Path

import pytest

from camc_pkg import cli, transport
from camc_pkg.storage import AgentStore


@pytest.fixture
def reboot(tmp_path, monkeypatch):
    store = AgentStore(str(tmp_path / 'agents.json'))
    agent = dict(id='deadbeef', tmux_session='cam-deadbeef', status='running',
                 session_id='previous-session', session_binding='pending',
                 context_path=str(tmp_path),
                 task=dict(tool='codex', name='my-agent', tags=['test'], prompt='old task'),
                 runtime=dict(launcher_mode='none'))
    store.save(agent)
    monkeypatch.setattr(cli, 'AgentStore', lambda: store)
    monkeypatch.setattr(cli, 'PIDS_DIR', str(tmp_path / 'pids'))
    monkeypatch.setattr(cli, 'default_tool_launcher', lambda: 'none')
    monkeypatch.setattr(transport, 'ensure_camc_tmux_config', lambda: '')
    events = []
    alive = [True]
    monkeypatch.setattr(cli, 'tmux_session_exists', lambda _: alive[0])
    def kill(session):
        events.append(('kill', session))
        alive[0] = False
        return True
    monkeypatch.setattr(cli, 'tmux_kill_session', kill)
    monkeypatch.setattr(cli, '_kill_monitor', lambda _: events.append(('monitor',)))
    monkeypatch.setattr(cli, '_stop_agent_api_proxy', lambda *a, **kw: events.append(('proxy',)))
    def create(*args, **kwargs):
        assert not alive[0]
        events.append(('create', args, kwargs))
        return True
    monkeypatch.setattr(cli, 'create_tmux_session', create)
    monkeypatch.setattr(cli, '_gen_agent_id', lambda: pytest.fail('must keep agent ID'))
    return store, agent, events, alive


def request():
    return json.loads(Path(cli._launch_request_path('deadbeef')).read_text())


@pytest.mark.parametrize('tool', ['claude', 'codex', 'cursor', 'pi', 'my-tool'])
@pytest.mark.parametrize('state', ['running', 'stopped', 'missing-tmux'])
def test_reboot_all_tools_fresh_same_id(reboot, tool, state):
    store, agent, events, alive = reboot
    agent['task']['tool'] = tool if tool != 'my-tool' else 'others'
    if tool == 'my-tool':
        agent['task']['requested_tool'] = tool
    agent['status'] = 'running' if state == 'running' else 'stopped'
    alive[0] = state != 'missing-tmux'
    store.save(agent)
    cli.cmd_migrate(argparse.Namespace(id=agent['id']))
    assert [e[0] for e in events] == ([] if state == 'missing-tmux' else ['kill']) + ['monitor', 'proxy', 'create']
    queued = request()
    assert queued['tool'] == tool
    assert queued['prompt'] == ''
    assert 'resume_session' not in queued
    assert queued['_reboot'] is True
    assert queued['name'] == 'my-agent' and queued['tag'] == ['test']
    record = store.get('deadbeef')
    assert record['id'] == agent['id']
    assert record['runtime']['preparing'] is True
    assert 'session_id' not in record and 'session_binding' not in record
    assert events[-1][1][0] == 'cam-deadbeef'


@pytest.mark.parametrize('saved,explicit,default,expected', [
    (None, None, 'auto', 'auto'), (None, None, 'none', 'none'),
    ('auto', None, 'none', 'none'), ('auto', 'none', 'auto', 'none'),
    ('none', None, 'auto', 'auto'), ('none', None, 'none', 'none'),
    ('none', 'auto', 'none', 'auto'), ('custom', 'qsub -Is', 'none', 'qsub -Is'),
])
def test_reboot_launcher_default_and_override(reboot, monkeypatch, saved, explicit, default, expected):
    store, agent, _, _ = reboot
    agent['runtime']['launcher_mode'] = saved
    store.save(agent)
    monkeypatch.setattr(cli, 'default_tool_launcher', lambda: default)
    cli.cmd_migrate(argparse.Namespace(id=agent['id'], tool_launcher=explicit))
    assert request()['tool_launcher'] == expected


def test_reboot_api_uses_profile_not_old_port_or_session(reboot):
    store, agent, _, _ = reboot
    agent['api'] = dict(name='example-profile', proxy_port=12345, model='retired')
    agent['runtime']['storage'] = dict(isolate_tool_home=True)
    store.save(agent)
    cli.cmd_migrate(argparse.Namespace(id=agent['id']))
    queued = request()
    assert queued['api'] == 'example-profile'
    assert queued['no_default_api'] is False
    assert queued['isolate_tool_home'] is True
    assert 'proxy_port' not in queued and 'model' not in queued


def test_reboot_login_does_not_pick_new_default_api(reboot):
    _, agent, _, _ = reboot
    cli.cmd_migrate(argparse.Namespace(id=agent['id']))
    assert request()['no_default_api'] is True


@pytest.mark.parametrize('failure', ['custom', 'environment', 'to', 'kill'])
def test_invalid_reboot_does_not_launch(reboot, monkeypatch, failure):
    store, agent, events, _ = reboot
    args = argparse.Namespace(id=agent['id'])
    if failure == 'custom':
        agent['runtime']['launcher_mode'] = 'custom'
    elif failure == 'environment':
        agent['runtime']['custom_launch'] = dict(tool_env_keys=['TOKEN'])
    elif failure == 'to':
        args.to = 'other-host'
    else:
        monkeypatch.setattr(cli, 'tmux_kill_session', lambda _: False)
    store.save(agent)
    with pytest.raises(SystemExit):
        cli.cmd_migrate(args)
    assert not events
    assert store.get('deadbeef')['session_id'] == 'previous-session'


def test_reboot_custom_arguments_and_environment(reboot):
    store, agent, _, _ = reboot
    agent['runtime']['custom_launch'] = dict(tool_dir='/tools/bin',
        tool_args=['--workspace', '/path with spaces'], tool_env_keys=['TOKEN'])
    store.save(agent)
    cli.cmd_migrate(argparse.Namespace(id=agent['id'], tool_env=['TOKEN=secret']))
    queued = request()
    assert queued['tool_dir'] == '/tools/bin'
    assert cli._parse_tool_args(queued['tool_args']) == ['--workspace', '/path with spaces']
    assert queued['tool_env'] == ['TOKEN=secret']
    assert store.get('deadbeef')['runtime']['custom_launch']['tool_env'] == ['TOKEN=secret']


def test_reboot_tmux_creation_failure_retains_record(reboot, monkeypatch):
    store, agent, _, _ = reboot
    monkeypatch.setattr(cli, 'create_tmux_session', lambda *a, **kw: False)
    with pytest.raises(SystemExit):
        cli.cmd_migrate(argparse.Namespace(id=agent['id']))
    record = store.get('deadbeef')
    assert record['state'] == 'failed'
    assert not record['runtime']['preparing']
    assert not Path(cli._launch_request_path('deadbeef')).exists()


def test_preparing_agent_can_reboot_again_with_current_default(reboot, monkeypatch):
    monkeypatch.setattr(cli, 'default_tool_launcher', lambda: 'auto')
    store, agent, _, _ = reboot
    agent['runtime']['launcher_mode'] = 'auto'
    store.save(agent)
    cli.cmd_migrate(argparse.Namespace(id=agent['id']))
    first_launch = store.get(agent['id'])['launch_id']
    cli.cmd_migrate(argparse.Namespace(id=agent['id']))
    assert request()['tool_launcher'] == 'auto'
    assert store.get(agent['id'])['launch_id'] != first_launch
    assert request()['tag'] == ['test']


@pytest.mark.parametrize('launcher', ['auto', 'none', 'qsub -Is --label "two words"'])
def test_reboot_reuses_saved_settings_and_persists_overrides(reboot, monkeypatch, launcher):
    store, agent, _, _ = reboot
    saved = dict(tool_launcher=launcher, tool_env=['TOKEN=old', 'KEEP=a=b'],
                 tool_env_keys=['TOKEN', 'KEEP'], tool_dir='/saved/bin',
                 tool_args=['--model', 'a b'], use_env_tool=True)
    agent['runtime']['custom_launch'] = saved
    agent['runtime']['launcher_mode'] = 'custom'
    store.save(agent)
    monkeypatch.setattr(cli, 'default_tool_launcher', lambda: pytest.fail('saved launcher lost'))
    cli.cmd_migrate(argparse.Namespace(id=agent['id']))
    first = request()
    assert first['tool_launcher'] == launcher
    assert first['tool_env'] == saved['tool_env']
    assert first['use_env_tool'] is True
    assert cli._parse_tool_args(first['tool_args']) == saved['tool_args']
    cli.cmd_migrate(argparse.Namespace(id=agent['id'], tool_launcher='none',
                    tool_env=['TOKEN=new'], tool_dir='/new/bin', tool_args=['']))
    second = request()
    assert second['tool_env'] == ['KEEP=a=b', 'TOKEN=new']
    assert second['tool_launcher'] == 'none'
    assert second['tool_dir'] == '/new/bin'
    assert cli._parse_tool_args(second['tool_args']) == []
    persisted = store.get(agent['id'])['runtime']['custom_launch']
    assert persisted['tool_env'] == second['tool_env']
    assert persisted['tool_launcher'] == 'none'
    assert persisted['tool_args'] == []


@pytest.mark.parametrize('extra', [dict(tool_env=['INVALID']), dict(tool_args=['"broken'])])
def test_invalid_reboot_settings_keep_old_tmux(reboot, extra):
    _, agent, events, alive = reboot
    with pytest.raises(SystemExit):
        cli.cmd_migrate(argparse.Namespace(id=agent['id'], **extra))
    assert alive[0] and not events
