"""Migration tool: convert cam SQLite data to camc JSON format.

Usage: camc migrate [--db PATH] [--dry-run]

Reads contexts from cam's SQLite database and writes:
  - ~/.cam/machines.json (deduplicated by host+port+user)
  - ~/.cam/contexts.json (referencing machines by name)
  - Merges active agents into ~/.cam/agents.json

Does NOT migrate events (800K+ rows, no practical value).
"""

import json
import os
import sqlite3
import sys


def _find_cam_db():
    """Find the cam SQLite database."""
    # Standard location
    default = os.path.expanduser("~/.local/share/cam/cam.db")
    if os.path.exists(default):
        return default
    # Check env var
    env = os.environ.get("CAM_DATA_DIR")
    if env:
        p = os.path.join(env, "cam.db")
        if os.path.exists(p):
            return p
    return None


def _read_contexts_from_sqlite(db_path):
    """Read all contexts from cam's SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM contexts ORDER BY created_at DESC").fetchall()
    contexts = []
    for row in rows:
        machine_config = json.loads(row["machine_config"])
        contexts.append({
            "id": row["id"],
            "name": row["name"],
            "path": row["path"],
            "machine_config": machine_config,
            "tags": json.loads(row["tags"]) if row["tags"] else [],
        })
    conn.close()
    return contexts


def _read_active_agents_from_sqlite(db_path):
    """Read active (non-terminal) agents from cam's SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM agents WHERE status IN ('running', 'starting', 'pending')"
    ).fetchall()
    agents = []
    for row in rows:
        agents.append(dict(row))
    conn.close()
    return agents


def _machine_key(mc):
    """Generate a unique key for a machine config."""
    return "%s@%s:%s" % (mc.get("user", ""), mc.get("host", "local"), mc.get("port", 22))


def _machine_name_from_host(host):
    """Generate a machine name from a hostname."""
    if not host or host in ("localhost", "127.0.0.1"):
        return "local"
    # Use short hostname: pdx-110.nvidia.com → pdx-110
    short = host.split(".")[0]
    # Sanitize: only alphanumeric, dash, underscore
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in short)


def run_migrate(db_path=None, dry_run=False):
    """Main migration logic. Returns (machines_count, contexts_count, agents_count)."""
    from camc_pkg import MACHINES_FILE, CONTEXTS_FILE
    from camc_pkg.machine_store import MachineStore
    from camc_pkg.context_store import ContextStore
    from camc_pkg.storage import AgentStore

    if db_path is None:
        db_path = _find_cam_db()
    if not db_path or not os.path.exists(db_path):
        print("Error: cam database not found at %s" % (db_path or "~/.local/share/cam/cam.db"))
        print("Use --db PATH to specify location.")
        return 0, 0, 0

    print("Reading from: %s" % db_path)

    # Read SQLite data
    sqlite_contexts = _read_contexts_from_sqlite(db_path)
    print("Found %d contexts in SQLite" % len(sqlite_contexts))

    # Deduplicate machines
    seen_machines = {}  # key → machine dict
    machine_name_map = {}  # key → machine name
    name_counts = {}  # track name collisions

    for ctx in sqlite_contexts:
        mc = ctx["machine_config"]
        mtype = mc.get("type", "local")
        if mtype == "local":
            key = "local"
            machine = {"name": "local", "type": "local"}
        else:
            key = _machine_key(mc)
            base_name = _machine_name_from_host(mc.get("host"))
            # Handle name collisions
            if base_name in name_counts and name_counts[base_name] != key:
                # Different machine with same short name — append port
                base_name = "%s-%s" % (base_name, mc.get("port", 22))
            name_counts.setdefault(base_name, key)

            machine = {"name": base_name, "type": mtype}
            if mc.get("host"):
                machine["host"] = mc["host"]
            if mc.get("user"):
                machine["user"] = mc["user"]
            if mc.get("port"):
                machine["port"] = mc["port"]
            if mc.get("env_setup"):
                machine["env_setup"] = mc["env_setup"]
            if mc.get("key_file"):
                machine["key_file"] = mc["key_file"]

        if key not in seen_machines:
            seen_machines[key] = machine
        machine_name_map[key] = seen_machines[key]["name"]

    machines = list(seen_machines.values())
    print("Deduplicated to %d machines" % len(machines))

    # Build contexts
    contexts = []
    for ctx in sqlite_contexts:
        mc = ctx["machine_config"]
        mtype = mc.get("type", "local")
        if mtype == "local":
            key = "local"
        else:
            key = _machine_key(mc)

        contexts.append({
            "name": ctx["name"],
            "machine": machine_name_map[key],
            "path": ctx["path"],
        })

    print("Converted %d contexts" % len(contexts))

    # Print plan
    print()
    print("Machines:")
    for m in machines:
        if m["type"] == "local":
            print("  local")
        else:
            print("  %s: %s@%s:%s" % (m["name"], m.get("user", ""), m.get("host", ""), m.get("port", 22)))

    print()
    print("Contexts:")
    for c in contexts:
        print("  %s → machine=%s, path=%s" % (c["name"], c["machine"], c["path"]))

    if dry_run:
        print()
        print("Dry run — no files written.")
        return len(machines), len(contexts), 0

    # Write machines.json
    m_store = MachineStore()
    for m in machines:
        m_store.save(m)
    print()
    print("Wrote %d machines to %s" % (len(machines), MACHINES_FILE))

    # Write contexts.json
    c_store = ContextStore()
    for c in contexts:
        c_store.save(c)
    print("Wrote %d contexts to %s" % (len(contexts), CONTEXTS_FILE))

    # Merge active agents
    agents_merged = 0
    try:
        sqlite_agents = _read_active_agents_from_sqlite(db_path)
        if sqlite_agents:
            a_store = AgentStore()
            existing_ids = {a["id"] for a in a_store.list()}
            for sa in sqlite_agents:
                aid = sa.get("id", "")
                if aid and aid not in existing_ids:
                    # Minimal agent record — these are cam-format agents
                    a_store.save({
                        "id": aid,
                        "task": {"name": "", "tool": "claude", "prompt": "", "auto_confirm": True, "auto_exit": False},
                        "context_name": sa.get("context_name", ""),
                        "context_path": sa.get("context_path", ""),
                        "transport_type": sa.get("transport_type", "local"),
                        "status": sa.get("status", "running"),
                        "state": sa.get("state", "initializing"),
                        "tmux_session": sa.get("tmux_session", ""),
                        "tmux_socket": sa.get("tmux_socket", ""),
                        "pid": sa.get("pid"),
                        "hostname": "",
                        "started_at": sa.get("started_at"),
                        "completed_at": sa.get("completed_at"),
                        "exit_reason": sa.get("exit_reason"),
                        "retry_count": 0, "cost_estimate": None, "files_changed": [],
                    })
                    agents_merged += 1
            if agents_merged:
                print("Merged %d active agents" % agents_merged)
    except Exception as e:
        print("Warning: could not merge agents: %s" % e)

    print()
    print("Migration complete!")
    print("You can now verify with: camc machine list && camc context list")
    print("When satisfied, you can remove: rm -rf ~/.local/share/cam/")
    return len(machines), len(contexts), agents_merged
