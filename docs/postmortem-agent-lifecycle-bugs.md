# Postmortem: Agent lifecycle bugs (2026-04-18)

## Summary

Three bugs in the camc / camflow agent-lifecycle path surfaced on the same
day, all sharing one root symptom: **agents that should have been gone kept
showing up again.** Duplicate "falcon" adoptions, agents re-appearing in
`camc list` after `rm`, and leftover camflow-fix agents after a demo all
traced back to missing or partial cleanup at different points of the
lifecycle.

This document captures the bugs, the investigation, the fixes, and the
lessons so the next agent lifecycle feature lands without regressing them.

## Timeline (rough)

- **Morning**: user reports duplicate `camflow-fix` agents lingering after
  the calculator demo.
- **Mid-day**: while fixing session-ID tracking (separate thread), we find
  that `camc heal` was adopting multiple orphan tmux sessions from the
  same workdir and naming every one of them identically
  (`falcon`, `falcon`, `falcon`, ...).
- **Afternoon**: user tries `camc stop` + `camc rm` to clear out a bad
  agent and finds it back in `camc list` on the next heal pass.

Each symptom was different, but they were all driven by the same pattern:
**a resource survived past its record's removal and got re-adopted**.

## Bug 1 — `camc rm` leaves tmux session and socket alive

### Symptom

```
$ camc stop abc12345
Stopped agent abc12345 (killed PID 2583644, tmux session still alive)

$ camc rm abc12345
Removed agent abc12345

$ camc heal
  Adopted orphan cam-abc12345 as agent ...
```

The agent reappears. `rm` removed the record but left the tmux session,
its socket, and its (still-running) shell. `heal` Phase 3's orphan scan
then adopted `cam-abc12345` again because the socket file still existed
and the tmux server still responded.

### Root cause

`cmd_rm` had a `--kill` flag that *gated* tmux teardown:

```python
if args.kill:
    _kill_monitor(a); tmux_kill_session(_sf(a, "tmux_session"))
store.remove(a["id"])
```

Without `--kill`, rm only deleted the agent record. The assumption was
that `stop` would have already torn down tmux — but `stop` (by design,
see `exit-stop-kill-migrate-spec.md`) kills the Claude process only and
keeps tmux alive so the session can be resumed or adopted. So the common
`stop` → `rm` cycle left tmux and socket orphaned.

A secondary issue: even when tmux was killed, the socket file under
`/tmp/cam-sockets/` occasionally lingered on NFS, which was enough for
the orphan scan to treat the session as live.

### Investigation

Reproduced by running `camc stop <id>` on a live agent and immediately
`camc rm <id>`, then checking `ls /tmp/cam-sockets/` — the `.sock` file
was still there. `tmux -S <sock> list-sessions` still listed the session.
A subsequent `camc heal` ran Phase 3 and re-adopted.

### Fix

`cmd_rm` now unconditionally kills the monitor, kills the tmux session,
and unlinks the socket file. `--kill` stays as a no-op for backward
compatibility. There is no legitimate "forget the record, keep the
session" use case; if the caller wants the session, they shouldn't be
running `rm`.

```python
_kill_monitor(a)
session = _sf(a, "tmux_session")
if session:
    tmux_kill_session(session)
    sock_path = _find_tmux_socket(session)
    if sock_path:
        try:
            os.unlink(sock_path)
        except OSError:
            pass
store.remove(a["id"])
```

Commit: `camc: rm always kills tmux session + socket`.

### Verification

Ran `camc run` → `camc stop` → `camc rm` → `camc heal`. Confirmed that:
1. The socket file is gone after `rm`.
2. `tmux list-sessions` returns error "no server running".
3. `heal` does not mention the agent.

Also cleaned up three stale test sockets
(`perm-auto2.sock`, `perm-auto-test.sock`, `perm-skip-test.sock`) that
had been left behind from early April test runs.

## Bug 2 — `camc heal` adopts orphans with identical display names

### Symptom

