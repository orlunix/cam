"""Pane discovery and auxiliary shells in an agent's existing tmux session."""
import fcntl
import os
import re
import sqlite3
import subprocess


def terminal_request(action, agent, args, state_path, probe):
    from camc_pkg.transport import _find_tmux_socket, _tmux_bin_for_session
    if args.get("launch_id") is not None and args["launch_id"] != agent.get("launch_id"):
        raise ValueError("agent launch changed; refresh terminals")
    session = agent.get("tmux_session") or agent.get("session")
    if not session:
        raise ValueError("agent has no terminal")
    binary = agent.get("tmux_bin") or _tmux_bin_for_session(session)
    sock = agent.get("tmux_socket") or _find_tmux_socket(session)
    argv = [binary, "-u"] + (["-S", sock] if sock else [])

    def panes():
        try:
            output = subprocess.check_output(argv + ["list-panes", "-s", "-t", session, "-F",
                "#{pane_id} #{window_id} #{window_index} #{pane_index} #{pane_width} #{pane_height} #{status}"],
                stderr=subprocess.DEVNULL, timeout=5).decode()
        except subprocess.CalledProcessError:
            if subprocess.call(argv + ["has-session", "-t", session], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5):
                return []
            raise
        result = []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 7 or not re.fullmatch(r"%[0-9]+", fields[0]) or not re.fullmatch(r"@[0-9]+", fields[1]):
                raise ValueError("invalid terminal identity")
            window, pane = int(fields[2]), int(fields[3])
            primary = window == 0 and pane == 0
            result.append(dict(pane_id=fields[0], window=fields[1], window_index=window, pane_index=pane,
                title="Agent" if primary else "Terminal %d%s" % (window, ".%d" % pane if pane else ""),
                primary=primary, cols=int(fields[4]), rows=int(fields[5]), status_lines=0 if fields[6] == "off" else 1))
        return sorted(result, key=lambda item: (not item["primary"], item["window_index"], item["pane_index"]))

    current = panes()
    if action == "terminal.list":
        return {"panes": current}
    if action == "terminal.create":
        request = args.get("request_id", "")
        if not isinstance(request, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request):
            raise ValueError("terminal creation request ID required")
        fd = os.open(state_path + ".terminals.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with sqlite3.connect(state_path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS terminal_creates "
                           "(agent TEXT,request TEXT,pane TEXT,PRIMARY KEY(agent,request))")
                previous = db.execute("SELECT pane FROM terminal_creates WHERE agent=? AND request=?",
                                      (agent["id"], request)).fetchone()
                if previous:
                    if not previous[0]:
                        raise ValueError("terminal creation outcome unknown; refresh terminals before retrying")
                    return {"pane_id": previous[0], "panes": panes()}
                current = panes()
                if not current:
                    raise ValueError("agent tmux session is not running")
                if len(current) >= 8:
                    raise ValueError("close a terminal before creating another (8-terminal limit)")
                index = max([p["window_index"] for p in current] + [0]) + 1
                path = agent.get("context_path") or agent.get("path") or os.path.expanduser("~")
                if not os.path.isabs(path) or not os.path.isdir(path):
                    raise ValueError("agent workspace is unavailable")
                db.execute("INSERT INTO terminal_creates VALUES (?,?,NULL)", (agent["id"], request))
                db.commit()  # Unknown outcomes are never automatically replayed.
                pane = subprocess.check_output(argv + ["new-window", "-d", "-t", "%s:%d" % (session, index),
                    "-n", "terminal-%d" % index, "-c", path, "-P", "-F", "#{pane_id}"],
                    stderr=subprocess.DEVNULL, timeout=5).decode().strip()
                if not re.fullmatch(r"%[0-9]+", pane):
                    raise ValueError("invalid new terminal identity")
                db.execute("UPDATE terminal_creates SET pane=? WHERE agent=? AND request=?", (pane, agent["id"], request))
            return {"pane_id": pane, "panes": panes()}
    wanted = args.get("pane_id")
    if wanted is not None and (not isinstance(wanted, str) or not re.fullmatch(r"%[0-9]+", wanted)):
        raise ValueError("invalid pane ID")
    selected = next((p for p in current if p["pane_id"] == wanted), None) if wanted else next(
        (p for p in current if p["primary"]), None)
    if action == "terminal.close":
        if not wanted:
            raise ValueError("auxiliary pane ID required")
        if selected and selected["primary"]:
            raise ValueError("the agent terminal cannot be closed; use Stop or Kill")
        if selected:
            subprocess.check_call(argv + ["kill-pane", "-t", wanted], stderr=subprocess.DEVNULL, timeout=5)
        return {"ok": True, "pane_id": wanted, "panes": panes()}
    if action != "terminal.resolve":
        raise ValueError("unsupported terminal action")
    if not selected:
        raise ValueError("agent pane is unavailable")
    result = dict(selected, argv=argv, session=session, pane=selected["pane_id"])
    if args.get("probe_modes"):
        modes = subprocess.check_output(argv + ["display-message", "-p", "-t", selected["pane_id"],
            "#{window_width};#{window_height};#{pane_active};#{window_active};#{bracketed_paste_flag}"],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip().split(";")
        result["bracketed_paste"] = None
        if len(modes) == 5:
            if modes[4] in ("0", "1"):
                result["bracketed_paste"] = modes[4] == "1"
            elif modes[2:4] == ["1", "1"]:
                result["bracketed_paste"] = probe(argv, session, int(modes[0]), int(modes[1]) + selected["status_lines"])
    return result
