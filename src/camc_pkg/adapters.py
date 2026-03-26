"""Adapter configuration: TOML parser, embedded configs, AdapterConfig class."""

import os
import re
import sys

from camc_pkg import CONFIGS_DIR, log
from camc_pkg.utils import compile_pattern

# ---------------------------------------------------------------------------
# Embedded adapter configs (written to ~/.cam/configs/ on `camc init`)
# ---------------------------------------------------------------------------

_EMBEDDED_CONFIGS = {
    "claude.toml": r"""# Claude Code adapter — interactive mode with --allowed-tools.
[adapter]
name = "claude"
display_name = "Claude Code"

[launch]
command = ["claude", "--allowed-tools", "Bash,Edit,Read,Write,Glob,Grep,WebFetch,TodoWrite,NotebookEdit"]
prompt_after_launch = true
startup_wait = 30.0
strip_ansi = true

ready_pattern = "^[❯>]"
ready_flags = ["MULTILINE"]

[state]
strategy = "last"
recent_chars = 2000

[[state.patterns]]
state = "planning"
pattern = "(● Read\\(|● Glob\\(|● Grep\\(|● WebFetch\\(|● WebSearch\\(|Thinking|Analyzing)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "editing"
pattern = "(● Edit\\(|● Write\\(|● NotebookEdit\\()"

[[state.patterns]]
state = "testing"
pattern = "(● Bash\\(|Running tests|pytest|npm test|npm run)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "committing"
pattern = "(git commit|git push|gh pr create)"
flags = ["IGNORECASE"]

[completion]
strategy = "prompt_count"

prompt_pattern = "^[❯>]"
prompt_flags = ["MULTILINE"]
prompt_count_threshold = 2

fallback_summary_pattern = "✻ .+ for \\d+"

[[confirm]]
pattern = "Enter to (confirm|select).*Esc to cancel"
flags = ["IGNORECASE", "DOTALL"]
response = ""
send_enter = true

[[confirm]]
pattern = "Do\\s+you\\s+want\\s+to\\s+proceed"
flags = ["IGNORECASE"]
response = ""
send_enter = true

[[confirm]]
pattern = "1\\.\\s*(Yes|Allow)"
flags = ["IGNORECASE"]
response = ""
send_enter = true

[[confirm]]
pattern = "Allow\\s+(once|always)"
flags = ["IGNORECASE"]
response = ""
send_enter = true

[[confirm]]
pattern = "\\(y/n\\)|\\[Y/n\\]|\\[y/N\\]"
flags = ["IGNORECASE"]
response = "y"
send_enter = true

[probe]
char = "Z"
confirm_response = ""
confirm_send_enter = true
wait = 0.3
idle_threshold = 2

[monitor]
confirm_cooldown = 5.0
confirm_sleep = 0.5
completion_stable = 3.0
health_check_interval = 15
empty_threshold = 3
auto_exit = false
exit_action = "kill_session"
exit_command = "/exit"
""",
    "codex.toml": r"""# Codex adapter — interactive mode.
[adapter]
name = "codex"
display_name = "OpenAI Codex"

[launch]
command = ["codex", "{prompt}"]
prompt_after_launch = false
startup_wait = 0.0

[state]
strategy = "first"
recent_chars = 2000

[[state.patterns]]
state = "planning"
pattern = "(Thinking|Planning|Analyzing|Reading|Searching|Reviewing)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "editing"
pattern = "(Editing|Writing|Creating|Modifying|Applying|Patching)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "testing"
pattern = "(Running|Testing|Executing|Verifying|npm test|pytest|cargo test)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "committing"
pattern = "(Committing|Pushing|git commit|git push|Creating PR)"
flags = ["IGNORECASE"]

[completion]
strategy = "prompt_count"

prompt_pattern = "^›"
prompt_flags = ["MULTILINE"]
prompt_count_threshold = 2

error_pattern = "(Error:|error:|FAILED|fatal:|Exception|command not found)"
error_flags = ["IGNORECASE"]

[[confirm]]
pattern = "1\\.\\s*(Yes,?\\s*)?allow Codex to work"
flags = ["IGNORECASE"]
response = "1"
send_enter = true

[[confirm]]
pattern = "(Apply|Accept|Approve|Continue|Proceed).*\\[Y/n\\]"
flags = ["IGNORECASE"]
response = "y"
send_enter = true

[[confirm]]
pattern = "(Apply|Accept|Approve|Continue|Proceed).*\\[y/N\\]"
flags = ["IGNORECASE"]
response = "y"
send_enter = true

[[confirm]]
pattern = "Press Enter"
flags = ["IGNORECASE"]
response = ""
send_enter = true

[probe]
char = "Z"
confirm_response = ""
confirm_send_enter = true
wait = 0.3
idle_threshold = 2

[monitor]
confirm_cooldown = 5.0
confirm_sleep = 0.5
completion_stable = 3.0
health_check_interval = 15
empty_threshold = 3
auto_exit = false
exit_action = "kill_session"
exit_command = "/exit"
""",
    "cursor.toml": r"""# Cursor Agent adapter — interactive mode.
[adapter]
name = "cursor"
display_name = "Cursor Agent"

[launch]
command = ["agent", "--workspace", "{path}"]
prompt_after_launch = true
startup_wait = 15.0
strip_ansi = true

ready_pattern = "→"
ready_flags = ["MULTILINE"]

[state]
strategy = "last"
recent_chars = 2000

[[state.patterns]]
state = "planning"
pattern = "(⬢ Read|⬢ Glob|⬢ Grep|Thinking|Analyzing)"

[[state.patterns]]
state = "editing"
pattern = "(⬢ Edit|⬢ Write)"

[[state.patterns]]
state = "testing"
pattern = "(\\$ .+in /|Running tests|pytest|npm test)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "committing"
pattern = "(git commit|git push|gh pr create)"
flags = ["IGNORECASE"]

[completion]
strategy = "prompt_count"

prompt_pattern = "→"
prompt_flags = ["MULTILINE"]
prompt_count_threshold = 2

fallback_summary_pattern = "·\\s*\\d+(\\.\\d+)?%"

[[confirm]]
pattern = "Trust this workspace"
response = ""
send_enter = true

[[confirm]]
pattern = "\\(y\\)"
response = ""
send_enter = true

[[confirm]]
pattern = "Run \\(always"
response = ""
send_enter = true

[probe]
char = "Z"
confirm_response = ""
confirm_send_enter = true
wait = 0.3
idle_threshold = 2

[monitor]
confirm_cooldown = 5.0
confirm_sleep = 0.5
completion_stable = 3.0
health_check_interval = 15
empty_threshold = 3
auto_exit = false
exit_action = "kill_session"
exit_command = "/exit"
""",
}


