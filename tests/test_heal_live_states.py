"""Heal follows local tool evidence, rather than terminal registry labels."""
import json
from types import SimpleNamespace

import pytest
from camc_pkg import cli, transport
from camc_pkg.storage import AgentStore


@pytest.fixture
def repair(tmp_path, monkeypatch):
    store = AgentStore(str(tmp_path / 'agents.json'))
    monkeypatch.setattr(cli, '_find_live_agent_tool', lambda a: ('codex', (123, 'start')))
    monkeypatch.setattr(cli, '_live_tool_identity', lambda p, t: (123, 'start'))
    monkeypatch.setattr(cli, '_load_config', lambda t: SimpleNamespace(
        busy_pattern=None, ready_pattern=__import__('re').compile('^>'),
        strip_ansi=True, cursor_flag_support=True, confirm_rules=[]))
    monkeypatch.setattr(cli, 'capture_tmux', lambda *a, **kw: '>')
    monkeypatch.setattr(transport, 'tmux_cursor_flag', lambda s: 1)
    return store


@pytest.mark.parametrize('old', ['complete', 'completed', 'stopped', 'failed', 'killed', 'running'])
@pytest.mark.parametrize('idle', [True, False])
def test_repair_live_tool_regardless_of_old_label(repair, monkeypatch, old, idle):
    store = repair
    store.save(dict(id='a',hostname='local',status=old,state='editing',
                    tmux_session='cam-a',completed_at='yesterday',exit_reason='old error'))
    monkeypatch.setattr(transport,'tmux_cursor_flag',lambda s: int(idle))
    assert cli._heal_live_agent_states(store,'local') == ['a']
    a=store.get('a')
    assert (a['status'],a['state'],a['phase']) == ('alive','idle' if idle else 'running','started')
    assert a['completed_at'] is None and a['exit_reason'] is None


def test_shell_or_ssh_only_does_not_revive_agent(repair,monkeypatch):
    store=repair
    store.save(dict(id='a',hostname='local',status='completed',tmux_session='cam-a'))
    monkeypatch.setattr(cli,'_find_live_agent_tool',lambda a: None)
    assert cli._heal_live_agent_states(store,'local') == []
    assert store.get('a')['status']=='dead'


def test_remote_host_is_not_inspected(repair,monkeypatch):
    repair.save(dict(id='a',hostname='remote',status='completed'))
    monkeypatch.setattr(cli,'_find_live_agent_tool',lambda a: pytest.fail('remote inspected'))
    assert cli._heal_live_agent_states(repair,'local') == []


@pytest.mark.parametrize('race', ['stop','reboot','pid_reuse'])
def test_concurrent_change_wins_over_heal(repair,monkeypatch,race):
    store=repair
    store.save(dict(id='a',hostname='local',status='completed',tmux_session='cam-a',launch_id='old'))
    def capture(*a,**kw):
        if race=='stop':store.update('a',exit_reason='user stopped now')
        if race=='reboot':store.update('a',launch_id='new',status='alive',state='running',phase='initializing')
        if race=='pid_reuse':monkeypatch.setattr(cli,'_live_tool_identity',lambda p,t:(123,'new'))
        return '>'
    monkeypatch.setattr(cli,'capture_tmux',capture)
    assert cli._heal_live_agent_states(store,'local') == []
    if race=='reboot':assert store.get('a')['phase']=='initializing'
    else:assert store.get('a')['status']=='dead'


def test_stale_exit_receipt_cannot_immediately_undo_repair(repair,tmp_path):
    receipt=tmp_path/'tool-exit';receipt.write_text('0')
    repair.save(dict(id='a',hostname='local',status='completed',tmux_session='cam-a',
                     runtime={'custom_launch':{'exit_status_path':str(receipt)}}))
    assert cli._heal_live_agent_states(repair,'local')==['a']
    assert not receipt.exists()


def test_probe_uses_pane_zero_and_walks_wrappers(monkeypatch):
    calls=[]
    monkeypatch.setattr(transport,'_tmux_base',lambda s:['tmux','-S','socket'])
    monkeypatch.setattr(cli,'_run',lambda args,**kw:(calls.append(args) or (0,'100 0')))
    monkeypatch.setattr(cli,'_proc_children',lambda pid:{100:[101],101:[102]}.get(pid,[]))
    monkeypatch.setattr(cli,'_live_tool_identity',lambda pid,t:(pid,'start') if pid==102 else None)
    assert cli._find_live_agent_tool({'tmux_session':'cam-a','task':{'tool':'codex'}})==('codex',(102,'start'))
    assert 'cam-a:0.0' in calls[0]


def test_actual_process_identity_rejects_prompt_mentions(tmp_path):
    import subprocess,sys
    p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)','codex'])
    try:assert cli._live_tool_identity(p.pid,'codex') is None
    finally:p.terminate();p.wait()
    script=tmp_path/'codex';script.write_text('import time; time.sleep(30)')
    p=subprocess.Popen([sys.executable,str(script)])
    try:
        import time
        deadline=time.monotonic()+2
        identity=None
        while identity is None and time.monotonic()<deadline:
            identity=cli._live_tool_identity(p.pid,'codex')
            time.sleep(.01)
        assert identity[0]==p.pid
    finally:p.terminate();p.wait()


@pytest.mark.parametrize('old', ['alive', 'dead'])
def test_empty_pane_reconciles_false_alive_but_keeps_dead(repair, monkeypatch, old):
    import builtins,io
    repair.save(dict(id='a',hostname='local',status=old,state='stopped' if old=='dead' else 'idle',
                     phase='started',tmux_session='cam-a'))
    monkeypatch.setattr(cli,'_find_live_agent_tool',lambda a: ('',('sh',(123,'start'))))
    original=builtins.open
    monkeypatch.setattr(builtins,'open',lambda path,*a,**kw:
                        io.StringIO('') if str(path)=='/proc/123/task/123/children'
                        else original(path,*a,**kw))
    changed=cli._heal_live_agent_states(repair,'local')
    assert changed == (['a'] if old=='alive' else [])
    record=repair.get('a')
    assert record['status']=='dead'
    assert record['state']==('failed' if old=='alive' else 'stopped')
    if old=='alive':assert 'No tool process' in record['exit_reason']


def test_preview_does_not_write(repair):
    repair.save(dict(id='a',hostname='local',status='completed',tmux_session='cam-a'))
    assert cli._heal_live_agent_states(repair,'local',dry_run=True)==['a']
    assert repair.get('a')['status']=='dead'
