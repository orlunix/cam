"""Feature pipeline for the per-agent monitor (behavior-preserving v1).

Replaces the earlier ``monitor_steps`` 4-step pipeline with a
coarser-grained, 3-phase feature pipeline:

    Phase A (before_confirm) — busy/done signals.
    Phase B (confirm)        — auto-confirm rules, may halt the cycle.
    Phase C (after_confirm)  — state detect, output-change reset, idle detect.

Registered features:

    * StateManagerFeature (order 10) — owns the state-management work that
      used to live across ScreenStateStep + StateDetectStep +
      OutputChangeStep + IdleDetectStep. Implements:
          before_confirm(): _update_screen_signals()
          after_confirm():  _detect_state_change()
                            _update_output_change()
                            _detect_idle()
      The phase split is what preserves the legacy ordering exactly: a
      halt_cycle from AutoConfirmationFeature.confirm() skips the
      after_confirm phase entirely, so the output-change reset and the
      idle detector don't run on the halted cycle — matching the legacy
      ``time.sleep(...); continue`` semantics.

    * AutoConfirmationFeature (order 20) — TOML-only auto-confirm.
      Cooldown-gated; the ONLY semantic decision is the adapter's
      `[[confirm]]` rule set. Emits a halt_cycle action on a successful
      confirm. The legacy bare_prompt skip and 1-spam guard were
      removed on 2026-06-10 (see docs/legacy/monitor-auto-confirm-v1.md).

    * MailboxFeature (order 30) — PLACEHOLDER, disabled by default,
      no-op. Reserved for a future slice that wires the camc msg
      mailbox into the monitor. Does NOT read / write the ledger, does
      NOT inject into tmux, does NOT call store.

    * CronFeature (order 40) — PLACEHOLDER, disabled by default,
      no-op. Reserved for a future slice that wires camc cron checks
      into the monitor. Does NOT consult jobs.json, does NOT spawn
      jobs, does NOT call store.

Action protocol is unchanged from the step-pipeline era:

    {"kind": "log",          "level": "debug|info|warning|error", "msg": str}
    {"kind": "send_input",   "text": str, "send_enter": bool}
    {"kind": "send_key",     "key": str}
    {"kind": "store_update", "fields": {<field>: <value>, ...}}
    {"kind": "event",        "name": str, "detail": dict|None}
    {"kind": "halt_cycle",   "sleep": float}

Python 3.6+, stdlib only. No dataclasses, no f-strings.
"""

# ---------------------------------------------------------------------------
# Snapshot + runtime (plain classes; no dataclasses for 3.6 compat)
# ---------------------------------------------------------------------------

class MonitorSnapshot(object):
    """Per-tick read-only payload the driver builds before phase A. See
    the per-field comments below; identical contract to the v0 pipeline
    so legacy tests reading these fields keep working."""

    __slots__ = (
        "output", "hash", "prev_hash", "changed", "now", "cycle",
        "prompt_visible", "screen_busy", "screen_done", "bare_prompt",
        "tail_lines", "idle_for",
        # 2026-06-10: hash0/hash1 normalized-content fingerprints.
        # hash0 is the canonical idle signal (mirrors `hash`); hash1
        # is hash0 with ASCII digits removed — exposed so a feature
        # or test can distinguish "numbers-only churn" from real
        # screen content change. ``idle_for_hash1`` is the parallel
        # seconds-stable counter for hash1.
        "hash0", "hash1", "idle_for_hash1",
    )

    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))


class MonitorRuntime(object):
    """Mutable monitor state carried across ticks.

    Features may mutate fields directly (no setter ceremony). The
    feature_state dict is a per-feature namespace so a feature can
    persist arbitrary local data without colliding with other
    features. (Compat shim: a `step_state` alias points at the same
    dict so any v0 step-era test that touches ``runtime.step_state``
    still works.)"""

    def __init__(self, agent_id, config, now=0.0):
        self.agent_id = agent_id
        self.config = config
        self.prev_hash = ""           # prev hash0 (kept as `prev_hash` for back-compat)
        self.prev_hash1 = ""
        self.prev_output = ""
        self.last_change = now        # hash0 last-changed timestamp
        self.last_change_hash1 = now  # hash1 last-changed timestamp (diagnostic)
        self.last_health = now
        self.last_confirm = 0.0
        self.last_confirm_hash = ""   # screen hash at last fire (dedup)
        self.last_confirm_response = ""  # response sent at last fire (input-box guard)
        self.current_state = None
        self.has_worked = False
        # 2026-06-10 addendum: "initialize" is a one-shot pre-busy
        # state. Once the screen has been busy at least once, this
        # latches True and the state detector refuses to fall back
        # to "initializing". The monitor only starts AFTER the first
        # prompt has been delivered (cmd_run sends the prompt before
        # spawning the monitor subprocess), so screen-busy is the
        # only remaining gate to track in the runtime itself.
        self.left_initializing = False
        self.idle_confirmed = False
        self.cycle = 0
        self.feature_state = {}
        # Back-compat alias for any leftover v0 callers / tests.
        self.step_state = self.feature_state