# ===========================================================================
# Minimal TOML parser (subset: strings, bools, numbers, arrays, tables)
# ===========================================================================

def _parse_toml(text):
    root = {}
    current = root
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[\[([^\]]+)\]\]$", line)
        if m:
            key_path = m.group(1).strip().split(".")
            target = root
            for k in key_path[:-1]:
                if k not in target:
                    target[k] = {}
                target = target[k]
            last = key_path[-1]
            if last not in target:
                target[last] = []
            new_item = {}
            target[last].append(new_item)
            current = new_item
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            key_path = m.group(1).strip().split(".")
            target = root
            for k in key_path:
                if k not in target:
                    target[k] = {}
                target = target[k]
            current = target
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', line)
        if m:
            current[m.group(1)] = _parse_toml_value(m.group(2).strip())
    return root


def _parse_toml_value(s):
    if s.startswith('"'):
        i, result = 1, []
        while i < len(s):
            c = s[i]
            if c == "\\":
                i += 1
                if i < len(s):
                    esc = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(s[i], "\\" + s[i])
                    result.append(esc)
            elif c == '"':
                break
            else:
                result.append(c)
            i += 1
        return "".join(result)
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith("["):
        inner = s[1:].rstrip()
        if inner.endswith("]"):
            inner = inner[:-1]
        return [_parse_toml_value(p.strip()) for p in inner.split(",") if p.strip()]
    val = s.split("#")[0].strip()
    try:
        return float(val) if "." in val else int(val)
    except ValueError:
        return val


def load_toml(path):
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (ImportError, ModuleNotFoundError):
        pass
    try:
        import tomli
        with open(path, "rb") as f:
            return tomli.load(f)
    except (ImportError, ModuleNotFoundError):
        pass
    with open(path, "r") as f:
        return _parse_toml(f.read())


