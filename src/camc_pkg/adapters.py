"""Adapter configuration: TOML parser, embedded configs, AdapterConfig class."""

import os
import re
import sys

from camc_pkg import CONFIGS_DIR, log
from camc_pkg.utils import compile_pattern

# ---------------------------------------------------------------------------
# Embedded adapter configs — injected by build_camc.py at build time.
# DO NOT edit here. Edit the TOML source files in src/cam/adapters/configs/.
# ---------------------------------------------------------------------------

_EMBEDDED_CONFIGS = {}  # populated by build_camc.py


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
    if s.startswith("["):
        inner = s[1:].rstrip()
        if inner.endswith("]"):
            inner = inner[:-1]
        return [_parse_toml_value(p.strip()) for p in inner.split(",") if p.strip()]
    # Strip inline comments before checking booleans/numbers
    val = s.split("#")[0].strip()
    if val == "true":
        return True
    if val == "false":
        return False
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
        self.auto_permission_mode = launch.get("auto_permission_mode", False)

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
        self.probe_wait = float(probe_cfg.get("wait", 0.3))
        self.probe_idle_threshold = int(probe_cfg.get("idle_threshold", 2))

        mon_cfg = config.get("monitor", {})
        bp = mon_cfg.get("busy_pattern")
        self.busy_pattern = compile_pattern(bp, mon_cfg.get("busy_flags")) if bp else None
        dp = mon_cfg.get("done_pattern")
        self.done_pattern = compile_pattern(dp, mon_cfg.get("done_flags")) if dp else None
        self.confirm_cooldown = float(mon_cfg.get("confirm_cooldown", 5.0))
        self.confirm_sleep = float(mon_cfg.get("confirm_sleep", 0.5))
        self.completion_stable = float(mon_cfg.get("completion_stable", 3.0))
        self.health_check_interval = float(mon_cfg.get("health_check_interval", 15))
        self.empty_threshold = int(mon_cfg.get("empty_threshold", 3))
        self.auto_exit = bool(mon_cfg.get("auto_exit", False))
        self.exit_action = mon_cfg.get("exit_action", "kill_session")
        self.exit_command = mon_cfg.get("exit_command", "/exit")
        self.probe_stable = float(mon_cfg.get("probe_stable", 10.0))
        self.probe_cooldown = float(mon_cfg.get("probe_cooldown", 20.0))


def _merge_toml(base, override):
    """Merge override TOML dict into base. Lists (e.g. [[confirm]]) are appended."""
    for k, v in override.items():
        if isinstance(v, list) and isinstance(base.get(k), list):
            base[k] = base[k] + v  # append (e.g. extra confirm rules)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge_toml(base[k], v)
        else:
            base[k] = v
    return base


def _load_config(tool):
    """Load adapter config for a tool.

    Always loads embedded config first. If an external TOML file exists
    at ~/.cam/configs/<tool>.toml, merges it on top (lists like [[confirm]]
    are appended, scalar values are overridden).
    """
    key = "%s.toml" % tool
    if key not in _EMBEDDED_CONFIGS:
        sys.stderr.write("Error: no config for tool '%s'\n" % tool)
        sys.stderr.write("Available: %s\n" % ", ".join(
            k.replace(".toml", "") for k in _EMBEDDED_CONFIGS))
        sys.exit(1)

    config = _parse_toml(_EMBEDDED_CONFIGS[key])

    # Merge external overrides if present
    toml_path = os.path.join(CONFIGS_DIR, "%s.toml" % tool)
    if os.path.exists(toml_path):
        try:
            ext = load_toml(toml_path)
            _merge_toml(config, ext)
            log.info("Merged external config: %s", toml_path)
        except Exception as e:
            log.warning("Failed to load external config %s: %s", toml_path, e)

    return AdapterConfig(config)
