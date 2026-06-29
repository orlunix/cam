"""Build-time registry for shell prelude hooks embedded in generated camc.

Hooks are POSIX sh fragments stored under this package (see manifest.json).
build_camc.py calls :func:`render_prelude` to stitch them into the camc
polyglot before the Python body.

Disable at build time via environment variable::

    CAMC_PRELUDE_DISABLE=capture   # omit capture hook only
    CAMC_PRELUDE_DISABLE=all       # omit every hook (exec python immediately)

Hook return protocol (each hook function)::

    0  handled success — stop prelude, exit camc with 0
    1  not handled — try next hook / fall through to Python
    2  usage error — stop prelude, exit camc with 2
    *  handled failure — stop prelude, exit camc with that code
"""

import json
import os

PRELUDE_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(PRELUDE_DIR, "manifest.json")

# Exit codes shared with generated shell dispatcher.
RC_HANDLED_SUCCESS = 0
RC_NOT_HANDLED = 1
RC_USAGE_ERROR = 2


def _load_manifest():
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("prelude manifest must be a JSON array")
    return data


def parse_disable_env(value=None):
    """Return a set of disabled hook names from CAMC_PRELUDE_DISABLE."""
    if value is None:
        value = os.environ.get("CAMC_PRELUDE_DISABLE", "")
    value = (value or "").strip()
    if not value:
        return set()
    if value.lower() == "all":
        return {hook["name"] for hook in _load_manifest()}
    return {part.strip() for part in value.split(",") if part.strip()}


def _read_hook_script(script_name):
    path = os.path.join(PRELUDE_DIR, script_name)
    with open(path, "r", encoding="utf-8") as f:
        lines = []
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#") and not stripped.startswith("#!"):
                continue
            if not stripped and not lines:
                continue
            lines.append(line.rstrip("\n"))
        while lines and not lines[-1].strip():
            lines.pop()
        return lines


def enabled_hooks(disabled=None):
    """Manifest entries minus disabled hook names."""
    disabled = parse_disable_env() if disabled is None else set(disabled)
    hooks = []
    for hook in _load_manifest():
        name = hook.get("name")
        if not name:
            raise ValueError("prelude hook missing name: %r" % (hook,))
        if name in disabled:
            continue
        if not hook.get("script") or not hook.get("function"):
            raise ValueError("prelude hook %r missing script/function" % name)
        hooks.append(hook)
    return hooks


def _dispatcher_lines(hooks):
    """Emit shell that runs each hook and interprets return codes."""
    lines = [
        "_camc_prelude_dispatch() {",
        "    _camc_rc=%d" % RC_NOT_HANDLED,
    ]
    for hook in hooks:
        fn = hook["function"]
        lines.append('    %s "$@"' % fn)
        lines.append("    _camc_rc=$?")
        lines.append('    case "$_camc_rc" in')
        lines.append("        %d) return 0 ;;" % RC_HANDLED_SUCCESS)
        lines.append("        %d) ;;" % RC_NOT_HANDLED)
        lines.append("        %d) return %d ;;" % (RC_USAGE_ERROR, RC_USAGE_ERROR))
        lines.append('        *) return "$_camc_rc" ;;')
        lines.append("    esac")
    lines.append("    return %d" % RC_NOT_HANDLED)
    lines.append("}")
    lines.append("")
    lines.append('_camc_prelude_dispatch "$@"')
    lines.append("_camc_rc=$?")
    lines.append('case "$_camc_rc" in')
    lines.append("    %d) exit 0 ;;" % RC_HANDLED_SUCCESS)
    lines.append("    %d) ;;" % RC_NOT_HANDLED)
    lines.append("    %d) exit %d ;;" % (RC_USAGE_ERROR, RC_USAGE_ERROR))
    lines.append('    *) exit "$_camc_rc" ;;')
    lines.append("esac")
    return lines


def render_prelude(disabled=None):
    """Return sh lines for the camc polyglot prelude (excluding shebang)."""
    hooks = enabled_hooks(disabled)
    lines = ["''':'"]
    for hook in hooks:
        lines.extend(_read_hook_script(hook["script"]))
        lines.append("")
    if hooks:
        lines.extend(_dispatcher_lines(hooks))
        lines.append("")
    lines.append('exec python3 "$0" "$@"')
    lines.append(":'''")
    return lines


def render_prelude_text(disabled=None):
    return "\n".join(render_prelude(disabled)) + "\n"
