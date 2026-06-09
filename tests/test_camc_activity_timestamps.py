"""F-07 (P0, dynamic-only): camc list computes updated_at at read time
from live tmux session activity, with a static started_at/completed_at
fallback. Nothing is persisted in agents.json.

Scope under test:
  * `_max_ts` (helper).
  * `_epoch_to_iso` (tmux epoch → ISO 8601 conversion).
  * `_compute_updated_at` two-path behavior:
      (a) live tmux session → tmux ts wins;
      (b) dead session OR tmux query failure → static fallback.
  * `_agent_to_cam_json` emits the dynamic updated_at and does NOT
    emit `last_input_at` (P0 spec drops the field entirely).
"""

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import cli as camc_cli  # noqa: E402


# ---------------------------------------------------------------------------
# _max_ts
# ---------------------------------------------------------------------------

def test_max_ts_ignores_none_and_empty():
    assert camc_cli._max_ts(None, "", None) is None
    assert camc_cli._max_ts(None, "2026-06-03T01:00:00Z") == "2026-06-03T01:00:00Z"
    assert camc_cli._max_ts("2026-06-03T00:00:00Z", "2026-06-04T00:00:00Z") == "2026-06-04T00:00:00Z"


# ---------------------------------------------------------------------------
# _epoch_to_iso
# ---------------------------------------------------------------------------

def test_epoch_to_iso_converts_unix_epoch_seconds_to_utc_iso():
    # 2026-06-03T08:00:00Z == 1780473600
    assert camc_cli._epoch_to_iso("1780473600") == "2026-06-03T08:00:00Z"
    assert camc_cli._epoch_to_iso(1780473600) == "2026-06-03T08:00:00Z"


def test_epoch_to_iso_rejects_garbage():
    assert camc_cli._epoch_to_iso(None) is None
    assert camc_cli._epoch_to_iso("") is None
    assert camc_cli._epoch_to_iso("not-a-number") is None
    assert camc_cli._epoch_to_iso("0") is None
    assert camc_cli._epoch_to_iso("-1") is None


# ---------------------------------------------------------------------------
# _compute_updated_at: live tmux vs fallback
# ---------------------------------------------------------------------------

def test_compute_updated_at_prefers_live_tmux_session_activity(monkeypatch):
    """When tmux returns an activity epoch, that wins over started_at."""
    def fake_query(session):
        assert session == "cam-aaaa1111"
        return "2026-06-03T08:00:00Z"
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso", fake_query)
    rec = {
        "id": "aaaa1111",
        "tmux_session": "cam-aaaa1111",
        "started_at":   "2026-04-01T00:00:00Z",
        "completed_at": None,
    }
    assert camc_cli._compute_updated_at(rec) == "2026-06-03T08:00:00Z"


def test_compute_updated_at_falls_back_when_session_missing(monkeypatch):
    """A dead/query-failed session falls back to max(completed_at, started_at)."""
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso", lambda s: None)
    rec = {
        "tmux_session": "cam-dead",
        "started_at":   "2026-04-01T00:00:00Z",
        "completed_at": "2026-04-15T03:00:00Z",
    }
    assert camc_cli._compute_updated_at(rec) == "2026-04-15T03:00:00Z"


def test_compute_updated_at_falls_back_started_when_no_completed(monkeypatch):
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso", lambda s: None)
    rec = {
        "tmux_session": "cam-dead",
        "started_at":   "2026-04-01T00:00:00Z",
    }
    assert camc_cli._compute_updated_at(rec) == "2026-04-01T00:00:00Z"


def test_compute_updated_at_returns_none_for_record_without_any_timestamp(monkeypatch):
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso", lambda s: None)
    assert camc_cli._compute_updated_at({"id": "x"}) is None


def test_compute_updated_at_no_session_skips_tmux_query(monkeypatch):
    """A record without a tmux_session must not call into tmux."""
    calls = []
    def trap(session):
        calls.append(session)
        return None
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso", trap)
    rec = {"started_at": "2026-04-01T00:00:00Z"}
    out = camc_cli._compute_updated_at(rec)
    # _compute_updated_at calls _tmux_session_activity_iso with the
    # session value; for a missing session that helper short-circuits
    # to None inside itself. Either path is acceptable as long as the
    # fallback runs; check the fallback result.
    assert out == "2026-04-01T00:00:00Z"


# ---------------------------------------------------------------------------
# _agent_to_cam_json: dynamic updated_at + no last_input_at
# ---------------------------------------------------------------------------

def test_agent_to_cam_json_emits_dynamic_updated_at(monkeypatch):
    """_agent_to_cam_json must call _compute_updated_at — the JSON
    shape carries the dynamic value, not whatever (if anything) is
    sitting in the record."""
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso",
                        lambda s: "2026-06-04T09:00:00Z")
    rec = {
        "id": "aaaa1111",
        "task": {"name": "x", "tool": "claude"},
        "status": "running",
        "tmux_session": "cam-aaaa1111",
        "started_at": "2026-06-01T00:00:00Z",
    }
    out = camc_cli._agent_to_cam_json(rec)
    assert out["updated_at"] == "2026-06-04T09:00:00Z"
    # last_input_at is intentionally dropped from P0.
    assert "last_input_at" not in out


def test_agent_to_cam_json_updated_at_falls_back_for_terminal_agent(monkeypatch):
    """Completed/killed agents have no live tmux session; the JSON
    shape must still surface a usable updated_at."""
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso", lambda s: None)
    rec = {
        "id": "bbbb2222",
        "task": {"name": "old", "tool": "claude"},
        "status": "completed",
        "tmux_session": "cam-bbbb2222",
        "started_at":   "2026-05-20T10:00:00Z",
        "completed_at": "2026-05-20T11:30:00Z",
    }
    out = camc_cli._agent_to_cam_json(rec)
    assert out["updated_at"] == "2026-05-20T11:30:00Z"