# ---------------------------------------------------------------------------
# Registry + base feature
# ---------------------------------------------------------------------------

_REGISTRY = []   # list of feature classes, in registration order


def register_feature(feature_cls):
    """Class decorator (or plain call) that adds a feature class to the
    registry. ``build_features()`` later instantiates these in ``order``
    ascending."""
    if feature_cls not in _REGISTRY:
        _REGISTRY.append(feature_cls)
    return feature_cls


def registered_features():
    """Read-only view of the registry. Tests use this to assert order."""
    return tuple(_REGISTRY)


def reset_registry():
    """Test-only: clear the registry so a focused test can install its
    own feature set without leaking globals."""
    del _REGISTRY[:]


def build_features(enabled=None):
    """Instantiate registered features, optionally toggling by name.

    ``enabled``:
      None              — every feature's own ``enabled`` attr wins.
      dict[name->bool]  — explicit overrides; missing names fall back
                          to the feature's default ``enabled``.
      iterable of names — only those names are kept; everything else
                          treated as disabled.

    Returns a list sorted by ``feature.order`` ascending. DISABLED
    features are still INCLUDED in the returned list so the driver
    (and tests) can introspect them; the driver checks ``feature.enabled``
    before invoking any phase hook."""
    instances = []
    if isinstance(enabled, dict):
        toggles = dict(enabled)
        for cls in _REGISTRY:
            inst = cls()
            if inst.name in toggles:
                inst.enabled = bool(toggles[inst.name])
            instances.append(inst)
    elif enabled is None:
        for cls in _REGISTRY:
            inst = cls()
            instances.append(inst)
    else:
        allowed = set(enabled)
        for cls in _REGISTRY:
            inst = cls()
            inst.enabled = inst.name in allowed
            instances.append(inst)
    instances.sort(key=lambda f: f.order)
    return instances


class MonitorFeature(object):
    """Base class for a monitor pipeline feature.

    Subclasses set ``name`` (unique within the registry), ``order``
    (lower runs first within each phase), and may override ``enabled``
    (default True). Override ``init_state`` if the feature needs a
    non-empty per-runtime state dict.

    Phase hooks — subclasses override only the ones they need. Each
    returns a list of action dicts (or empty)."""

    name = ""
    order = 0
    enabled = True

    def init_state(self):
        return {}

    def get_state(self, runtime):
        st = runtime.feature_state.get(self.name)
        if st is None:
            st = self.init_state()
            runtime.feature_state[self.name] = st
        return st

    def before_confirm(self, snap, runtime):
        return []

    def confirm(self, snap, runtime):
        return []

    def after_confirm(self, snap, runtime):
        return []


# ---------------------------------------------------------------------------
# Small helper (private to this module so monitor_features stays self-contained)
# ---------------------------------------------------------------------------

def _screen_tail_str(output, n=3):
    lines = [l.strip() for l in output.rstrip("\n").split("\n") if l.strip()]
    return " | ".join(lines[-n:]) if lines else "(empty)"


# ===========================================================================
# StateManagerFeature — owns busy/done + state detect + output change + idle
# ===========================================================================