User ran a parallel workflow that spawned several agents in the same
`/home/scratch.hren/falcon/` directory. All of the monitor processes died
(NFS glitch), leaving the tmux sessions orphaned. `camc heal` adopted
them — and every adopted agent was named `falcon`:

```
$ camc list
falcon    running ...
falcon    running ...
falcon    running ...
falcon    running ...
falcon    running ...
```

No way to tell which was which in output, and any `camc <verb> falcon`
would hit the wrong one (or fail ambiguous).

### Root cause

`cmd_heal` Phase 3 built the display name as `os.path.basename(cwd)`
directly, with no uniqueness check against the existing store. Multiple
orphan sessions in the same cwd → same basename → identical names.

(An earlier draft of this fix also deduplicated *by workdir* and skipped
any orphan whose cwd already had a running agent. That was wrong:
multiple agents per workdir is a legitimate pattern for parallel work,
not something to prevent.)

### Investigation

Pre-check with `python3 -c "import json; [print(a['id'], (a['task'] or {}).get('name','')) for a in json.load(open('/home/hren/.cam/agents.json'))]"` showed ~5 records named `falcon` with different IDs, all `context_path=/home/scratch.hren/falcon`. The heal source made it obvious: `agent_name = os.path.basename(cwd) if cwd else "orphan-%s" % aid[:4]` and that name went straight into `store.save()` with no collision check.

Reproduced locally by creating three raw tmux sessions in
`/tmp/heal-name-test/` (no agents.json record), then running heal.
Confirmed all three adopted with name `heal-name-test`.

### Fix

Precompute a `known_names` set from the current store. When the chosen
basename collides, append the agent ID suffix (first 8 chars):

```python
base_name = os.path.basename(cwd) if cwd else "orphan-%s" % aid[:4]
agent_name = base_name
if agent_name in known_names:
    agent_name = "%s-%s" % (base_name, aid[:8])
known_names.add(agent_name)
```

After the fix, three orphans in the same cwd land as
`heal-name-test`, `heal-name-test-31f0668a`, `heal-name-test-348a1f1b`.
No dedup by workdir — all three coexist, just with distinct names.

Commit: `camc: disambiguate adopted orphan agent names by agent id`.

### Verification

Seeded 3 raw tmux sessions in `/tmp/heal-name-test/`; ran heal; inspected
`camc list`. Got three distinct names, all three adoptable independently.

## Bug 3 — camflow engine leaves child agents behind

### Symptom

After the calculator demo run in `demos/camflow-cli-demo/`, `camc list`
still showed six `camflow-fix` agents in `running` state hours later.
The engine had exited cleanly — it just never shut its children down.

### Root cause

Three compounding issues, per `~/.cam/camflow-cleanup-fix.md`:

1. **`agent_runner` calls bare `camc`.** If the engine is launched from an
   environment whose `PATH` doesn't include the install dir (common under
   cron or systemd), `subprocess.run(["camc", ...])` fails silently and
   the cleanup call never reaches camc at all.
2. **No `finally` block around the main loop.** If the engine crashed or
   was interrupted (Ctrl-C), the `state["current_agent_id"]` pointed at a
   live agent that nothing ever told to stop.
3. **No end-of-run sweep.** Even when the engine exited cleanly, agents
   from *earlier* failed runs accumulated — nothing reconciled the store
   against the engine's own state.

Note: the camflow engine lives outside this repo (this cam repo only
contains `demos/camflow-cli-demo/`, not the engine source). The fixes
below are prescribed; tracking the engine-side commit is out of scope for
this postmortem.

### Investigation

