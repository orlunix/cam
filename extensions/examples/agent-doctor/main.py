#!/usr/bin/env python3
"""agent-doctor remote collector — SPEC v2 type-B tool.

Contract: python3 main.py <method> '<json>' → one JSON object on stdout.
python3.6 stdlib only.

Methods:
  collect {tool}           — probe the harness' well-known paths and
                             return structured facts (no judgement;
                             rules live in the view).
  review {tool}            — same adapter paths, but returns CONTENT for
                             the review UI: global memory, settings
                             (secret-redacted), skills' SKILL.md, MCP
                             config, recent sessions. All paths come from
                             the adapter — never from the caller.
  install_adapter {tool, config} — write a user adapter JSON to
                             ~/.cam/extensions/agent-doctor/adapters/<tool>.json
  list_adapters            — built-in + user adapter names.
"""
import json
import os
import re
import subprocess
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


# ── review: content-level probe for the review UI ──
# Size caps keep the payload sane over slow links; totals are bounded by
# (SKILL_CAP * MAX_SKILLS) + MEM_CAP + SETTINGS_CAP + ...  ≈ 220KB worst
# case, typically far less.
MEM_CAP = 24 * 1024
SETTINGS_CAP = 16 * 1024
SKILL_CAP = 8 * 1024
MAX_SKILLS = 20
MEM_DIR_FILES = 8
MEM_DIR_CAP = 8 * 1024
SESSIONS_LIST = 10

# Assignments whose VALUE is almost certainly a credential. Applied to
# settings / MCP config before returning them to the view.
_SECRET_RE = re.compile(
    r'(?i)((?:api[_-]?key|auth[_-]?token|access[_-]?token|token|secret|password|passwd|authorization)'
    r'[A-Za-z0-9_]*"?\s*[=:]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,\n]+)')


def _redact(text):
    return _SECRET_RE.sub(lambda m: m.group(1) + '"***"', text)


def _read_file(rel, cap, redact=False):
    p = os.path.expanduser(rel)
    if not os.path.isfile(p):
        return {"path": rel, "exists": False}
    st = os.stat(p)
    try:
        with open(p, "rb") as f:
            raw = f.read(cap + 1)
    except Exception as e:
        return {"path": rel, "exists": True, "size": st.st_size,
                "mtime": int(st.st_mtime), "error": str(e)}
    content = raw[:cap].decode("utf-8", "replace")
    return {"path": rel, "exists": True, "size": st.st_size,
            "mtime": int(st.st_mtime),
            "content": _redact(content) if redact else content,
            "truncated": len(raw) > cap}


def _review_skills(skills_dir):
    sd = os.path.expanduser(skills_dir)
    out = {"path": skills_dir, "exists": os.path.isdir(sd), "skills": []}
    if not out["exists"]:
        return out
    try:
        names = sorted(os.listdir(sd))
    except Exception as e:
        out["error"] = str(e)
        return out
    out["total"] = len(names)
    for name in names[:MAX_SKILLS]:
        entry = {"name": name}
        base = os.path.join(sd, name)
        # A skill is normally a dir with SKILL.md; tolerate plain files.
        cand = os.path.join(base, "SKILL.md") if os.path.isdir(base) else base
        if os.path.isfile(cand):
            rel = "%s/%s" % (skills_dir.rstrip("/"), name)
            f = _read_file(rel + "/SKILL.md" if os.path.isdir(base) else rel, SKILL_CAP)
            if f.get("content") is not None:
                entry["doc"] = f
        out["skills"].append(entry)
    return out


def _review_sessions(sessions_dir):
    p = os.path.expanduser(sessions_dir)
    out = {"path": sessions_dir, "exists": os.path.isdir(p), "count": 0, "recent": []}
    if not out["exists"]:
        return out
    seen = []
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.stat(fp)
                except Exception:
                    continue
                seen.append((int(st.st_mtime), st.st_size,
                             os.path.relpath(fp, p)))
    except Exception as e:
        out["error"] = str(e)
        return out
    out["count"] = len(seen)
    seen.sort(reverse=True)
    out["recent"] = [{"file": rel, "size": sz, "mtime": mt}
                     for mt, sz, rel in seen[:SESSIONS_LIST]]
    return out


def _review_mcp_claude(mcp_config):
    """~/.claude.json holds far more than MCP (history, state) — parse
    and return ONLY the mcpServers block, redacted."""
    p = os.path.expanduser(mcp_config)
    out = {"path": mcp_config, "exists": os.path.isfile(p), "servers": []}
    if not out["exists"]:
        return out
    try:
        with open(p) as f:
            cfg = json.load(f)
        servers = cfg.get("mcpServers") or {}
        out["servers"] = sorted(servers.keys())
        out["config"] = _redact(json.dumps(servers, indent=1))
    except Exception as e:
        out["error"] = str(e)
    return out


