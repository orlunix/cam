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

_EMBEDDED_BOOT_CONFIGS = {}  # populated by build_camc.py


def _load_dev_configs_fallback():
    """When running camc from the dev source tree (python3 -m camc_pkg or
    /data/venv/bin/camc from a `pip install -e .`), _EMBEDDED_CONFIGS is
    empty because build_camc.py is what injects them. Walk up from this
    module's path to find the repo's src/cam/adapters/configs/ and read
    the TOML files from disk. Silent no-op outside the dev tree.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # .../cam/src/camc_pkg/adapters.py → up twice to .../cam/, then into
    # src/cam/adapters/configs/. build_camc.py uses the same path.
    cfg_dir = os.path.normpath(
        os.path.join(here, "..", "cam", "adapters", "configs"))
    if not os.path.isdir(cfg_dir):
        return
    for fname in sorted(os.listdir(cfg_dir)):
        if not fname.endswith(".toml") or fname.endswith(".boot.toml"):
            continue
        try:
            with open(os.path.join(cfg_dir, fname)) as f:
                _EMBEDDED_CONFIGS[fname] = f.read()
        except OSError:
            pass


# Populate from source tree on import when the build-time injection was
# skipped (dev mode). Only fires if _EMBEDDED_CONFIGS stayed empty — a
# built single-file camc with injected configs is unaffected.
if not _EMBEDDED_CONFIGS:
    _load_dev_configs_fallback()


def _load_dev_boot_configs_fallback():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_dir = os.path.normpath(
        os.path.join(here, "..", "cam", "adapters", "configs"))
    if not os.path.isdir(cfg_dir):
        return
    for fname in sorted(os.listdir(cfg_dir)):
        if not fname.endswith(".boot.toml"):
            continue
        try:
            with open(os.path.join(cfg_dir, fname)) as f:
                _EMBEDDED_BOOT_CONFIGS[fname] = f.read()
        except OSError:
            pass


if not _EMBEDDED_BOOT_CONFIGS:
    _load_dev_boot_configs_fallback()


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


def _split_toml_top_commas(s):
    # Split on commas that are NOT inside a double-quoted string.
    # Naive .split(",") shatters elements like "Bash,Edit,Read,..." into
    # separate args; this preserves them.
    parts, buf, i, in_str = [], [], 0, False
    while i < len(s):
        c = s[i]
        if in_str:
            if c == "\\" and i + 1 < len(s):
                buf.append(c); buf.append(s[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            buf.append(c); i += 1; continue
        if c == '"':
            in_str = True; buf.append(c); i += 1; continue
        if c == ",":
            parts.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    if buf:
        parts.append("".join(buf))
    return parts


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
        return [_parse_toml_value(p.strip())
                for p in _split_toml_top_commas(inner) if p.strip()]
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
        self.prompt_submit_delay = float(launch.get("prompt_submit_delay", 0.0))

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

        # Note (2026-06-10): the legacy [probe] block (char/wait/
        # idle_threshold) and the [monitor] completion_stable /
        # empty_threshold / probe_stable / probe_cooldown fields were
        # removed from the TOML schema as part of the TOML-only
        # auto-confirm simplification — the camc monitor no longer
        # consults any of them. ``cam.client.AdapterConfig`` (the
        # legacy CAM client) keeps its own parsing of these fields
        # for backwards compatibility; the defaults there match the
        # values previously written into shared TOMLs, so removing
        # them is behavior-preserving for the legacy path.
        mon_cfg = config.get("monitor", {})
        bp = mon_cfg.get("busy_pattern")
        self.busy_pattern = compile_pattern(bp, mon_cfg.get("busy_flags")) if bp else None
        dp = mon_cfg.get("done_pattern")
        self.done_pattern = compile_pattern(dp, mon_cfg.get("done_flags")) if dp else None
        self.confirm_cooldown = float(mon_cfg.get("confirm_cooldown", 5.0))
        self.confirm_recent_lines = int(mon_cfg.get("confirm_recent_lines", 8))
        self.confirm_sleep = float(mon_cfg.get("confirm_sleep", 0.5))
        self.health_check_interval = float(mon_cfg.get("health_check_interval", 15))
        # Idle stability threshold (hash0 unchanged this long → idle).
        # Default 60s preserves the prior hardcoded threshold exactly.
        self.idle_stable_seconds = float(mon_cfg.get("idle_stable_seconds", 60.0))
        self.auto_exit = bool(mon_cfg.get("auto_exit", False))
        self.exit_action = mon_cfg.get("exit_action", "kill_session")
        self.exit_command = mon_cfg.get("exit_command", "/exit")

        # ----- Readiness (F-08 adapter-owned) -----
        # Tool-specific readiness policy. When the [readiness] block is
        # present, runtime_env.check_tool_readiness will prefer it over
        # the hardcoded _TOOL_SPECS fallback so each tool can own its
        # auth / version / file-existence rules in TOML rather than in
        # Python. Shape (all fields optional):
        #
        #   [readiness]
        #   binary           = "claude"
        #   version_args     = ["--version"]
        #   version_required = true
        #   install_hint     = "npm install -g @anthropic-ai/claude-code"
        #
        #   [[readiness.required_files]]
        #   path  = "~/.claude.json"
        #   label = "Claude auth file"
        #
        #   [[readiness.optional_files]]
        #   path  = "~/.codex/auth.json"
        #
        #   [[readiness.optional_env]]
        #   name  = "OPENAI_API_KEY"
        #
        # Reserved-but-disabled keys (e.g. auth_probe) MAY be parsed
        # here in the future; right now we only carry shapes that
        # actually have effect. self.readiness is None when no block
        # is configured — callers fall back to _TOOL_SPECS.
        rd = config.get("readiness")
        if isinstance(rd, dict):
            req_files = []
            for entry in (rd.get("required_files") or []):
                if isinstance(entry, dict) and entry.get("path"):
                    req_files.append({
                        "path":  str(entry.get("path")),
                        "label": str(entry.get("label") or ""),
                    })
            opt_files = []
            for entry in (rd.get("optional_files") or []):
                if isinstance(entry, dict) and entry.get("path"):
                    opt_files.append({
                        "path":  str(entry.get("path")),
                        "label": str(entry.get("label") or ""),
                    })
            opt_env = []
            for entry in (rd.get("optional_env") or []):
                if isinstance(entry, dict) and entry.get("name"):
                    opt_env.append({"name": str(entry.get("name"))})
            self.readiness = {
                "binary":           rd.get("binary") or "",
                "version_args":     list(rd.get("version_args") or ["--version"]),
                "version_required": bool(rd.get("version_required", True)),
                "install_hint":     rd.get("install_hint") or "",
                "required_files":   req_files,
                "optional_files":   opt_files,
                "optional_env":     opt_env,
            }
        else:
            self.readiness = None


# Keys whose list value should APPEND (user-extension lists) rather
# than override. Anything not in this set is treated as a scalar-like
# override even when the value happens to be a list. Without this
# distinction, install_default_configs() (which writes a FULL embedded
# snapshot to ~/.cam/configs/<tool>.toml) would cause _load_config to
# double every list field on merge — e.g. readiness.version_args ended
# up as ['--version', '--version'], state.patterns doubled, etc.
_APPEND_LIST_KEYS = frozenset({"confirm"})


def _merge_toml(base, override):
    """Merge ``override`` TOML dict into ``base``.

    - For keys in ``_APPEND_LIST_KEYS`` (currently ``confirm``), lists
      APPEND to the embedded defaults — this is what lets a user-side
      ``[[confirm]]`` block extend the rule set without duplicating the
      whole adapter config.
    - Every other list value (readiness.version_args, state.patterns,
      *.flags, required_files, optional_files, optional_env, …)
      OVERRIDES the embedded value. Treating these like ``confirm``
      would silently double their entries each time merge ran.
    - Dicts merge recursively so per-section overrides still work.
    """
    for k, v in override.items():
        if (k in _APPEND_LIST_KEYS
                and isinstance(v, list)
                and isinstance(base.get(k), list)):
            base[k] = base[k] + v
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge_toml(base[k], v)
        else:
            base[k] = v
    return base


def install_default_configs(target_dir=None, force=False):
    """Write embedded adapter TOMLs to ``target_dir`` (default
    :data:`CONFIGS_DIR`, i.e. ``~/.cam/configs/``).

    For each adapter in ``_EMBEDDED_CONFIGS`` (claude / codex / cursor):

      * If ``<name>.toml`` exists in ``target_dir`` AND ``force`` is
        false → leave the user's copy alone (status ``skipped_exists``).
      * Otherwise → write the embedded TOML verbatim
        (``created`` / ``overwritten``).

    The on-disk file serves two purposes:
      1. It is the **template** users edit to add custom
         ``[[confirm]]`` rules. ``_load_config(tool)`` merges this file
         on top of the embedded defaults, so a custom rule simply
         appends to the active rule set without touching Python.
      2. It is a stable snapshot of the embedded TOML — handy when a
         user wants to read or tweak any other adapter field
         (busy_pattern, state patterns, monitor config, etc.).

    Idempotent and side-effect-light: never overwrites an
    edited file unless the caller explicitly asks via ``force=True``.
    Returns a dict mapping filename → action string.
    """
    out_dir = target_dir or CONFIGS_DIR
    try:
        os.makedirs(out_dir)
    except OSError:
        pass
    results = {}
    for fname, content in _EMBEDDED_CONFIGS.items():
        path = os.path.join(out_dir, fname)
        if os.path.exists(path) and not force:
            results[fname] = "skipped_exists"
            continue
        existed = os.path.exists(path)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except OSError as e:
            log.warning("install_default_configs: %s: %s", path, e)
            results[fname] = "error:%s" % e
            continue
        results[fname] = "overwritten" if existed else "created"
    return results


def install_default_boot_configs(target_dir=None, force=False):
    """Write embedded ``<tool>.boot.toml`` files to CONFIGS_DIR."""
    out_dir = target_dir or CONFIGS_DIR
    try:
        os.makedirs(out_dir)
    except OSError:
        pass
    results = {}
    for fname, content in _EMBEDDED_BOOT_CONFIGS.items():
        path = os.path.join(out_dir, fname)
        if os.path.exists(path) and not force:
            results[fname] = "skipped_exists"
            continue
        existed = os.path.exists(path)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except OSError as e:
            log.warning("install_default_boot_configs: %s: %s", path, e)
            results[fname] = "error:%s" % e
            continue
        results[fname] = "overwritten" if existed else "created"
    return results


def _load_boot_config(tool):
    """Load boot config for initializing phase (``<tool>.boot.toml``)."""
    key = "%s.boot.toml" % tool
    if key not in _EMBEDDED_BOOT_CONFIGS:
        return None
    config = _parse_toml(_EMBEDDED_BOOT_CONFIGS[key])
    toml_path = os.path.join(CONFIGS_DIR, key)
    if os.path.exists(toml_path):
        try:
            ext = load_toml(toml_path)
            _merge_toml(config, ext)
            log.info("Merged external boot config: %s", toml_path)
        except Exception as e:
            log.warning("Failed to load external boot config %s: %s", toml_path, e)
    return AdapterConfig(config)


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
