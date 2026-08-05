# Codex Session Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably bind a newly launched Codex agent to its exact local rollout JSONL and include that file in `camc archive`, without changing Claude or non-Codex session behavior.

**Architecture:** A short-lived post-launch binder follows the CAMC tmux pane to its Codex process, inspects that process's open file descriptors, and accepts a rollout only after its path and `session_meta` agree with the agent's canonical workdir and UUID. `session_id` remains the common record field, but archive dispatches on `task.tool`: Claude retains its current collector, Codex adds the validated rollout, and all other tools add no tool-session file.

**Tech Stack:** Python 3 standard library, Linux `/proc`, pytest, existing CAMC `AgentStore` and tar.gz archive format.

## Global Constraints

- Do not guess from newest files, `codex resume --last`, tmux history, or workdir-only candidates.
- Bind only a Codex process descended from the target tmux pane; a missing or ambiguous binding leaves `session_id` empty.
- Validate rollout path under the process `CODEX_HOME` (or `~/.codex`), filename UUID, `session_meta.session_id`, canonical `cwd`, and launch time before updating the record.
- Binder is detached and bounded; `camc run` must not wait for it.
- Preserve Claude's current launch, extraction, archive, and reboot behavior byte-for-byte where possible.
- Other tools must continue to archive CAMC metadata, events, monitor logs, and tmux capture only.
- Do not commit or push as part of this implementation task.

---

### Task 1: Validate one Codex rollout from process-level evidence

**Files:**
- Modify: `src/camc_pkg/cli.py`
- Create: `tests/test_codex_session_archive.py`

**Interfaces:**
- Produces: `_validated_codex_rollout(pid, workdir, started_at) -> tuple[str, str] | None`.
- Consumes: `/proc/<pid>/fd`, process `CODEX_HOME`, and the first `session_meta` JSONL object.

- [ ] **Step 1: Write failing tests**

```python
def test_validated_codex_rollout_requires_fd_uuid_meta_cwd_and_start(monkeypatch, tmp_path):
    session_id, path = _make_rollout(tmp_path, cwd="/work", started_at="2026-08-05T10:00:00Z")
    _fake_proc_fd(monkeypatch, 4321, path, codex_home=tmp_path)
    assert _validated_codex_rollout(4321, "/work", "2026-08-05T10:00:00Z") == (session_id, str(path))

def test_validated_codex_rollout_rejects_wrong_cwd_or_uuid(monkeypatch, tmp_path):
    _, path = _make_rollout(tmp_path, cwd="/other", started_at="2026-08-05T10:00:00Z")
    _fake_proc_fd(monkeypatch, 4321, path, codex_home=tmp_path)
    assert _validated_codex_rollout(4321, "/work", "2026-08-05T10:00:00Z") is None
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_codex_session_archive.py`

Expected: import failure because `_validated_codex_rollout` does not exist.

- [ ] **Step 3: Implement the smallest validator**

```python
def _validated_codex_rollout(pid, workdir, started_at):
    # enumerate only this PID's fd links; accept exactly one validated path
    # under its CODEX_HOME/sessions directory
    ...
```

Read `CODEX_HOME` from `/proc/<pid>/environ`, default to `~/.codex`; parse only `session_meta`; use `os.path.realpath` for root and cwd comparisons; return `None` for missing, malformed, stale, or multiple candidates.

- [ ] **Step 4: Run the focused test to verify GREEN**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_codex_session_archive.py`

Expected: all validator cases pass.

### Task 2: Bind a launched Codex agent without delaying `camc run`

**Files:**
- Modify: `src/camc_pkg/cli.py`
- Modify: `tests/test_codex_session_archive.py`

**Interfaces:**
- Produces: `_bind_codex_session(agent_id, tmux_session, tmux_socket, workdir, started_at) -> None`.
- Consumes: target tmux pane PID and `_validated_codex_rollout`.
- Persists: `session_id`, `session_path`, and `session_binding` only for a matching live Codex agent.

- [ ] **Step 1: Write failing tests**

```python
def test_binder_updates_only_the_target_codex_agent(monkeypatch, store):
    monkeypatch.setattr(cli, "_find_codex_pid_for_pane", lambda *_: 4321)
    monkeypatch.setattr(cli, "_validated_codex_rollout", lambda *_: (UUID, PATH))
    _bind_codex_session("agent-a", "cam-agent-a", SOCK, "/work", START)
    assert store.get("agent-a")["session_id"] == UUID
    assert store.get("agent-b")["session_id"] == ""