def _review_mcp_from_toml(settings_text):
    """codex keeps MCP in config.toml [mcp_servers.*] tables — cut those
    blocks out of the (already redacted) settings text."""
    blocks = []
    cur = None
    for line in settings_text.splitlines():
        if line.strip().startswith("["):
            if cur is not None:
                blocks.append("\n".join(cur))
            cur = [line] if line.strip().startswith("[mcp_servers") else None
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append("\n".join(cur))
    servers = []
    for b in blocks:
        m = re.match(r"\s*\[mcp_servers\.([^\]]+)\]", b)
        if m:
            servers.append(m.group(1).strip().strip('"'))
    return {"servers": sorted(servers),
            "config": "\n\n".join(blocks) if blocks else ""}


CAMC = os.path.expanduser("~/.cam/camc")


def _review_workspace_prompt(workspace_path, candidates):
    """Read the agent's workspace system prompt. The collector runs on
    the agent's own node, so the workspace dir is directly readable —
    this keeps the view single-path (no bound-agent bridge needed)."""
    out = {"dir": workspace_path, "candidates": candidates or []}
    base = os.path.expanduser(workspace_path)
    for name in candidates or []:
        p = os.path.join(base, name)
        if os.path.isfile(p):
            out["file"] = _read_file(
                "%s/%s" % (workspace_path.rstrip("/"), name), MEM_CAP)
            return out
    out["file"] = None
    return out


def _review_loops(agent_id):
    """camc-managed cron jobs + prompt loops owned by this agent. Mirrors
    the hub's query: jobs carry the `agent-<id8>-` name prefix; loops are
    owner-scoped. Errors are non-fatal and reported per leg."""
    out = {"agent_id": agent_id, "jobs": [], "loops": []}
    if not os.path.isfile(CAMC):
        out["error"] = "camc_missing"
        return out
    prefix = "agent-%s-" % str(agent_id)[:8]
    try:
        raw = subprocess.check_output(
            [CAMC, "cron", "list", "--json"],
            stderr=subprocess.DEVNULL, text=True, timeout=20)
        jobs = json.loads(raw or "{}").get("jobs") or []
        out["jobs"] = [j for j in jobs
                       if isinstance(j, dict) and str(j.get("name") or "").startswith(prefix)]
    except Exception as e:
        out["jobs_error"] = str(e)
    try:
        raw = subprocess.check_output(
            [CAMC, "cron", "list", "--loop", "--owner", str(agent_id), "--json"],
            stderr=subprocess.DEVNULL, text=True, timeout=20)
        out["loops"] = json.loads(raw or "{}").get("loops") or []
    except Exception as e:
        out["loops_error"] = str(e)
    return out


def review(args):
    tool = str(args.get("tool") or "").strip().lower()
    if not tool:
        return {"error": "missing_tool", "detail": "pass {tool: 'claude'|'codex'|...}"}
    ad = _user_adapter(tool) or ADAPTERS.get(tool)
    if not ad:
        return {"error": "no_adapter", "detail": "no adapter for tool '%s'" % tool,
                "known": sorted(set(list(ADAPTERS) + (list_adapters({})["user"])))}
    hp = ad.get("home_paths") or {}
    out = {"tool": tool, "adapter": "user" if _user_adapter(tool) else "builtin",
           "workspace_prompt_candidates": ad.get("workspace_system_prompt") or [],
           "sections": {}}
    sec = out["sections"]

    if hp.get("global_memory"):
        sec["global_memory"] = {"kind": "file",
                                "file": _read_file(hp["global_memory"], MEM_CAP)}
    if hp.get("memories"):
        md = os.path.expanduser(hp["memories"])
        msec = {"kind": "dir", "path": hp["memories"],
                "exists": os.path.isdir(md), "files": []}
        if msec["exists"]:
            try:
                names = sorted(os.listdir(md))
            except Exception as e:
                names = []
                msec["error"] = str(e)
            msec["total"] = len(names)
            for name in names[:MEM_DIR_FILES]:
                fp = os.path.join(md, name)
                if os.path.isfile(fp):
                    msec["files"].append(
                        _read_file("%s/%s" % (hp["memories"].rstrip("/"), name),
                                   MEM_DIR_CAP))
        sec["memories"] = msec

    settings_text = ""
    if hp.get("settings"):
        f = _read_file(hp["settings"], SETTINGS_CAP, redact=True)
        sec["settings"] = {"kind": "file", "file": f}
        settings_text = f.get("content") or ""

    if hp.get("skills_dir"):
        sec["skills"] = _review_skills(hp["skills_dir"])

    if hp.get("sessions_dir"):
        sec["sessions"] = _review_sessions(hp["sessions_dir"])

    if hp.get("mcp_config"):
        sec["mcp"] = _review_mcp_claude(hp["mcp_config"])
    elif settings_text:
        sec["mcp"] = _review_mcp_from_toml(settings_text)

    # Optional caller context: the agent's workspace dir and id unlock
    # the workspace system prompt and the camc loops sections.
    ws = str(args.get("workspace_path") or "").strip()
    if ws:
        out["workspace_prompt"] = _review_workspace_prompt(
            ws, out["workspace_prompt_candidates"])
    aid = str(args.get("agent_id") or "").strip()
    if aid:
        out["loops"] = _review_loops(aid)

    out["collected_at"] = int(time.time())
    return out


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


METHODS = {"collect": collect, "review": review,
           "install_adapter": install_adapter, "list_adapters": list_adapters}


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