`camc list | grep camflow-` showed the accumulated agents. Log inspection
confirmed the engine's `_cleanup_agent()` was declared but only called
on the happy path. A quick `which camc` in the engine's working directory
(under the demo's `.claude` wrapper env) returned nothing, confirming
fix #1 was necessary.

### Fix (prescribed, engine-side)

Three defenses in depth:

1. **Resolve `camc` at startup.**
   ```python
   import shutil
   CAMC_BIN = shutil.which("camc") or "camc"
   if CAMC_BIN == "camc":
       log.warning("camc not found on PATH — cleanup may fail")
   ```
   Use `CAMC_BIN` everywhere instead of the literal string.

2. **Wrap the main loop in try / finally.**
   ```python
   try:
       # main loop
   finally:
       aid = state.get("current_agent_id")
       if aid:
           _cleanup_agent(aid)
   ```

3. **Sweep leftover `camflow-*` agents on exit.** Belt-and-suspenders:
   even if (1) and (2) fail, the end-of-run sweep reconciles.
   ```python
   def _cleanup_all_camflow_agents():
       proc = subprocess.run([CAMC_BIN, "--json", "list"],
                             capture_output=True, text=True, timeout=10)
       for a in json.loads(proc.stdout):
           name = (a.get("task") or {}).get("name", "")
           if name.startswith("camflow-"):
               subprocess.run([CAMC_BIN, "rm", a["id"]],
                              capture_output=True, timeout=10)
   ```

4. **Enforce at most one active camflow agent.** Before spawning a new
   agent node, check for any existing `camflow-*` agent and kill it.
   Prevents accumulation even if (1)–(3) fail.

## Common root cause

All three bugs share the same shape:

> **A resource survived past its owner's removal, and something downstream
> re-adopted the orphan.**

- Bug 1: `rm` removed the record, tmux + socket survived, heal re-adopted.
- Bug 2: orphans collected legitimate records via heal, but identical names
  made them indistinguishable in practice.
- Bug 3: engine exit removed nothing, running agents survived, next engine
  run saw yesterday's children alongside its own.

In each case the code had an implicit assumption that "my caller already
cleaned up" or "my callee will clean up for me." Neither party actually
did — so resources accumulated.

## Lessons

**1. Cleanup must be unconditional and co-located with ownership.**
If a record owns a resource, removing the record must tear the resource
down. No `--kill` flag that the happy path forgets to pass; no "caller
will do it" handshake that nobody actually wrote. Bug 1 was `rm` waiting
for someone else to kill tmux. Bug 3 was the engine waiting for an
outer shell to clean up its children. Both failed because the chain
had no guaranteed last link.

**2. End-of-scope must be try / finally, not "exit normally."**
The engine cleanup worked fine when the loop exited normally; it only
broke on exceptions and Ctrl-C. Anywhere a process manages external
state, the cleanup lives in `finally`, full stop.

**3. Reconciliation is cheap insurance.**
Bug 3's "sweep `camflow-*` agents on exit" is redundant with the
`finally` cleanup when things are healthy, and free when they aren't.
`camc heal` is the same idea at a higher level. If you own a class of
named resources, reconciliation against your own naming convention
catches whatever your point-fixes missed.

**4. Unique identifiers in display names matter as soon as there are
two of anything.**
Bug 2 wasn't a data-integrity issue — the stored IDs were already
distinct. It was a UX issue that poisoned every downstream command
that took a name. "Unique display name per live record" should be an
invariant, not an emergent property.

**5. NFS, sockets, and sub-second fs semantics are not your friends.**
Both Bug 1 and the heal implementation had edge cases where a socket
file lingered past the tmux server it belonged to. Explicit `os.unlink`
and stale-socket cleanup in heal Phase 3 handle the common case; the
long-term fix is to move sockets out of NFS-shared paths entirely, but
that's a separate project.

## Follow-ups

- [ ] Audit the rest of the codebase for other `if flag:` gated cleanups
  that could leave dangling state (grep for `tmux_kill_session`,
  `os.unlink`, `_kill_monitor`).
- [ ] Add an integration test: `run → stop → rm → heal` and assert that
  the agent does not reappear. Would have caught Bug 1.
- [ ] Add an integration test: multiple orphans in the same cwd adopt
  with unique names. Would have caught Bug 2.
- [ ] Land the camflow engine fixes and confirm a clean demo run leaves
  `camc list` empty of `camflow-*` agents.
- [ ] Consider making `camc rm --kill` a hard error (not a no-op) after
  a grace period, so nobody is silently depending on the old behavior.
