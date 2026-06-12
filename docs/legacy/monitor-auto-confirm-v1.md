# Legacy: Monitor Auto-Confirm v1 (pre-2026-06-10 simplification)

Snapshot of the auto-confirm behavior **before** simplification to
"TOML-only". Captured here so future readers can recover the rationale
without `git blame` archaeology. The new behavior is documented in
`src/camc_pkg/monitor_features.py` and `src/camc_pkg/monitor.py`.

## Feature pipeline (unchanged in v2)

The monitor's per-tick logic was reorganized into a 3-phase pipeline:

| Phase           | Feature              | Order |
|-----------------|----------------------|-------|
| before_confirm  | StateManagerFeature  | 10    |
| confirm         | AutoConfirmationFeature | 20 |
| after_confirm   | StateManagerFeature  | 10    |

A `halt_cycle` action from the confirm phase short-circuits the
after_confirm phase entirely (the inherited "sleep + continue" semantics
of the legacy step-3 path).

## v1 AutoConfirmationFeature behavior (now archived)

`confirm(snap, runtime)` implemented FIVE distinct gates, in order:

1. **Cooldown gate** — `now - runtime.last_confirm < cfg.confirm_cooldown`
   suppresses the fire. *(kept in v2 — runtime mechanic, not pattern
   matching)*
2. **`bare_prompt` skip** — if a bare prompt line (`❯`/`>`/`›` alone)
   was visible in the last 5 lines, the feature refused to confirm,
   reasoning that the confirm text was stale scrollback. *(REMOVED in
   v2 — TOML rules are now the only decision-maker)*
3. **TOML rule match** — `should_auto_confirm(snap.output, cfg)` walks
   the `[[confirm]]` rules from the adapter TOML; first match wins. The
   rule supplies `(response, send_enter, pattern_str, matched_text)`.
   *(kept in v2 — this IS the TOML-only path)*
4. **1-spam guard** — if the matched response was `"1"` and the screen
   tail contained three or more consecutive `1` characters
   (`re.search(r"1{3,}", "\n".join(snap.tail_lines))`), the feature
   suppressed the fire and pushed `last_confirm` forward by
   `60 - confirm_cooldown` seconds so the cooldown gate would block it
   again for a minute. Rationale: a runaway loop where the agent
   echoed `1` characters back into the pane could otherwise feed
   itself. *(REMOVED in v2)*
5. **Successful fire** — send `response` (or `Enter` if `response`
   empty + `send_enter`), update `last_confirm` / `last_change`, clear
   `idle_confirmed`, set `has_worked=True`, emit `auto_confirm` event,
   halt the cycle with `sleep=cfg.confirm_sleep`. *(kept in v2)*

## v1 stuck fallback (also archived)

`monitor.py` `run_monitor_loop` contained an inline fallback after the
phase pipeline:

```python
stuck_threshold = getattr(config, "stuck_timeout", 120)
if (runtime.has_worked and not runtime.idle_confirmed
        and idle_for >= stuck_threshold
        and not prompt_visible):
    log.warning("Stuck fallback (%.0fs frozen, prompt not visible) — sending '1'", idle_for)
    log.warning("Stuck screen: %s", _screen_tail(output, 5))
    tmux_send_input(session, "1", send_enter=False)
    runtime.last_change = now
    _event("stuck_fallback", {"idle_for": idle_for})
```

It injected a bare `"1"` (no Enter) when the screen had been frozen
≥120s AND no prompt was visible. Rationale: nudge select-menu dialogs
that had stalled past the cooldown. *(REMOVED in v2 — not driven by
TOML, fragile against unrelated tools, and the same outcome can be
expressed as a TOML rule when needed)*

## Why the simplification

- One source of truth: `[[confirm]]` blocks in each adapter TOML.
  No hidden Python-side semantics, no hidden 60s extension of the
  cooldown, no hardcoded `"1"` injection.
- Eliminates two surprising suppressions (`bare_prompt` and `1-spam`)
  that hid otherwise-correct TOML matches under specific screen states.
- Eliminates the stuck-fallback `"1"` write that could be misrouted
  to a non-Claude/Codex pane (e.g. a shell, a vim instance).
- Tools that genuinely need an extra nudge declare it in TOML the
  same way every other confirmation does.

## What v2 retains

- Cooldown gate (`confirm_cooldown`) — runtime pacing, not semantics.
- `confirm_sleep` — halt duration after a successful fire.
- `send_input` / `send_key` / `event` / `log` / `halt_cycle` action
  protocol — unchanged.
- StateManagerFeature, MailboxFeature placeholder, CronFeature
  placeholder — untouched.
- Health check / auto-exit blocks in `monitor.py` — untouched.
