"""Two-level lifecycle and startup supervision; no real user agents."""
import argparse
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from camc_pkg import cli, monitor, transport
from camc_pkg.storage import AgentStore, normalize_agent
from camc_pkg.monitor_features import BootPromptFeature, MonitorRuntime, MonitorSnapshot


@pytest.mark.parametrize('old,state,expected', [
    ('running','initializing',('alive','running','initializing')),
    ('pending',None,('alive','running','initializing')),
    ('starting',None,('alive','running','initializing')),
    ('running','idle',('alive','idle','started')),
    *[('running',s,('alive','running','started')) for s in
      ('planning','editing','testing','committing')],
    ('completed','idle',('dead','stopped',None)),
    *[(s,'editing',('dead',s,None)) for s in ('stopped','failed','killed')],
])
def test_legacy_record_normalizes_and_persists(tmp_path, old, state, expected):
    path = tmp_path/'agents.json'
    path.write_text(json.dumps([dict(id='a',status=old,state=state,exit_reason='original')]))
    store = AgentStore(str(path))
    record = store.get('a')
    assert tuple(record[k] for k in ('status','state','phase')) == expected
    store.update('a', task={'name':'kept'})
    saved = json.loads(path.read_text())[0]
    assert tuple(saved[k] for k in ('status','state','phase')) == expected
    assert saved['exit_reason'] == 'original'


def test_late_monitor_cannot_overwrite_dead_or_new_generation(tmp_path):
    store = AgentStore(str(tmp_path/'agents.json'))
    store.save(dict(id='a',launch_id='new',status='dead',state='stopped'))
    store.update('a',state='idle',phase='started')
    assert store.get('a')['state']=='stopped'
    store.update('a', expected_launch_id='new', status='dead', state='failed')
    assert store.get('a')['state']=='stopped'
    store.update('a',expected_launch_id='old',status='dead',state='failed')
    assert store.get('a')['state']=='stopped'


@pytest.fixture
def supervised(tmp_path, monkeypatch):
    store=AgentStore(str(tmp_path/'agents.json'))
    store.save(dict(id='1234abcd',launch_id='g1',status='alive',state='running',phase='initializing'))
    monkeypatch.setattr(cli,'AgentStore',lambda:store)
    launch=dict(command=[sys.executable,'-c','import time; time.sleep(.4)'],
                env=dict(os.environ),workdir=str(tmp_path),known_tool=True,startup_timeout=3)
    return store,launch


def test_startup_guard_requires_readiness_even_on_zero_exit(supervised):
    store,launch=supervised
    assert cli._run_foreground_tool('1234abcd',launch)==1
    assert store.get('1234abcd')['state']=='failed'
    assert 'before startup readiness' in store.get('1234abcd')['exit_reason']


def test_startup_guard_times_out_with_reason_and_reaps_child(supervised):
    store,launch=supervised
    launch['command']=[sys.executable,'-c','import time; time.sleep(30)']
    launch['startup_timeout']=0
    assert cli._run_foreground_tool('1234abcd',launch)==1
    assert store.get('1234abcd')['status']=='dead'
    assert 'did not become ready' in store.get('1234abcd')['exit_reason']


def test_startup_guard_hands_off_only_after_started(supervised):
    store,launch=supervised
    def ready():
        time.sleep(.1)
        store.update('1234abcd',state='idle',phase='started')
    thread=threading.Thread(target=ready)
    thread.start()
    try:
        assert cli._run_foreground_tool('1234abcd',launch)==0
        assert store.get('1234abcd')['phase']=='started'
    finally:
        thread.join()


def test_boot_timeout_is_failure_not_idle():
    cfg=SimpleNamespace(startup_wait=2)
    runtime=MonitorRuntime('a',cfg)
    runtime.in_initializing=True
    runtime.boot_deadline=2
    actions=BootPromptFeature().after_confirm(SimpleNamespace(now=3),runtime)
    fields=next(a['fields'] for a in actions if a['kind']=='store_update')
    assert fields['status']=='dead' and fields['state']=='failed'
    assert fields['exit_reason']