# ===========================================================================
# Adapter config parser
# ===========================================================================

class AdapterConfig(object):
    """Parsed adapter config from a TOML dict."""

    def __init__(self, config):
        launch = config.get("launch", {})
        self.strip_ansi = launch.get("strip_ansi", False)
        self.command = launch.get("command", [])

        rp = launch.get("ready_pattern")
        self.ready_pattern = compile_pattern(rp, launch.get("ready_flags")) if rp else None
        self.prompt_after_launch = launch.get("prompt_after_launch", False)
        self.startup_wait = float(launch.get("startup_wait", 2.0))

        state_cfg = config.get("state", {})
        self.state_strategy = state_cfg.get("strategy", "first")
        self.state_recent_chars = state_cfg.get("recent_chars", 2000)
        self.state_patterns = []
        for entry in state_cfg.get("patterns", []):
            self.state_patterns.append((
                entry["state"],
                compile_pattern(entry["pattern"], entry.get("flags")),
            ))

        comp = config.get("completion", {})
        self.completion_strategy = comp.get("strategy", "process_exit")
        self.completion_recent_chars = comp.get("recent_chars", 500)
        self.min_output_length = comp.get("min_output_length", 100)

        cp = comp.get("completion_pattern")
        self.completion_pattern = compile_pattern(cp, comp.get("completion_flags")) if cp else None
        ep = comp.get("error_pattern")
        self.error_pattern = compile_pattern(ep, comp.get("error_flags")) if ep else None
        self.error_search_full = comp.get("error_search_full", True)

        sp = comp.get("shell_prompt_pattern")
        self.shell_prompt_pattern = compile_pattern(sp, comp.get("shell_prompt_flags")) if sp else None

        pp = comp.get("prompt_pattern")
        self.prompt_pattern = compile_pattern(pp, comp.get("prompt_flags")) if pp else None
        self.prompt_count_threshold = comp.get("prompt_count_threshold", 2)
        fp = comp.get("fallback_summary_pattern")
        self.fallback_summary_pattern = compile_pattern(fp, comp.get("fallback_summary_flags")) if fp else None

        self.confirm_rules = []
        for rule in config.get("confirm", []):
            self.confirm_rules.append((
                compile_pattern(rule["pattern"], rule.get("flags")),
                rule.get("response", ""),
                rule.get("send_enter", True),
            ))

        probe_cfg = config.get("probe", {})
        self.probe_char = probe_cfg.get("char", "Z")
        self.probe_confirm_response = probe_cfg.get("confirm_response", "")
        self.probe_confirm_send_enter = probe_cfg.get("confirm_send_enter", True)
        self.probe_wait = float(probe_cfg.get("wait", 0.3))
        self.probe_idle_threshold = int(probe_cfg.get("idle_threshold", 2))

        mon_cfg = config.get("monitor", {})
        self.confirm_cooldown = float(mon_cfg.get("confirm_cooldown", 5.0))
        self.confirm_sleep = float(mon_cfg.get("confirm_sleep", 0.5))
        self.completion_stable = float(mon_cfg.get("completion_stable", 3.0))
        self.health_check_interval = float(mon_cfg.get("health_check_interval", 15))
        self.empty_threshold = int(mon_cfg.get("empty_threshold", 3))
        self.auto_exit = bool(mon_cfg.get("auto_exit", False))
        self.exit_action = mon_cfg.get("exit_action", "kill_session")
        self.exit_command = mon_cfg.get("exit_command", "/exit")


def _load_config(tool):
    """Load adapter config for a tool, from file or embedded."""
    toml_path = os.path.join(CONFIGS_DIR, "%s.toml" % tool)
    if os.path.exists(toml_path):
        return AdapterConfig(load_toml(toml_path))
    # Fall back to embedded config
    key = "%s.toml" % tool
    if key in _EMBEDDED_CONFIGS:
        return AdapterConfig(_parse_toml(_EMBEDDED_CONFIGS[key]))
    sys.stderr.write("Error: no config for tool '%s'\n" % tool)
    sys.stderr.write("Available: %s\n" % ", ".join(
        k.replace(".toml", "") for k in _EMBEDDED_CONFIGS))
    sys.exit(1)
