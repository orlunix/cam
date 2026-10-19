"""cam tui — cam's terminal interface (stdlib curses, lazy-imported).

One of the cam interfaces, alongside cam desktop / cam mobile / cam webui.
Launched by bare `cam` or `cam tui` (see cam.cli.app). Agent operations
shell out to `camc` from the same Python environment.

Layout (Herdr-inspired): left sidebar = info/commands (top) + agent
list (bottom); right = agent tabs + work window rendering the live tmux
screen (capture-pane -e keeps SGR colours — tmux is the terminal
emulator, we only parse SGR); bottom bar = mouse actions + composer.

Mouse: click buttons/tabs/list items · wheel scrolls. The composer is
always available; there is no input mode and there are no letter hotkeys.
Keys: Up/Down select · Enter sends a non-empty composer, otherwise opens
the agent action menu (attach/reboot/rename/exit/stop/kill/rm) ·
":" on an empty composer opens the command console — any one-shot
cam/camc command runs in the background and its output opens in the work
pane (output mode) · Esc clears input / leaves output mode / goes back ·
Tab toggles the narrow pane · PgUp/PgDn scroll · Home/End jump
top/bottom · Ctrl+X quits.

Import safety: curses is optional at import time so `cam` never fails at
startup on minimal Python installs (e.g. Windows); run() reports a clean
error when it is missing.
"""

import locale
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata

try:
    import curses
except ImportError:  # minimal Python installs may lack _curses
    curses = None

from cam import __version__
from camc_pkg.storage import AgentStore

REFRESH_CAPTURE_S = 1.5
REFRESH_LIST_S = 5.0
TICK_MS = 250
CAPTURE_TAIL_LINES = 400
INFO_H = 6
SIDEBAR_DEFAULT = 26
SIDEBAR_MIN = 18
SIDEBAR_MAX = 36
NARROW_W = 60  # below this: single-column, Tab toggles list/work

F_BOLD = 1
F_DIM = 2
F_UNDERLINE = 4
F_REVERSE = 8

_STATUS_RANK = {"running": 0, "waiting": 1, "idle": 1,
                "created": 2, "stopped": 3, "exited": 3}
_STATUS_DOT = {"running": "●", "waiting": "◐", "idle": "◐",
               "created": "◌", "stopped": "○", "exited": "○"}
_DOT_COLOR = {"running": 2, "waiting": 3, "idle": 3,   # green / yellow
              "created": 6, "stopped": 1, "exited": 1}  # cyan / red


# ---------------------------------------------------------------------------
# Pure helpers (no curses) — unit-testable
# ---------------------------------------------------------------------------

def _disp_width(s):
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _truncate_disp(s, w):
    """Truncate s to display width w, appending … when truncated."""
    if _disp_width(s) <= w:
        return s
    out, used = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def _rgb_to_256(r, g, b):
    """Map a truecolor triple to the xterm-256 palette."""
    if max(r, g, b) - min(r, g, b) <= 8:  # near-gray → gray ramp
        v = (r + g + b) // 3
        return 232 + max(0, min(23, round((v - 8) / 247 * 24)))
    ri = round(r / 255 * 5)
    gi = round(g / 255 * 5)
    bi = round(b / 255 * 5)
    return 16 + 36 * ri + 6 * gi + bi


_ESC_TOKEN = re.compile(
    r"\x1b\[([0-9;]*)m"          # SGR
    r"|\x1b\[[0-9;?]*[A-Za-z]"   # other CSI — drop
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC — drop
    r"|\x1b[()][0-9A-Za-z]"      # charset select — drop
    r"|\x1b."                    # any other escape — drop
)


def parse_ansi_rows(text):
    """Parse tmux capture-pane -e output (SGR-only ANSI) into rows of
    styled segments: [[(text, fg, bg, flags), ...], ...].
    fg/bg are xterm-256 indices or -1 for default. Non-SGR escapes are
    stripped; the terminal state is tmux's problem, not ours."""
    rows = []
    for raw in text.split("\n"):
        raw = raw.expandtabs(8)
        segs = []
        fg, bg, flags = -1, -1, 0
        pos = 0
        for m in _ESC_TOKEN.finditer(raw):
            if m.start() > pos:
                segs.append((raw[pos:m.start()], fg, bg, flags))
            params = m.group(1)
            if params is not None:  # SGR
                nums = [int(p) if p else 0 for p in params.split(";")]
                i = 0
                while i < len(nums):
                    n = nums[i]
                    if n == 0:
                        fg, bg, flags = -1, -1, 0
                    elif n == 1:
                        flags |= F_BOLD
                    elif n == 2:
                        flags |= F_DIM
                    elif n == 4:
                        flags |= F_UNDERLINE
                    elif n == 7:
                        flags |= F_REVERSE
                    elif n == 22:
                        flags &= ~(F_BOLD | F_DIM)
                    elif n == 24:
                        flags &= ~F_UNDERLINE
                    elif n == 27:
                        flags &= ~F_REVERSE
                    elif 30 <= n <= 37:
                        fg = n - 30
                    elif n == 39:
                        fg = -1
                    elif 40 <= n <= 47:
                        bg = n - 40
                    elif n == 49:
                        bg = -1
                    elif 90 <= n <= 97:
                        fg = n - 90 + 8
                    elif 100 <= n <= 107:
                        bg = n - 100 + 8
                    elif n in (38, 48) and i + 1 < len(nums):
                        mode = nums[i + 1]
                        if mode == 5 and i + 2 < len(nums):
                            val = max(0, min(255, nums[i + 2]))
                            if n == 38:
                                fg = val
                            else:
                                bg = val
                            i += 2
                        elif mode == 2 and i + 4 < len(nums):
                            val = _rgb_to_256(*[max(0, min(255, v))
                                                for v in nums[i + 2:i + 5]])
                            if n == 38:
                                fg = val
                            else:
                                bg = val
                            i += 4
                    i += 1
            pos = m.end()
        if pos < len(raw):
            segs.append((raw[pos:], fg, bg, flags))
        # drop fully-blank trailing segments; keep row even if empty
        segs = [(t, f, b, fl) for (t, f, b, fl) in segs if t]
        rows.append(segs)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _fld(agent, field, default=""):
    """Record field lookup: nested task dict first (cli._tf), then
    top-level, then legacy session-name fallbacks (cli._sf)."""
    task = agent.get("task")
    if isinstance(task, dict) and task.get(field) not in (None, ""):
        return task[field]
    v = agent.get(field)
    if v not in (None, ""):
        return v
    legacy = {"tmux_session": "session", "name": "id"}
    return agent.get(legacy.get(field, ""), default)