@register_feature
class StateManagerFeature(MonitorFeature):
    """Phase A: busy/done signals (legacy step 2b).
    Phase C: detect_state (3) → output_change reset (5) → idle_detect (6).

    Auto-confirm sits in between (phase B, owned by
    AutoConfirmationFeature). A halt_cycle from phase B skips phase C
    for this cycle, which is exactly what the legacy ``time.sleep(...);
    continue`` did after step 3."""

    name = "state_manager"
    order = 10

    def before_confirm(self, snap, runtime):
        return self._update_screen_signals(snap, runtime)

    def after_confirm(self, snap, runtime):
        actions = []
        actions.extend(self._detect_state_change(snap, runtime))
        actions.extend(self._update_output_change(snap, runtime))
        actions.extend(self._detect_idle(snap, runtime))
        return actions

    # -- private phase implementations -------------------------------------

    def _update_screen_signals(self, snap, runtime):
        """Legacy step 2b. busy_pattern → reset last_change + mark
        has_worked + clear idle_confirmed + latch left_initializing;
        done_pattern → mark has_worked only."""
        actions = []
        if snap.screen_busy:
            if not runtime.has_worked:
                runtime.has_worked = True
                actions.append({"kind": "log", "level": "info",
                                "msg": "Busy signal detected, has_worked=True"})
            if not runtime.left_initializing:
                runtime.left_initializing = True
                actions.append({"kind": "log", "level": "info",
                                "msg": "First busy seen — left initializing permanently"})
            runtime.last_change = snap.now
            runtime.idle_confirmed = False
        if snap.screen_done and not runtime.has_worked:
            runtime.has_worked = True
            actions.append({"kind": "log", "level": "info",
                            "msg": "Done signal detected, has_worked=True"})
        return actions

    def _detect_state_change(self, snap, runtime):
        """detect_state(output, config) → emit state_change event +
        store_update(state=ns) + mark has_worked unless transitioning
        to 'initializing'.

        2026-06-10 boundary: once ``runtime.left_initializing`` has
        latched True (first busy seen), refuse to fall back to
        'initializing'. The first prompt is always delivered before
        the monitor subprocess starts, so the busy-seen latch is the
        only remaining gate for the boundary."""
        from camc_pkg.detection import detect_state
        actions = []
        ns = detect_state(snap.output, runtime.config)
        if ns == "initializing" and runtime.left_initializing:
            return actions
        if ns and ns != runtime.current_state:
            if ns != "initializing":
                runtime.has_worked = True
            actions.append({"kind": "log", "level": "info",
                            "msg": "State: %s -> %s" % (runtime.current_state, ns)})
            actions.append({"kind": "event", "name": "state_change",
                            "detail": {"from": runtime.current_state, "to": ns}})
            runtime.current_state = ns
            actions.append({"kind": "store_update", "fields": {"state": ns}})
        return actions

    def _update_output_change(self, snap, runtime):
        """Legacy step 5. snap.changed → reset last_change + clear
        idle_confirmed + emit Output-changed debug log."""
        actions = []
        if snap.changed:
            actions.append({"kind": "log", "level": "debug",
                            "msg": "[%d] Output changed (hash %s -> %s), reset idle timer"
                                   % (snap.cycle, snap.prev_hash, snap.hash)})
            runtime.last_change = snap.now
            runtime.idle_confirmed = False
        return actions

    def _detect_idle(self, snap, runtime):
        """Idle = hash0 (normalized printable ASCII + CJK) stable for
        ``cfg.idle_stable_seconds`` (default 60s — preserves the prior
        hardcoded threshold). Fast-track 5s when ``screen_done`` AND
        ``bare_prompt`` are both set. Requires ``has_worked`` AND not
        ``idle_confirmed`` AND ``snap.idle_for >= idle_threshold`` AND
        ``prompt_visible``.

        ``snap.idle_for`` is the hash0 stability window (driven by
        runtime.last_change, which is only advanced when the
        normalized-content hash changes). Numeric-only churn (timer,
        progress, spinner) changes hash0 every tick (because the
        digits ARE part of hash0) while hash1 stays stable (digits
        are stripped from hash1). That combination means the agent
        is still working: hash0 keeps resetting the idle timer, so
        idle is NOT confirmed — exactly what the addendum asked for.
        """
        actions = []
        idle_stable = getattr(runtime.config, "idle_stable_seconds", 60.0)
        idle_threshold = 5 if (snap.screen_done and snap.bare_prompt) else idle_stable
        if runtime.has_worked and not runtime.idle_confirmed and snap.idle_for >= idle_threshold:
            if snap.prompt_visible:
                runtime.idle_confirmed = True
                if idle_threshold < idle_stable:
                    actions.append({"kind": "log", "level": "info",
                                    "msg": "Idle confirmed (done signal + prompt, %.0fs stable)"
                                           % snap.idle_for})
                else:
                    actions.append({"kind": "log", "level": "info",
                                    "msg": "Idle confirmed (screen stable %.0fs, prompt visible)"
                                           % snap.idle_for})
                actions.append({"kind": "event", "name": "idle_confirmed",
                                "detail": {"idle_for": snap.idle_for}})
                actions.append({"kind": "store_update", "fields": {"state": "idle"}})
            else:
                actions.append({"kind": "log", "level": "debug",
                                "msg": "[%d] Idle candidate (%.0fs stable) but prompt not visible"
                                       % (snap.cycle, snap.idle_for)})
        return actions


# ===========================================================================
# AutoConfirmationFeature — owns the cooldown-gated dialog auto-confirm
# ===========================================================================

