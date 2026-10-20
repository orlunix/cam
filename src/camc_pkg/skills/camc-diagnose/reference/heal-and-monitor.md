# Heal and lifecycle reconciliation

Agent `status` is `alive` or `dead`. Alive agents have `state=idle|running`
and `phase=initializing|started`; dead agents have `state=stopped|failed|killed`
and no startup phase. Finishing a task returns to idle, not completed.

## Heal

```bash
~/.cam/camc heal                 # reconcile local evidence, repair missing monitors
~/.cam/camc heal --monitor       # same default operation
~/.cam/camc heal --restart       # replace verified local monitors, then normal heal
~/.cam/camc heal --tmux          # refresh managed tmux configuration
~/.cam/camc heal --agents        # explicit legacy record migration only
```

`heal --upgrade` is deprecated compatibility, not the maintenance workflow.
The retired `cam` server is not involved. Default heal can recover cron's
node-local scheduler when work exists; it does not edit system crontab.

Heal checks the tool process rooted at window 0/pane 0. It does not infer
liveness from a retained tmux shell, an extra pane, a stale PID, old screen
text, or an SSH/battach/qsub transport alone. Verified local tools restore
incorrect terminal labels to alive/started, clear old failure metadata and
stale exit receipts, and classify idle using the current input UI; otherwise
they are running. Process identity and the record are checked again before
writing so concurrent stop/reboot and PID reuse win over stale observations.
Remote-host records are not probed as local processes. If a tool only exists
on an LSF execution host, transport presence on the submit host is insufficient
for revival; obtain target-side evidence before claiming it is alive.

A live tmux server behind a temporarily failing socket query is left alone.
Confirmed missing local sessions become failed with a reason. Dead empty
shells remain dead; an alive record with a verified childless shell is marked
failed because its tool exited without a reliable exit result. Local orphan adoption and stale-socket cleanup are scoped
to owned sockets and the same HOME.

## Monitor and startup worker

The monitor observes screen activity and input readiness, records idle/running,
and applies adapter confirmation rules when enabled. It does not categorize
planning/editing/testing/committing or infer completed from prompt counts.
It records real exit receipts and checks session health. A monitor crash
retries up to five times and then records monitor_error; it does not declare
a working tool dead.

Run/reboot create tmux first; preparation, launcher waits, and the tool run in
pane 0. The worker supervises startup to started or a reasoned failure. The
assistant must also inspect stalled startup and handle routine trust/menu
choices as described in managing-camc. A successful run command is not
startup completion. Do not inject arbitrary characters into a stalled UI.

Logs: `~/.cam/logs/monitor-<id>.log` and `monitor-<id>.stderr`.
Registry: `~/.cam/agents.json`; events: `~/.cam/events.jsonl`.