def test_binder_never_updates_when_evidence_is_missing(monkeypatch, store):
    monkeypatch.setattr(cli, "_validated_codex_rollout", lambda *_: None)
    _bind_codex_session("agent-a", "cam-agent-a", SOCK, "/work", START)
    assert store.get("agent-a")["session_id"] == ""
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_codex_session_archive.py`

Expected: failure because `_bind_codex_session` does not exist.

- [ ] **Step 3: Implement minimal binding and detached launch**

Add a bounded binder command/helper after a successful Codex tmux launch. It may poll briefly for the pane's Codex descendant PID, but `cmd_run` starts it detached and returns without waiting. Guard the final store update by agent id, `tool == "codex"`, socket/session identity, and started-at value so an old binder cannot alter a restarted record.

- [ ] **Step 4: Run the focused test to verify GREEN**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_codex_session_archive.py`

Expected: binding is target-specific and missing evidence is a safe no-op.

### Task 3: Add Codex rollout JSONL to tool-aware archives

**Files:**
- Modify: `src/camc_pkg/cli.py`
- Modify: `tests/test_codex_session_archive.py`

**Interfaces:**
- Consumes: Codex agent `session_id` and validated `session_path`.
- Produces: `codex/session.jsonl` in the existing tar.gz when both metadata and file validation pass.

- [ ] **Step 1: Write failing tests**

```python
def test_archive_includes_only_validated_codex_rollout(tmp_path, monkeypatch, capsys):
    archive = _archive_agent(tool="codex", session_id=UUID, session_path=PATH)
    assert _tar_members(archive)["codex/session.jsonl"] == Path(PATH).read_bytes()

def test_archive_does_not_guess_codex_rollout_when_unbound(tmp_path, monkeypatch):
    archive = _archive_agent(tool="codex", session_id="", session_path="")
    assert "codex/session.jsonl" not in _tar_members(archive)
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_codex_session_archive.py`

Expected: archive contains no `codex/session.jsonl` before the collector exists.

- [ ] **Step 3: Implement tool-aware collection**

Branch archive session resolution and collection by `task.tool`. Retain the current Claude code path. For Codex, never run Claude extractors; revalidate `session_path` against the stored UUID and canonical workdir before reading it, then add it as `codex/session.jsonl`. For unbound/invalid paths print a Codex-specific warning and continue building the archive.

- [ ] **Step 4: Run the focused test to verify GREEN**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_codex_session_archive.py`

Expected: a validated rollout is present; unbound Codex never gets a guessed transcript.

### Task 4: Regression verification and bundle generation

**Files:**
- Modify: `dist/camc`
- Modify: `dist/BUILD_LOG.md`

- [ ] **Step 1: Run focused and Claude regression tests**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/cam-node-transport-venv/bin/python -m pytest -q tests/test_codex_session_archive.py tests/test_camc_session_id.py`

Expected: all pass.

- [ ] **Step 2: Build and inspect bundle**

Run: `PYTHONPATH=src python3 build_camc.py && ./dist/camc version`

Expected: build succeeds and `dist/camc` reports its version.

- [ ] **Step 3: Run final static verification**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m py_compile src/camc_pkg/cli.py && git diff --check`

Expected: both commands exit 0.

## Plan Self-Review

- Scope coverage: Tasks 1–2 bind new Codex sessions without startup wait; Task 3 archives only the exact validated file; Task 4 protects Claude and the generated bundle.
- No-guess rule: all binding and archive tasks reject missing, invalid, stale, or ambiguous evidence.
- Compatibility: Claude uses its existing extractor; non-Codex/non-Claude agents do not gain session-file discovery.
- No commit or push: this task leaves the resulting diff for user review.