@register_feature
class AutoConfirmationFeature(MonitorFeature):
    """Phase B only — TOML-only auto-confirm.

    The ONLY decision for whether to confirm and what to send is the
    adapter's TOML ``[[confirm]]`` rule set, consulted through
    ``should_auto_confirm(snap.output, cfg)``. No Python-side semantic
    matching, no ``bare_prompt`` skip, no ``1-spam`` guard. See
    ``docs/legacy/monitor-auto-confirm-v1.md`` for the archived v1
    behavior.

    Retained runtime mechanics (config-driven, not pattern matching):
      * cooldown gate via ``runtime.last_confirm`` vs
        ``config.confirm_cooldown``;
      * successful fire: send ``response`` (or ``Enter`` when response
        is empty + ``send_enter`` is true), update ``last_confirm`` /
        ``last_change``, clear ``idle_confirmed``, set
        ``has_worked=True``, emit ``auto_confirm`` event, halt the
        cycle with ``sleep=cfg.confirm_sleep``.
    """

    name = "auto_confirm"
    order = 20

    def confirm(self, snap, runtime):
        from camc_pkg.detection import should_auto_confirm, input_residue_count
        actions = []
        cfg = runtime.config
        confirm_cd = snap.now - runtime.last_confirm
        if confirm_cd < cfg.confirm_cooldown:
            actions.append({"kind": "log", "level": "debug",
                            "msg": "[%d] Confirm cooldown (%.1fs remaining)"
                                   % (snap.cycle, cfg.confirm_cooldown - confirm_cd)})
            return actions
        # spam-fix: if our last_response chars leaked into the input
        # box, send a backspace to clean them up one per cycle. The
        # rest of the auto-confirm flow stays suppressed by
        # has_input_cursor (condition 2) until the input is clean.
        residue = input_residue_count(snap.output, runtime.last_confirm_response)
        if residue > 0:
            actions.append({"kind": "log", "level": "info",
                            "msg": "[%d] Backspace to clean input residue (%d chars)"
                                   % (snap.cycle, residue)})
            actions.append({"kind": "send_key", "key": "BSpace"})
            actions.append({"kind": "halt_cycle", "sleep": cfg.confirm_sleep})
            return actions
        confirm = should_auto_confirm(snap.output, cfg,
                                       last_response=runtime.last_confirm_response,
                                       prev_output=runtime.prev_output or "")
        if not confirm:
            return actions
        response, send_enter, pat_str, matched = confirm
        # Dedup: skip if the digit-stripped screen hash (hash1)
        # hasn't changed since the last fire. Why hash1 not hash0:
        # hash0 includes ASCII digits, so codex's "Working 5m 31s"
        # → "32s" → "33s" timer changes hash0 every second and
        # defeats dedup. hash1 strips ASCII digits, so timer ticks
        # don't flip it — but a NEW dialog with different surrounding
        # text (e.g. a different `$ command` line) does.
        if snap.hash1 and snap.hash1 == runtime.last_confirm_hash:
            actions.append({"kind": "log", "level": "debug",
                            "msg": "[%d] Confirm dedup: hash1 unchanged since last fire"
                                   % snap.cycle})
            return actions
        actions.append({"kind": "log", "level": "info",
                        "msg": "Auto-confirm: pattern=%r matched=%r -> %r (enter=%s)"
                               % (pat_str, matched, response, send_enter)})
        actions.append({"kind": "log", "level": "debug",
                        "msg": "[%d] Confirm screen: %s"
                               % (snap.cycle, _screen_tail_str(snap.output, 5))})
        if response:
            actions.append({"kind": "send_input",
                            "text": response, "send_enter": send_enter})
        elif send_enter:
            actions.append({"kind": "send_key", "key": "Enter"})
        runtime.last_confirm = snap.now
        runtime.last_confirm_hash = snap.hash1
        runtime.last_confirm_response = response
        runtime.last_change = snap.now
        runtime.idle_confirmed = False
        runtime.has_worked = True
        actions.append({"kind": "event", "name": "auto_confirm",
                        "detail": {"pattern": pat_str, "response": response}})
        actions.append({"kind": "halt_cycle", "sleep": cfg.confirm_sleep})
        return actions


# ===========================================================================
# Placeholders — registered but disabled. Reserved for future slices.
# ===========================================================================

@register_feature
class MailboxFeature(MonitorFeature):
    """PLACEHOLDER, disabled by default. A future slice will wire the
    camc msg mailbox here so the monitor can react to incoming
    inter-agent messages. Until then: registered for discoverability +
    ordering, but ``enabled=False`` and every phase hook is a no-op.

    This deliberately does NOT read or write the messages.jsonl ledger,
    does NOT inject into the tmux pane, and does NOT touch the agent
    store. The driver skips disabled features entirely; this class
    exists purely as a public contract."""

    name = "mailbox"
    order = 30
    enabled = False


@register_feature
class CronFeature(MonitorFeature):
    """PLACEHOLDER, disabled by default. A future slice will wire camc
    cron checks here so per-agent recurring jobs can land inside the
    monitor loop. Until then: registered for discoverability +
    ordering, but ``enabled=False`` and every phase hook is a no-op.

    This deliberately does NOT consult jobs.json, does NOT spawn jobs,
    and does NOT call the store. The driver skips disabled features
    entirely; this class exists purely as a public contract."""

    name = "cron"
    order = 40
    enabled = False