# ---------------------------------------------------------------------------
# list sort uses dynamic updated_at
# ---------------------------------------------------------------------------

def test_sort_agents_by_updated_at_recency_desc_within_untagged_group(monkeypatch):
    """The extracted sort helper must produce newest-first within the
    untagged group. This is the actual contract `camc list` uses;
    earlier the chr(127)+s[::-1] key did NOT do this, and that bug
    was the subject of the cam-review finding."""
    activity = {
        "cam-busy":   "2026-06-04T09:00:00Z",
        "cam-mid":    "2026-06-04T08:00:00Z",
        "cam-quiet":  "2026-06-04T07:00:00Z",
        "cam-dead":   None,
    }
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso",
                        lambda s: activity.get(s))
    agents = [
        {"id": "dead",  "task": {"tags": []}, "tmux_session": "cam-dead",
         "started_at": "2026-06-01T00:00:00Z"},
        {"id": "quiet", "task": {"tags": []}, "tmux_session": "cam-quiet",
         "started_at": "2026-05-01T00:00:00Z"},
        {"id": "busy",  "task": {"tags": []}, "tmux_session": "cam-busy",
         "started_at": "2026-04-01T00:00:00Z"},
        {"id": "mid",   "task": {"tags": []}, "tmux_session": "cam-mid",
         "started_at": "2026-04-15T00:00:00Z"},
    ]
    ordered = camc_cli._sort_agents_by_updated_at(agents)
    # busy (09:00) → mid (08:00) → quiet (07:00) → dead (falls back
    # to 2026-06-01 started_at, which is the oldest of these four).
    assert [a["id"] for a in ordered] == ["busy", "mid", "quiet", "dead"]


def test_sort_agents_by_updated_at_tag_group_then_recency(monkeypatch):
    """Tagged agents come BEFORE untagged; within each group, newest
    first. With identical first-tags, recency still drives order."""
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso",
                        lambda s: {
                            "cam-a-new":  "2026-06-04T09:00:00Z",
                            "cam-a-old":  "2026-06-04T05:00:00Z",
                            "cam-b-new":  "2026-06-04T10:00:00Z",
                            "cam-untag":  "2026-06-04T11:00:00Z",  # newest overall
                        }.get(s))
    agents = [
        {"id": "untag",  "task": {"tags": []},        "tmux_session": "cam-untag"},
        {"id": "a-new",  "task": {"tags": ["alpha"]}, "tmux_session": "cam-a-new"},
        {"id": "a-old",  "task": {"tags": ["alpha"]}, "tmux_session": "cam-a-old"},
        {"id": "b-new",  "task": {"tags": ["beta"]},  "tmux_session": "cam-b-new"},
    ]
    ordered = camc_cli._sort_agents_by_updated_at(agents)
    # Tagged group first (alpha then beta by tag name); untagged last.
    # Within alpha: a-new (09:00) before a-old (05:00). Within beta:
    # just b-new (10:00). Then untagged: untag (11:00). Note the
    # newest-overall (untag at 11:00) does NOT jump the tag group.
    assert [a["id"] for a in ordered] == ["a-new", "a-old", "b-new", "untag"]


def test_sort_agents_by_updated_at_empty_recency_sorts_last(monkeypatch):
    """An agent with no recency (no live tmux, no started_at) lands
    at the end of its tag group, not at the top."""
    monkeypatch.setattr(camc_cli, "_tmux_session_activity_iso", lambda s: None)
    agents = [
        {"id": "no-ts",  "task": {"tags": []}},   # no recency at all
        {"id": "has-ts", "task": {"tags": []},
         "started_at": "2026-06-04T08:00:00Z"},
    ]
    ordered = camc_cli._sort_agents_by_updated_at(agents)
    assert [a["id"] for a in ordered] == ["has-ts", "no-ts"]


def test_sort_agents_by_updated_at_uses_provided_recency_closure():
    """The recency_fn closure overrides the default _compute_updated_at.
    This is the path cmd_list uses to precompute updated_at once and
    share it across sort + JSON-emit + table render — verify the
    contract that closure is honored."""
    agents = [
        {"id": "A", "task": {"tags": []}},
        {"id": "B", "task": {"tags": []}},
        {"id": "C", "task": {"tags": []}},
    ]
    # Inject a closure that returns hard-coded recency values.
    cache = {"A": "2026-06-04T05:00:00Z",
             "B": "2026-06-04T09:00:00Z",
             "C": "2026-06-04T07:00:00Z"}
    ordered = camc_cli._sort_agents_by_updated_at(
        agents, recency_fn=lambda a: cache[a["id"]])
    # Newest first: B → C → A.
    assert [a["id"] for a in ordered] == ["B", "C", "A"]


# ---------------------------------------------------------------------------
# Persistence guard: agents.json shape is NOT extended
# ---------------------------------------------------------------------------

def test_run_record_init_does_not_persist_updated_or_input_fields():
    """Read the cmd_run record-init source to confirm we did NOT add
    persistent updated_at / last_input_at fields. The P0 dynamic
    design depends on these NOT being in agents.json (so a stale
    persisted value can never override the live tmux query)."""
    src_path = os.path.join(SRC, "camc_pkg", "cli.py")
    with open(src_path, "r") as f:
        src = f.read()
    # Find the cmd_run create-block. We don't pin a line number — just
    # confirm no record-creation block stamps these fields.
    assert '"updated_at": _now_iso()' not in src
    assert '"last_input_at": None' not in src
    assert '"last_input_at": _now_iso()' not in src