@pytest.mark.parametrize("reboot", [False, True])
def test_monitor_failure_does_not_kill_started_tool(tmp_path,monkeypatch,reboot):
    store=AgentStore(str(tmp_path/'agents.json'))
    store.save(dict(id='a',launch_id='old',status='alive',state='running',phase='started',task={'tool':'codex'}))
    monkeypatch.setattr(monitor,'AgentStore',lambda:store)
    monkeypatch.setattr(monitor,'EventStore',lambda:None)
    monkeypatch.setattr(monitor,'LOGS_DIR',str(tmp_path))
    monkeypatch.setattr('camc_pkg.PIDS_DIR',str(tmp_path))
    monkeypatch.setattr(monitor,'_load_config',lambda t:SimpleNamespace(confirm_rules=[]))
    monkeypatch.setattr(monitor,'_load_boot_config',lambda t:None)
    monkeypatch.setattr(monitor.time,'sleep',lambda s:None)
    monkeypatch.setattr(monitor.logging,'basicConfig',lambda **kw:None)
    monkeypatch.setattr(monitor.logging.root,'handlers',[])
    attempts = []
    def broken(*a,**kw):
        attempts.append(1)
        if reboot:
            store.update('a', launch_id='new')
        raise RuntimeError('test fault')
    monkeypatch.setattr(monitor,'run_monitor_loop',broken)
    monitor._run_monitor('a')
    assert store.get('a')['status']=='alive'
    assert store.get('a')['phase']=='started'
    if reboot:
        assert len(attempts) == 1
        assert not store.get('a').get('monitor_error')
    else:
        assert len(attempts) == 6
        assert store.get('a')['monitor_error']


def test_prune_keeps_stopped_shell_dead(tmp_path,monkeypatch):
    store=AgentStore(str(tmp_path/'agents.json'))
    store.save(dict(id='a',status='dead',state='stopped',tmux_session='cam-a'))
    monkeypatch.setattr(cli,'AgentStore',lambda:store)
    monkeypatch.setattr(cli,'tmux_session_exists',lambda s:True)
    cli.cmd_prune(argparse.Namespace(orphans=False,dry_run=False))
    assert store.get('a')['status']=='dead'
    assert store.get('a')['state']=='stopped'


@pytest.mark.parametrize("tool", ["claude", "codex", "cursor", "pi"])
def test_adapters_have_no_semantic_state_or_completion_config(tool):
    from camc_pkg.adapters import _EMBEDDED_CONFIGS, _parse_toml, AdapterConfig
    data = _parse_toml(_EMBEDDED_CONFIGS[tool + ".toml"])
    assert "state" not in data and "completion" not in data
    config = AdapterConfig(data)
    assert not hasattr(config, "state_patterns")
    assert not hasattr(config, "completion_strategy")


def test_queue_output_cannot_claim_tool_started():
    cfg = SimpleNamespace(startup_wait=2)
    runtime = MonitorRuntime("a", cfg)
    runtime.in_initializing = True
    runtime.feature_state["waiting_for_launcher"] = True
    assert BootPromptFeature().after_confirm(
        SimpleNamespace(now=3, screen_busy=True), runtime) == []
    assert runtime.in_initializing