def _is_local(agent, hostname):
    h = agent.get("hostname")
    if not h or not hostname:
        return True
    return h == hostname or h.split(".")[0] == hostname.split(".")[0]


def gather_agents(hostname=None):
    """Local agents sorted by status rank then name."""
    hostname = hostname if hostname is not None else socket.gethostname()
    agents = [a for a in AgentStore().list() if _is_local(a, hostname)]
    agents.sort(key=lambda a: (_STATUS_RANK.get(a.get("status"), 4),
                               str(_fld(a, "name"))))
    return agents


def gather_nodes():
    """machines.json entries (the Desktop 'Nodes' page equivalent)."""
    from camc_pkg.machine_store import MachineStore
    nodes = MachineStore().list()
    nodes.sort(key=lambda m: str(m.get("name") or ""))
    return nodes


_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_node_form(values, existing_names, editing=False):
    """Validate an add/edit-node form. values keys: name, host, user,
    port, key_file, env_setup. Returns an error string, or None when ok.
    Mirrors cmd_machine_add's rules (ssh machines need host + user)."""
    name = (values.get("name") or "").strip()
    if not editing:
        if not name:
            return "name is required"
        if not _NODE_NAME_RE.match(name):
            return "name: letters, digits, _ . - only"
        if name == "local":
            return "'local' is reserved"
        if name in existing_names:
            return "machine '%s' already exists" % name
    if not (values.get("host") or "").strip():
        return "host is required"
    if not (values.get("user") or "").strip():
        return "user is required"
    port = (values.get("port") or "").strip() or "22"
    try:
        p = int(port)
        if not 1 <= p <= 65535:
            raise ValueError
    except ValueError:
        return "port must be 1-65535"
    return None


def node_record_from_form(name, values):
    """Build a machines.json record from validated form values."""
    rec = {"name": name, "type": "ssh",
           "host": values["host"].strip(), "user": values["user"].strip()}
    port = (values.get("port") or "").strip()
    rec["port"] = int(port) if port else 22
    for opt in ("key_file", "env_setup", "camc_path", "remote_home"):
        v = (values.get(opt) or "").strip()
        if v:
            rec[opt] = v
    return rec


_AGENT_MENU = (("attach", "attach"), ("reboot (resume)", "reboot"),
               ("rename", "rename"), ("exit (graceful)", "exit"),
               ("stop", "stop"), ("kill", "kill"), ("rm (remove)", "rm"))

_COMMAND_BUTTONS = (("new", "new"), ("settings", "settings"),
                    ("nodes", "nodes"))
_COMMAND_BUTTONS_COMPACT = (("new", "new"), ("set", "settings"),
                            ("nod", "nodes"))
_TAB_MAX_W = 18


def command_buttons(width):
    """Command labels that fit the sidebar, with a narrow fallback."""
    full = " ".join("[%s]" % label for label, _ in _COMMAND_BUTTONS)
    return _COMMAND_BUTTONS if _disp_width(full) <= width \
        else _COMMAND_BUTTONS_COMPACT


