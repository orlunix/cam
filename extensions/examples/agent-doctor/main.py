#!/usr/bin/env python3
"""agent-doctor remote collector — SPEC v2 type-B tool.

Contract: python3 main.py <method> '<json>' → one JSON object on stdout.
python3.6 stdlib only.

Methods:
  collect {tool}           — probe the harness' well-known paths and
                             return structured facts (no judgement;
                             rules live in the view).
  install_adapter {tool, config} — write a user adapter JSON to
                             ~/.cam/extensions/agent-doctor/adapters/<tool>.json
  list_adapters            — built-in + user adapter names.
"""
import json
import os
import sys
import time

# Built-in adapters: where each harness keeps its home config.
# Paths are ~/-relative; "ws" values are workspace-relative (checked by
# the view via agents.workspaceRead, not here).
ADAPTERS = {
    "claude": {
        "workspace_system_prompt": ["CLAUDE.md", "AGENTS.md"],
        "home_paths": {
            "global_memory": "~/.claude/CLAUDE.md",
            "settings":      "~/.claude/settings.json",
            "skills_dir":    "~/.claude/skills",
            "sessions_dir":  "~/.claude/projects",
            "mcp_config":    "~/.claude.json",
        },
    },
    "codex": {
        "workspace_system_prompt": ["AGENTS.md"],
        "home_paths": {
            "settings":      "~/.codex/config.toml",
            "sessions_dir":  "~/.codex/sessions",
            "skills_dir":    "~/.codex/skills",
            "memories":      "~/.codex/memories",
        },
    },
}

ADAPTER_DIR = os.path.expanduser("~/.cam/extensions/agent-doctor/adapters")


def _user_adapter(tool):
    p = os.path.join(ADAPTER_DIR, "%s.json" % tool)
    if not os.path.isfile(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _stat(p):
    p = os.path.expanduser(p)
    if not os.path.exists(p):
        return {"exists": False}
    st = os.stat(p)
    out = {"exists": True, "is_dir": os.path.isdir(p),
           "size": st.st_size, "mtime": int(st.st_mtime)}
    if os.path.isdir(p):
        try:
            out["entries"] = len(os.listdir(p))
        except Exception:
            out["entries"] = -1
    return out


def collect(args):
    tool = str(args.get("tool") or "").strip().lower()
    if not tool:
        return {"error": "missing_tool", "detail": "pass {tool: 'claude'|'codex'|...}"}
    ad = _user_adapter(tool) or ADAPTERS.get(tool)
    if not ad:
        return {"error": "no_adapter", "detail": "no adapter for tool '%s'" % tool,
                "known": sorted(set(list(ADAPTERS) + (list_adapters({})["user"])))}
    report = {"tool": tool, "adapter": "user" if _user_adapter(tool) else "builtin",
              "home_paths": {}, "skills": [], "sessions": {}, "mcp_servers": []}
    for name, rel in (ad.get("home_paths") or {}).items():
        report["home_paths"][name] = _stat(rel)

    skills_dir = (ad.get("home_paths") or {}).get("skills_dir")
    if skills_dir:
        sd = os.path.expanduser(skills_dir)
        if os.path.isdir(sd):
            try:
                report["skills"] = sorted(os.listdir(sd))[:50]
            except Exception:
                pass

    sessions_dir = (ad.get("home_paths") or {}).get("sessions_dir")
    if sessions_dir:
        p = os.path.expanduser(sessions_dir)
        if os.path.isdir(p):
            count = 0
            newest = 0
            try:
                for root, _dirs, files in os.walk(p):
                    count += len(files)
                    for f in files:
                        try:
                            mt = os.path.getmtime(os.path.join(root, f))
                            newest = max(newest, int(mt))
                        except Exception:
                            pass
            except Exception:
                pass
            report["sessions"] = {"count": count, "newest_mtime": newest}

    mcp_path = (ad.get("home_paths") or {}).get("mcp_config")
    if mcp_path:
        p = os.path.expanduser(mcp_path)
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                servers = cfg.get("mcpServers") or {}
                report["mcp_servers"] = sorted(servers.keys())
            except Exception as e:
                report["mcp_error"] = str(e)

    report["collected_at"] = int(time.time())
    return report


def install_adapter(args):
    tool = str(args.get("tool") or "").strip().lower()
    config = args.get("config")
    if not tool or not isinstance(config, dict) or not config.get("home_paths"):
        return {"error": "invalid_adapter", "detail": "need {tool, config:{home_paths:{...}}}"}
    try:
        os.makedirs(ADAPTER_DIR, exist_ok=True)
    except Exception:
        # python3.6 has exist_ok; keep for clarity
        if not os.path.isdir(ADAPTER_DIR):
            os.makedirs(ADAPTER_DIR)
    p = os.path.join(ADAPTER_DIR, "%s.json" % tool)
    with open(p, "w") as f:
        json.dump(config, f, indent=1)
    return {"installed": tool, "path": p}


def list_adapters(_args):
    user = []
    if os.path.isdir(ADAPTER_DIR):
        try:
            user = sorted(f[:-5] for f in os.listdir(ADAPTER_DIR) if f.endswith(".json"))
        except Exception:
            pass
    return {"builtin": sorted(ADAPTERS), "user": user}


METHODS = {"collect": collect, "install_adapter": install_adapter, "list_adapters": list_adapters}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "missing_method", "detail": "methods: " + ", ".join(sorted(METHODS))}))
        return 2
    fn = METHODS.get(sys.argv[1])
    if not fn:
        print(json.dumps({"error": "unknown_method", "detail": "methods: " + ", ".join(sorted(METHODS))}))
        return 2
    try:
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except Exception as e:
        print(json.dumps({"error": "bad_args", "detail": str(e)}))
        return 2
    try:
        print(json.dumps(fn(args)))
        return 0
    except Exception as e:
        print(json.dumps({"error": "collector_failed", "detail": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