def test_long_queue_wait_preserves_initializing_until_target_runs(supervised, tmp_path):
    import shlex
    from camc_pkg.tool_launcher import build_launcher_command, build_resolved_tool_command
    store, launch = supervised
    receipt, submitted, gate, ready = [tmp_path / name for name in
                                       ('receipt', 'submitted', 'dispatch', 'tool-ready')]
    store.update('1234abcd', runtime={'launcher': {'launch_status_file': str(receipt)}})
    target = tmp_path / 'target-tool'
    target.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo fixture; exit 0; fi\n'
                      ': > %s\nsleep .2\n' % shlex.quote(str(ready)))
    target.chmod(0o700)
    launcher = tmp_path / 'queued-launcher.py'
    launcher.write_text('import pathlib,sys,time,os\n'
                        'pathlib.Path(%r).touch()\n'
                        'while not pathlib.Path(%r).exists():time.sleep(.01)\n'
                        'os.execl("/bin/sh","sh","-c",sys.argv[1])\n' %
                        (str(submitted), str(gate)))
    command = build_resolved_tool_command([str(target)], str(target), str(tmp_path),
                                         started_status_path=str(receipt),
                                         started_transport='custom')
    launch.update(command=build_launcher_command([sys.executable, str(launcher)], command),
                  startup_timeout=.05)
    observed = []
    def dispatch():
        deadline = time.monotonic() + 4
        while not submitted.exists() and time.monotonic() < deadline:time.sleep(.01)
        time.sleep(.3)  # Longer than the foreground startup timeout.
        observed.append((store.get('1234abcd')['phase'], receipt.exists()))
        gate.touch()
        while not ready.exists() and time.monotonic() < deadline:time.sleep(.01)
        if ready.exists():store.update('1234abcd', state='idle', phase='started')
    thread = threading.Thread(target=dispatch)
    thread.start()
    try:
        assert cli._run_foreground_tool('1234abcd', launch) == 0
        assert observed == [('initializing', False)]
        assert receipt.read_text().strip() == 'custom'
        assert store.get('1234abcd')['status'] == 'alive'
        assert store.get('1234abcd')['phase'] == 'started'
    finally:
        gate.touch()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_queue_cancellation_preserves_stopped_and_reaps_launcher(supervised, tmp_path):
    store, launch = supervised
    store.update('1234abcd', runtime={'launcher': {'launch_status_file': str(tmp_path/'not-dispatched')}})
    launch.update(command=[sys.executable, '-c', 'import time;time.sleep(30)'], startup_timeout=0)
    def stop():
        time.sleep(.1)
        store.update('1234abcd', status='dead', state='stopped')
    thread = threading.Thread(target=stop)
    thread.start()
    try:
        assert cli._run_foreground_tool('1234abcd', launch) == 1
        assert store.get('1234abcd')['state'] == 'stopped'
    finally:
        thread.join()


def test_startup_timeout_remains_active_after_dispatch(supervised, tmp_path):
    store, launch = supervised
    receipt = tmp_path/'receipt'
    receipt.write_text('custom\n')
    store.update('1234abcd', runtime={'launcher': {'launch_status_file': str(receipt)}})
    launch.update(command=[sys.executable, '-c', 'import time;time.sleep(30)'], startup_timeout=0)
    assert cli._run_foreground_tool('1234abcd', launch) == 1
    assert store.get('1234abcd')['state'] == 'failed'
    assert 'did not become ready' in store.get('1234abcd')['exit_reason']


def test_unknown_tool_prompt_is_owned_by_launch_worker():
    cfg = SimpleNamespace(startup_wait=2)
    runtime = MonitorRuntime('a', cfg)
    record = dict(phase='initializing', task={'tool': 'others',
                  'requested_tool': 'custom-tool', 'prompt': 'send once'})
    monitor._refresh_boot_runtime(runtime, SimpleNamespace(get=lambda _id: record), 'a', None)
    assert runtime.boot_prompt == ''


def test_short_queued_custom_tool_exit_uses_target_receipt(supervised, tmp_path, monkeypatch):
    store, launch = supervised
    receipt = tmp_path/'receipt'
    store.update('1234abcd', runtime={'launcher': {'launch_status_file': str(receipt)}})
    launch.update(command=[sys.executable, '-c',
                          'import pathlib;pathlib.Path(%r).write_text("custom\\n")' % str(receipt)],
                  known_tool=False, prompt='do not paste after exit', startup_wait=0,
                  startup_timeout=0)
    monkeypatch.setattr(cli, 'tmux_send_input', lambda *a, **kw: pytest.fail('tool already exited'))
    assert cli._run_foreground_tool('1234abcd', launch) == 0
    assert store.get('1234abcd')['phase'] == 'started'