def tab_geometry(count, selected, width):
    """Return (first index, visible count, tab width) for the tab strip."""
    if count <= 0 or width <= 0:
        return 0, 0, 0
    selected = max(0, min(selected, count - 1))
    slots = max(1, min(count, (width + 1) // (_TAB_MAX_W + 1)))
    top = max(0, min(selected - slots + 1, count - slots))
    tab_w = max(1, min(_TAB_MAX_W, (width - slots + 1) // slots))
    return top, slots, tab_w


def layout(height, width, sidebar_w=None):
    """Rect computation in one pure function (Herdr-style).
    Returns dict of (y, x, h, w) rects; narrow terminals collapse to a
    single column (sidebar view or work view, caller toggles)."""
    bar = (height - 1, 0, 1, width)
    if sidebar_w is None:
        sidebar_w = max(SIDEBAR_MIN, min(SIDEBAR_MAX, SIDEBAR_DEFAULT))
    if width < NARROW_W:
        return {"narrow": True, "bar": bar,
                "info": (0, 0, INFO_H, width),
                "list": (INFO_H, 0, height - INFO_H - 1, width),
                "header": (0, 0, 1, width),
                "work": (1, 0, height - 2, width)}
    sw = min(sidebar_w, max(SIDEBAR_MIN, width - 30))
    return {"narrow": False, "bar": bar,
            "info": (0, 0, INFO_H, sw),
            "list": (INFO_H, 0, height - INFO_H - 1, sw),
            "header": (0, sw, 1, width - sw),
            "work": (1, sw, height - 2, width - sw)}


# ---------------------------------------------------------------------------
# Curses shell
# ---------------------------------------------------------------------------

class _Palette:
    """On-demand color-pair allocation with 8-color fallback."""

    def __init__(self):
        self.enabled = curses.has_colors()
        self._pairs = {}
        self._n = 0
        if self.enabled:
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass

    @staticmethod
    def _to8(c):
        if c < 0:
            return -1
        if c < 16:
            return c % 8
        if c >= 232:
            return 7 if c >= 244 else 0
        c -= 16
        r, g, b = c // 36, (c // 6) % 6, c % 6
        return (1 if r >= 3 else 0) + (2 if g >= 3 else 0) + (4 if b >= 3 else 0)

    def attr(self, fg, bg, flags):
        a = 0
        if flags & F_BOLD:
            a |= curses.A_BOLD
        if flags & F_DIM:
            a |= curses.A_DIM
        if flags & F_UNDERLINE:
            a |= curses.A_UNDERLINE
        if flags & F_REVERSE:
            a |= curses.A_REVERSE
        if self.enabled:
            if curses.COLORS < 256:
                fg, bg = self._to8(fg), self._to8(bg)
            key = (fg, bg)
            pair = self._pairs.get(key)
            if pair is None:
                if self._n >= min(curses.COLOR_PAIRS, 256) - 1:
                    return a
                self._n += 1
                pair = self._n
                try:
                    curses.init_pair(pair, fg, bg)
                except curses.error:
                    pair = 0
                self._pairs[key] = pair
            if pair:
                a |= curses.color_pair(pair)
        return a


class Tui:
    def __init__(self):
        self.agents = []
        self.sel = 0
        self.scroll = 0  # lines up from the bottom of the capture
        self.rows = []   # parsed ANSI rows of the selected agent
        self.narrow_view = "list"  # narrow terminals: "list" | "work"
        self.input_buf = ""
        self.message = ""
        self.next_capture = 0.0
        self.next_list = 0.0
        self.refresh_s = REFRESH_CAPTURE_S      # settings-tunable
        self.capture_lines = CAPTURE_TAIL_LINES  # settings-tunable
        self._hits = []  # (y, x, w, action) mouse hit map, rebuilt per draw
        # ops layer: nodes view (Desktop "Nodes" page equivalent)
        self.view = "agents"       # "agents" | "nodes"
        self.nodes = []
        self.node_sel = 0
        self.ping = {}             # node name -> "…" | "ok" | "FAIL"
        self.form = None           # modal form state dict or None
        self.confirm = None        # (prompt, on_yes) or None
        self.menu = None           # modal agent-action menu or None
        self.work_mode = "agent"   # "agent" (live capture) | "output" (cmd)
        self.output_title = ""     # work-pane title while in output mode

    # -- data ---------------------------------------------------------------

    def _refresh_agents(self):
        old_id = self._selected_id()
        self.agents = gather_agents()
        ids = [a.get("id") for a in self.agents]
        if old_id in ids:
            self.sel = ids.index(old_id)
        self.sel = max(0, min(self.sel, len(self.agents) - 1))

    def _refresh_nodes(self):
        old = self._selected_node_name()
        self.nodes = gather_nodes()
        names = [n.get("name") for n in self.nodes]
        if old in names:
            self.node_sel = names.index(old)
        self.node_sel = max(0, min(self.node_sel, len(self.nodes) - 1))

    def _selected_node_name(self):
        if self.nodes and 0 <= self.node_sel < len(self.nodes):
            return self.nodes[self.node_sel].get("name")
        return None

    def _selected_node(self):
        if self.nodes and 0 <= self.node_sel < len(self.nodes):
            return self.nodes[self.node_sel]
        return None

    def _selected_id(self):
        if self.agents and 0 <= self.sel < len(self.agents):
            return self.agents[self.sel].get("id")
        return None

    def _session_of(self, agent):
        return _fld(agent, "tmux_session")

    def _recapture(self):
        from camc_pkg.transport import capture_tmux
        agent = self.agents[self.sel] if self.agents else None
        session = self._session_of(agent) if agent else None
        if not session:
            self.rows = []
            return
        text = capture_tmux(session, lines=self.capture_lines,
                            preserve_ansi=True)
        self.rows = parse_ansi_rows(text) if text else []
        self._clamp_scroll()

    # -- geometry -----------------------------------------------------------

    def _rect(self, name):
        return self._layout[name]

    def _clamp_scroll(self):
        wh = self._rect("work")[2] if hasattr(self, "_layout") else 20
        self.scroll = max(0, min(self.scroll, max(0, len(self.rows) - wh)))

    # -- rendering ----------------------------------------------------------

    def _put(self, y, x, s, attr=0):
        try:
            maxy, maxx = self.stdscr.getmaxyx()
            if 0 <= y < maxy and 0 <= x < maxx:
                self.stdscr.addnstr(y, x, s, maxx - x - 1, attr)
        except curses.error:
            pass

    def _hline(self, y, x, w):
        try:
            self.stdscr.hline(y, x, curses.ACS_HLINE, w)
        except curses.error:
            pass

    def _draw_info(self):
        y, x, h, w = self._rect("info")
        host = socket.gethostname().split(".")[0]
        lines = [
            "cam tui v%s" % __version__,
            "host: %s" % host,
            "agents: %d  nodes: %d" % (len(self.agents), len(self.nodes)),
            self.message or time.strftime("%H:%M:%S"),
        ]
        for i in range(h - 1):
            text = lines[i] if i < len(lines) else ""
            self._put(y + i, x, _truncate_disp(text, w - 1))
        bx = x
        for label, action in command_buttons(w - 1):
            text = "[%s]" % label
            room = w - (bx - x) - 1
            if room <= 0:
                break
            text = _truncate_disp(text, room)
            attr = self.palette.attr(6, -1, 0)
            if action == "nodes" and self.view == "nodes":
                attr |= curses.A_REVERSE
            self._put(y + h - 2, bx, text, attr)
            self._hits.append((y + h - 2, bx, len(text), ("button", action)))
            bx += len(text) + 1
        self._hline(y + h - 1, x, w)

    def _draw_list(self):
        y, x, h, w = self._rect("list")
        self._put(y, x, _truncate_disp("agents (↑↓)", w - 1), curses.A_BOLD)
        self._hline(y + 1, x, w)
        rows_y, rows_h = y + 2, h - 2
        if not self.agents:
            self._put(rows_y, x, "(no agents)")
            return
        # keep selection visible
        top = 0
        if self.sel >= rows_h:
            top = self.sel - rows_h + 1
        for i, a in enumerate(self.agents[top:top + rows_h]):
            idx = top + i
            st = a.get("status") or ""
            dot = _STATUS_DOT.get(st, "?")
            name = "%s %s %s" % (a.get("id", "?")[:8], _fld(a, "name"),
                                 a.get("tool") or "")
            attr = 0
            if idx == self.sel:
                attr |= curses.A_REVERSE
            dot_attr = self.palette.attr(_DOT_COLOR.get(st, -1), -1, 0) | attr
            self._put(rows_y + i, x, dot, dot_attr)
            self._put(rows_y + i, x + 2, _truncate_disp(name, w - 3), attr)
            self._hits.append((rows_y + i, x, w, ("agent", idx)))

    def _draw_header(self, title):
        hy, hx, hh, hw = self._rect("header")
        shown = _truncate_disp(title, hw - 1)
        self._put(hy, hx, shown, curses.A_BOLD)
        self._hline(hy, hx + _disp_width(shown),
                    max(0, hw - _disp_width(shown) - 1))

    def _draw_tabs(self):
        hy, hx, hh, hw = self._rect("header")
        if not self.agents:
            self._draw_header(" (no agent selected)")
            return
        top, slots, tab_w = tab_geometry(len(self.agents), self.sel, hw)
        cx = hx
        for idx in range(top, top + slots):
            agent = self.agents[idx]
            dot = _STATUS_DOT.get(agent.get("status") or "", "?")
            raw = " %s %s " % (dot, _fld(agent, "name"))
            text = _truncate_disp(raw, tab_w)
            text += " " * max(0, tab_w - _disp_width(text))
            attr = curses.A_REVERSE if idx == self.sel else 0
            self._put(hy, cx, text, attr)
            self._hits.append((hy, cx, tab_w, ("agent", idx)))
            cx += tab_w
            if idx + 1 < top + slots:
                self._put(hy, cx, " ")
                cx += 1
        if cx < hx + hw:
            self._hline(hy, cx, hx + hw - cx - 1)
        if self.scroll:
            marker = "[scrolled %d]" % self.scroll
            if hx + hw - cx > _disp_width(marker) + 1:
                self._put(hy, hx + hw - _disp_width(marker) - 1, marker)

    def _draw_rows(self, empty_hint):
        y, x, h, w = self._rect("work")
        if not self.rows:
            self._put(y, x, empty_hint)
            return
        end = len(self.rows) - self.scroll
        visible = self.rows[max(0, end - h):end]
        for i, row in enumerate(visible):
            cx = x
            for text, fg, bg, flags in row:
                room = w - (cx - x)
                if room <= 0:
                    break
                text = _truncate_disp(text, room)
                self._put(y + i, cx, text, self.palette.attr(fg, bg, flags))
                cx += _disp_width(text)

    def _draw_work(self):
        y, x, h, w = self._rect("work")
        if self.work_mode == "output":
            self._draw_header(" " + (self.output_title or "command output"))
            self._draw_rows("(no output)")
            return
        agent = self.agents[self.sel] if self.agents else None
        self._draw_tabs()
        if not agent:
            self._put(y, x, "use [new] or `cam run <tool>` to start an agent")
            return
        self._draw_rows("(no screen output — agent starting or session gone)")

    # -- nodes view (Desktop "Nodes" page equivalent) ------------------------

    _NODE_FIELDS = ("name", "host", "user", "port", "key_file",
                    "camc_path", "remote_home", "env_setup")

    def _draw_nodes(self):
        y, x, h, w = self._rect("list")
        self._put(y, x, _truncate_disp("nodes (↑↓)", w - 1), curses.A_BOLD)
        self._hline(y + 1, x, w)
        rows_y, rows_h = y + 2, h - 2
        if not self.nodes:
            self._put(rows_y, x, "(no nodes — press n)")
            return
        top = 0
        if self.node_sel >= rows_h:
            top = self.node_sel - rows_h + 1
        for i, m in enumerate(self.nodes[top:top + rows_h]):
            idx = top + i
            name = m.get("name") or "?"
            p = self.ping.get(name, "")
            dot, color = ("○", -1) if not p else \
                ("…", 3) if p == "…" else \
                (("●", 2) if p == "ok" else ("●", 1))
            label = "%s@%s:%s" % (m.get("user", ""), m.get("host", ""),
                                  m.get("port", 22)) if m.get("type") == "ssh" \
                else m.get("type", "")
            attr = curses.A_REVERSE if idx == self.node_sel else 0
            self._put(rows_y + i, x, dot, self.palette.attr(color, -1, 0) | attr)
            self._put(rows_y + i, x + 2, _truncate_disp(name, w - 3), attr)
            if not attr and label:
                tail = _truncate_disp(label, w - 3)
                self._put(rows_y + i, x + w - 1 - _disp_width(tail), tail,
                          self.palette.attr(-1, -1, F_DIM))
            self._hits.append((rows_y + i, x, w, ("node", idx)))

    def _draw_node_detail(self):
        y, x, h, w = self._rect("work")
        m = self._selected_node()
        self._draw_header(" node: %s" % (m.get("name") if m else "-"))
        if not m:
            self._put(y, x, "machines live in ~/.cam/machines.json")
            return
        lines = []
        for f in self._NODE_FIELDS:
            v = m.get(f)
            if v not in (None, ""):
                lines.append("%-12s %s" % (f + ":", v))
        p = self.ping.get(m.get("name"))
        if p:
            lines.append("%-12s %s" % ("ping:", p))
        for i, line in enumerate(lines[:h]):
            self._put(y + i, x, _truncate_disp(line, w - 1))

    # -- modal form (add/edit node) ------------------------------------------

    def _open_node_form(self, edit=False):
        m = self._selected_node() if edit else None
        if edit and not m:
            return
        values = {f: (str(m.get(f, "")) if m else "") for f in self._NODE_FIELDS}
        if m and m.get("port"):
            values["port"] = str(m["port"])
        self.form = {
            "title": "Edit node: %s" % m["name"] if m else "Add node",
            "fields": list(self._NODE_FIELDS),
            "values": values,
            "focus": 0,
            "edit": bool(m),
            "orig_name": m.get("name") if m else None,
            "error": "",
        }

    def _draw_form(self):
        f = self.form
        maxy, maxx = self.stdscr.getmaxyx()
        fw = min(56, maxx - 4)
        fh = len(f["fields"]) + 5
        fy, fx = max(0, (maxy - fh) // 2), max(0, (maxx - fw) // 2)
        for i in range(fh):
            self._put(fy + i, fx, " " * (fw - 1), curses.A_REVERSE)
        self._put(fy, fx + 2, _truncate_disp(" %s " % f["title"], fw - 4),
                  curses.A_REVERSE | curses.A_BOLD)
        for i, field in enumerate(f["fields"]):
            row = fy + 2 + i
            label = "%-12s" % (field + ":")
            val = f["values"].get(field, "")
            attr = curses.A_REVERSE
            if i == f["focus"]:
                attr |= curses.A_BOLD
                shown = val + "█"
            else:
                shown = val
            self._put(row, fx + 2, label, curses.A_REVERSE)
            self._put(row, fx + 15, _truncate_disp(shown, fw - 18), attr)
        hint = f["error"] or "↑↓ field · type to edit · Enter save · Esc cancel"
        hattr = self.palette.attr(1, -1, 0) | curses.A_REVERSE if f["error"] \
            else curses.A_REVERSE
        self._put(fy + fh - 2, fx + 2, _truncate_disp(hint, fw - 4), hattr)
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def _form_key(self, ch):
        f = self.form
        if ch == "\x1b":
            self.form = None
            return
        if ch in (curses.KEY_UP, curses.KEY_BTAB):
            f["focus"] = (f["focus"] - 1) % len(f["fields"])
            return
        if ch in (curses.KEY_DOWN, "\t"):
            f["focus"] = (f["focus"] + 1) % len(f["fields"])
            return
        if ch in ("\n", "\r", curses.KEY_ENTER):
            kind = f.get("kind", "node")
            if kind == "agent":
                self._submit_agent_form()
            elif kind == "settings":
                self._submit_settings()
            elif kind == "rename":
                self._submit_rename()
            elif kind == "command":
                self._submit_command()
            else:
                self._submit_node_form()
            return
        field = f["fields"][f["focus"]]
        if field == "name" and f.get("edit"):
            return  # rename not supported; delete + re-add instead
        if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            f["values"][field] = f["values"].get(field, "")[:-1]
        elif ch == "\x15":
            f["values"][field] = ""
        elif isinstance(ch, str) and ch.isprintable():
            if field in ("port", "capture_tail_lines") and not ch.isdigit():
                return
            if field == "capture_refresh_s" and not (ch.isdigit() or ch == "."):
                return
            f["values"][field] = f["values"].get(field, "") + ch
        f["error"] = ""

    def _submit_node_form(self):
        f = self.form
        from camc_pkg.machine_store import MachineStore
        store = MachineStore()
        existing = {m.get("name") for m in store.list()}
        err = validate_node_form(f["values"], existing, editing=f["edit"])
        if err:
            f["error"] = err
            return
        name = f["orig_name"] if f["edit"] else f["values"]["name"].strip()
        store.save(node_record_from_form(name, f["values"]))
        self.message = "node '%s' %s" % (name, "updated" if f["edit"] else "added")
        self.form = None
        self._refresh_nodes()

    def _ping_node(self, m):
        name = m.get("name")
        if not name or m.get("type") != "ssh":
            self.message = "ping applies to ssh nodes"
            return
        self.ping[name] = "…"

        def work():
            from camc_pkg.remote import ssh_ping
            try:
                ok = ssh_ping(m, timeout=8)
            except Exception:
                ok = False
            self.ping[name] = "ok" if ok else "FAIL"

        threading.Thread(target=work, daemon=True).start()

    def _node_command(self, action):
        if action == "add":
            self._open_node_form(edit=False)
            return
        m = self._selected_node()
        if not m:
            return
        if action == "edit":
            if m.get("name") == "local":
                self.message = "'local' is built-in, not editable"
            else:
                self._open_node_form(edit=True)
        elif action == "ping":
            self._ping_node(m)
        elif action == "delete" and m.get("name") != "local":
            self.confirm = (
                "Delete node '%s'? Enter delete · Esc cancel" % m.get("name"),
                lambda: self._delete_node(m))

    def _node_key(self, ch):
        if ch == curses.KEY_UP:
            self.node_sel = max(0, self.node_sel - 1)
        elif ch == curses.KEY_DOWN:
            self.node_sel = min(max(0, len(self.nodes) - 1), self.node_sel + 1)
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            self._node_command("edit")

    def _delete_node(self, m):
        from camc_pkg.machine_store import MachineStore
        MachineStore().remove(m.get("name"))
        self.ping.pop(m.get("name"), None)
        self.message = "node '%s' deleted" % m.get("name")
        self._refresh_nodes()

    # -- mouse (Herdr hit-map style: _draw registers rects, clicks dispatch) --

    def _mouse(self):
        try:
            _id, x, y, _z, bstate = curses.getmouse()
        except curses.error:
            return
        if self.menu:
            if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                for hy, hx, hw, action in reversed(self._hits):
                    if hy == y and hx <= x < hx + hw and action[0] == "menu":
                        self._hit(action)
                        return
            return
        if self.form or self.confirm:
            return
        wy, wx, wh, ww = self._rect("work")
        ly, lx, lh, lw = self._rect("list")
        in_work = wy <= y < wy + wh and wx <= x < wx + ww
        in_list = ly <= y < ly + lh and lx <= x < lx + lw
        if bstate & getattr(curses, "BUTTON4_PRESSED", 0):  # wheel up
            if in_work and self.view == "agents":
                self.scroll += 3
                self._clamp_scroll()
            elif in_list:
                self._step_sel(-1)
            return
        if bstate & getattr(curses, "BUTTON5_PRESSED", 0):  # wheel down
            if in_work and self.view == "agents":
                self.scroll -= 3
                self._clamp_scroll()
            elif in_list:
                self._step_sel(1)
            return
        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
            for hy, hx, hw, action in reversed(self._hits):
                if hy == y and hx <= x < hx + hw:
                    self._hit(action)
                    return

    def _step_sel(self, d):
        if self.view == "nodes":
            self.node_sel = max(0, min(len(self.nodes) - 1, self.node_sel + d))
        else:
            self.sel = max(0, min(len(self.agents) - 1, self.sel + d))
            self.scroll = 0
            self.next_capture = 0.0

    def _hit(self, action):
        kind, val = action
        if kind == "agent":
            self.sel = val
            self.scroll = 0
            self.next_capture = 0.0
        elif kind == "node":
            self.node_sel = val
        elif kind == "menu":
            op = None
            if self.menu and 0 <= val < len(self.menu["items"]):
                op = self.menu["items"][val][1]
            self.menu = None
            if op:
                self._agent_action(op)
        elif kind == "button":
            self.message = ""
            if val == "new":
                self._open_agent_form()
            elif val == "settings":
                self._open_settings()
            elif val == "actions":
                self._open_agent_menu()
            elif val == "cmd":
                self._open_console()
            elif (val == "back" and self.view == "agents"
                    and self.work_mode == "output"):
                self.work_mode = "agent"
                self.next_capture = 0.0
            elif val in ("nodes", "back"):
                self._toggle_view()
            elif val == "attach":
                self._attach()
            elif val == "refresh":
                self.next_capture = self.next_list = 0.0
                self.message = "refreshed"
            elif val == "quit":
                raise _Quit
            elif val in ("add", "edit", "delete", "ping"):
                self._node_command(val)

    def _toggle_view(self):
        self.view = "nodes" if self.view == "agents" else "agents"
        if self.view == "nodes" and not self.nodes:
            self._refresh_nodes()

    # -- new-agent / settings forms -------------------------------------------

    @staticmethod
    def _camc_argv():
        """Agent operations shell out to `camc` from the same Python env
        (the wheel ships camc_pkg alongside cam). Never use sys.argv[0]
        here: that is `cam`, whose command syntax differs."""
        return [sys.executable, "-m", "camc_pkg"]

    def _run_camc(self, argv, timeout=120):
        """Run `camc <argv>` in a background thread; report the last output
        line in the status bar and refresh agents/capture when done."""
        full = self._camc_argv() + list(argv)
        self.message = "running: camc %s…" % " ".join(argv)

        def work():
            try:
                proc = subprocess.run(full, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,
                                      universal_newlines=True, timeout=timeout)
                out = (proc.stdout or "").strip().splitlines()
                tail = (out[-1] if out else "")[:180]
                if not tail:
                    tail = "camc " + " ".join(argv)
                self.message = ("done: %s" % tail) if proc.returncode == 0 \
                    else "failed (%d): %s" % (proc.returncode, tail)
            except Exception as e:
                self.message = "camc %s failed: %s" % (argv[0], str(e)[:150])
            self.next_list = 0.0
            self.next_capture = 0.0

        threading.Thread(target=work, daemon=True).start()

    def _open_agent_form(self):
        tools = ["codex", "claude", "pi", "cursor"]
        try:
            from camc_pkg.adapters import _EMBEDDED_CONFIGS
            if isinstance(_EMBEDDED_CONFIGS, dict) and _EMBEDDED_CONFIGS:
                tools = sorted(_EMBEDDED_CONFIGS)
        except Exception:
            pass
        self.form = {
            "title": "New agent", "kind": "agent",
            "fields": ["tool", "name", "path", "prompt"],
            "values": {"tool": tools[0], "name": "",
                       "path": os.getcwd(), "prompt": ""},
            "focus": 0, "error": "",
        }

    def _submit_agent_form(self):
        f = self.form
        v = f["values"]
        tool = (v.get("tool") or "").strip()
        path = (v.get("path") or "").strip() or os.getcwd()
        if not tool:
            f["error"] = "tool is required"
            return
        if not os.path.isdir(path):
            f["error"] = "path does not exist"
            return
        argv = ["run", "--tool", tool, "--path", path]
        if (v.get("name") or "").strip():
            argv += ["--name", v["name"].strip()]
        if (v.get("prompt") or "").strip():
            argv += [v["prompt"].strip()]
        self.form = None
        self._run_camc(argv, timeout=90)

    def _open_settings(self):
        self.form = {
            "title": "Settings (session only)", "kind": "settings",
            "fields": ["capture_refresh_s", "capture_tail_lines"],
            "values": {"capture_refresh_s": "%g" % self.refresh_s,
                       "capture_tail_lines": str(self.capture_lines)},
            "focus": 0, "error": "",
        }

    def _submit_settings(self):
        f = self.form
        try:
            rs = float((f["values"].get("capture_refresh_s") or "").strip())
            cl = int((f["values"].get("capture_tail_lines") or "").strip())
        except ValueError:
            f["error"] = "numbers only"
            return
        if not 0.3 <= rs <= 30:
            f["error"] = "refresh must be 0.3-30 s"
            return
        if not 50 <= cl <= 10000:
            f["error"] = "tail lines must be 50-10000"
            return
        self.refresh_s, self.capture_lines = rs, cl
        self.form = None
        self.message = "settings applied"
        self.next_capture = 0.0

    # -- agent action menu ----------------------------------------------------

    def _open_agent_menu(self):
        agent = self.agents[self.sel] if self.agents else None
        if not agent:
            self.message = "no agent selected"
            return
        self.menu = {"title": "agent: %s" % _fld(agent, "name"),
                     "items": list(_AGENT_MENU), "sel": 0}

    def _draw_menu(self):
        m = self.menu
        maxy, maxx = self.stdscr.getmaxyx()
        fw = min(40, maxx - 4)
        fh = len(m["items"]) + 4
        fy, fx = max(0, (maxy - fh) // 2), max(0, (maxx - fw) // 2)
        for i in range(fh):
            self._put(fy + i, fx, " " * (fw - 1), curses.A_REVERSE)
        self._put(fy, fx + 2, _truncate_disp(" %s " % m["title"], fw - 4),
                  curses.A_REVERSE | curses.A_BOLD)
        for i, (label, _op) in enumerate(m["items"]):
            attr = curses.A_REVERSE
            prefix = "  "
            if i == m["sel"]:
                attr |= curses.A_BOLD
                prefix = "> "
            self._put(fy + 2 + i, fx + 2,
                      _truncate_disp(prefix + label, fw - 4), attr)
            self._hits.append((fy + 2 + i, fx, fw, ("menu", i)))
        self._put(fy + fh - 2, fx + 2,
                  _truncate_disp("↑↓ choose · Enter run · Esc cancel", fw - 4),
                  curses.A_REVERSE)

    def _menu_key(self, ch):
        m = self.menu
        if ch == "\x1b":
            self.menu = None
        elif ch in (curses.KEY_UP, curses.KEY_BTAB):
            m["sel"] = (m["sel"] - 1) % len(m["items"])
        elif ch in (curses.KEY_DOWN, "\t"):
            m["sel"] = (m["sel"] + 1) % len(m["items"])
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            op = m["items"][m["sel"]][1]
            self.menu = None
            self._agent_action(op)

    def _agent_action(self, op):
        agent = self.agents[self.sel] if self.agents else None
        if not agent:
            return
        aid = agent.get("id")
        name = _fld(agent, "name")
        if op == "attach":
            self._attach()
        elif op == "rename":
            self.form = {"title": "Rename agent: %s" % name, "kind": "rename",
                         "fields": ["name"], "values": {"name": name},
                         "focus": 0, "error": "", "agent_id": aid}
        elif op in ("exit", "stop", "kill", "rm"):
            self.confirm = (
                "%s agent '%s'? Enter confirm · Esc cancel" % (op, name),
                lambda: self._run_camc([op, aid]))
        else:
            self._run_camc([op, aid])

    def _submit_rename(self):
        f = self.form
        name = (f["values"].get("name") or "").strip()
        if not name:
            f["error"] = "name is required"
            return
        aid = f.get("agent_id")
        self.form = None
        self._run_camc(["update", aid, "--name", name])

    # -- command console (any camc/cam one-shot command) ----------------------

    def _open_console(self):
        self.form = {
            "title": "Run command (one-shot, 120s timeout; "
                     "interactive: use attach)",
            "kind": "command", "fields": ["command"],
            "values": {"command": ""}, "focus": 0, "error": "",
        }

    def _submit_command(self):
        f = self.form
        cmd = (f["values"].get("command") or "").strip()
        if not cmd:
            f["error"] = "command is required"
            return
        self.form = None
        self.message = "running: %s…" % cmd[:80]

        def work():
            try:
                proc = subprocess.run(
                    cmd, shell=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, universal_newlines=True,
                    timeout=120)
                text = (proc.stdout or "")
                text += "\n[exit %d] %s" % (proc.returncode, cmd)
            except subprocess.TimeoutExpired:
                text = "[timeout after 120s] %s" % cmd
            except Exception as e:
                text = "[error] %s: %s" % (cmd, e)
            self.output_title = "$ " + cmd
            self.rows = parse_ansi_rows(text)
            self.scroll = 0
            self.work_mode = "output"
            self.message = "command finished"

        threading.Thread(target=work, daemon=True).start()

    def _bar_buttons(self, y, x, w, buttons):
        cx = x
        for label, action in buttons:
            text = "[%s]" % label
            room = w - (cx - x) - 1
            if room <= 0:
                break
            text = _truncate_disp(text, room)
            self._put(y, cx, text, curses.A_REVERSE | curses.A_BOLD)
            self._hits.append((y, cx, len(text), ("button", action)))
            cx += len(text) + 1
        return cx

    def _draw_bar(self):
        y, x, h, w = self._rect("bar")
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if self.confirm:
            self._put(y, x, _truncate_disp(self.confirm[0], w - 1),
                      curses.A_REVERSE | curses.A_BOLD)
            return
        if self.form:
            self._put(y, x, _truncate_disp(
                "editing %s" % self.form["title"], w - 1), curses.A_REVERSE)
            return
        if self.menu:
            self._put(y, x, _truncate_disp(
                "menu: %s" % self.menu["title"], w - 1), curses.A_REVERSE)
            return
        self._put(y, x, " " * max(0, w - 1), curses.A_REVERSE)
        if self.view == "nodes":
            self._bar_buttons(y, x, w, (
                ("add", "add"), ("edit", "edit"), ("delete", "delete"),
                ("ping", "ping"), ("back", "back")))
            return
        if self.work_mode == "output":
            cx = self._bar_buttons(y, x, w, (
                ("back", "back"), ("cmd", "cmd"), ("quit", "quit")))
        else:
            cx = self._bar_buttons(y, x, w, (
                ("attach", "attach"), ("actions", "actions"),
                ("cmd", "cmd"), ("refresh", "refresh"), ("quit", "quit")))
        prompt = " send> %s" % self.input_buf
        room = w - (cx - x) - 1
        if room <= 0:
            return
        shown = _truncate_disp(prompt, room)
        self._put(y, cx, shown, curses.A_REVERSE)
        if self.agents:
            try:
                curses.curs_set(1)
                self.stdscr.move(y, min(cx + _disp_width(shown), w - 2))
            except curses.error:
                pass

    def _draw(self):
        self._layout = layout(*self.stdscr.getmaxyx())
        self.stdscr.erase()
        self._hits = []
        narrow = self._layout["narrow"]
        if not narrow or self.narrow_view == "list":
            self._draw_info()
            if self.view == "nodes":
                self._draw_nodes()
            else:
                self._draw_list()
        if not narrow or self.narrow_view == "work":
            if self.view == "nodes":
                self._draw_node_detail()
            else:
                self._draw_work()
        self._draw_bar()
        if self.form:
            self._draw_form()
        if self.menu:
            self._draw_menu()
        self.stdscr.noutrefresh()
        curses.doupdate()

    # -- input handling -----------------------------------------------------

    def _attach(self):
        agent = self.agents[self.sel] if self.agents else None
        session = self._session_of(agent) if agent else None
        if not session:
            self.message = "no tmux session for this agent"
            return
        from camc_pkg.transport import _find_tmux_socket, _tmux_bin_for_session
        sock = _find_tmux_socket(session)
        tmux = _tmux_bin_for_session(session)
        cmd = [tmux, "-S", sock, "attach", "-t", session] if sock else \
            [tmux, "attach", "-t", session]
        curses.endwin()
        try:
            subprocess.call(cmd)
        except OSError as e:
            self.message = "attach failed: %s" % e
        finally:
            curses.reset_prog_mode()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.stdscr.keypad(True)
            self.next_capture = 0.0
            self.scroll = 0

    def _send_input(self):
        text = self.input_buf
        self.input_buf = ""
        if not text.strip():
            return
        agent = self.agents[self.sel] if self.agents else None
        session = self._session_of(agent) if agent else None
        if not session:
            self.message = "no tmux session for this agent"
            return
        from camc_pkg.transport import tmux_send_input
        ok = tmux_send_input(session, text, send_enter=True)
        self.message = "sent." if ok else "send failed"
        self.next_capture = 0.0

    def _key(self, ch):
        if ch == "\x18":  # Ctrl+X
            raise _Quit
        if self.menu:
            self._menu_key(ch)
            return
        if self.form:
            self._form_key(ch)
            return
        if self.confirm:
            _, on_yes = self.confirm
            if ch == "\x1b":
                self.confirm = None
                self.message = "cancelled"
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                self.confirm = None
                on_yes()
            return
        if ch == curses.KEY_MOUSE:
            self._mouse()
        elif ch == "\x1b":
            self.message = ""
            if self.input_buf:
                self.input_buf = ""
            elif self.view == "nodes":
                self._toggle_view()
            elif self.work_mode == "output":
                self.work_mode = "agent"
                self.next_capture = 0.0
        elif ch == "\t":
            self.narrow_view = "work" if self.narrow_view == "list" else "list"
        elif self.view == "nodes":
            self._node_key(ch)
        elif ch == curses.KEY_UP:
            self.sel = max(0, self.sel - 1)
            self.scroll = 0
            self.next_capture = 0.0
        elif ch == curses.KEY_DOWN:
            self.sel = min(max(0, len(self.agents) - 1), self.sel + 1)
            self.scroll = 0
            self.next_capture = 0.0
        elif ch == curses.KEY_PPAGE:
            self.scroll += max(1, self._rect("work")[2] - 1)
            self._clamp_scroll()
        elif ch == curses.KEY_NPAGE:
            self.scroll -= max(1, self._rect("work")[2] - 1)
            self._clamp_scroll()
        elif ch == curses.KEY_HOME:
            self.scroll = len(self.rows)
            self._clamp_scroll()
        elif ch == curses.KEY_END:
            self.scroll = 0
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.input_buf = self.input_buf[:-1]
            self.message = ""
        elif ch == "\x15":  # Ctrl+U clears the composer
            self.input_buf = ""
            self.message = ""
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            if self.input_buf:
                self._send_input()
            elif self.work_mode == "output":
                self.work_mode = "agent"
                self.next_capture = 0.0
            else:
                self._open_agent_menu()
        elif ch == ":" and not self.input_buf:
            self._open_console()
        elif isinstance(ch, str) and ch.isprintable() and self.agents:
            self.input_buf += ch
            self.message = ""

    # -- main loop ----------------------------------------------------------

    def _main(self, stdscr):
        self.stdscr = stdscr
        self.palette = _Palette()
        stdscr.keypad(True)
        stdscr.timeout(TICK_MS)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        try:
            curses.mouseinterval(0)
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._layout = layout(*stdscr.getmaxyx())
        self._refresh_agents()
        self._refresh_nodes()
        self._recapture()
        self.next_list = time.monotonic() + REFRESH_LIST_S
        while True:
            now = time.monotonic()
            idle = self.form or self.confirm or self.menu
            if not idle:
                if now >= self.next_list:
                    self._refresh_agents()
                    self._refresh_nodes()
                    self.next_list = now + REFRESH_LIST_S
                if (self.view == "agents" and self.work_mode == "agent"
                        and now >= self.next_capture):
                    self._recapture()
                    self.next_capture = now + self.refresh_s
            self._draw()
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue  # tick timeout
            if ch == curses.KEY_RESIZE:
                continue
            self._key(ch)

    def run(self):
        locale.setlocale(locale.LC_ALL, "")
        curses.wrapper(self._main)


class _Quit(Exception):
    pass


def tui_main():
    if curses is None:
        sys.stderr.write(
            "cam tui: the curses module is unavailable in this Python "
            "install (on Debian/Ubuntu: apt install python3-curses)\n")
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stderr.write("cam tui: needs a real terminal (tty)\n")
        return 1
    tui = Tui()
    try:
        tui.run()
    except _Quit:
        pass
    except KeyboardInterrupt:
        pass
    return 0
