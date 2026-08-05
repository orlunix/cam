"""Per-agent system prompt injection.

camc writes a marker-delimited block into the tool-specific
auto-loaded file (CLAUDE.md / AGENTS.md) in the workdir before
launch. The tool picks it up via its normal auto-load mechanism;
no API integration needed.

See docs/system-prompt-feature.md for the full design.
"""

import os
import re

# Tool → filename that the tool auto-loads from workdir.
_TOOL_FILES = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "cursor": "AGENTS.md",
}

_CODEX_HOST_ESCALATION_ID = "codex-host-escalation"
_CODEX_HOST_ESCALATION_GUIDANCE = """## CAMC host execution

When an important shell tool call fails specifically because the sandbox
denied host access (for example `Operation not permitted` or `Read-only file
system`), retry the exact same command once using
`sandbox_permissions: require_escalated` with a concise justification. Do not
change the command, use `sudo`, or escalate ordinary command failures. If the
elevated retry fails, report that failure instead of retrying again."""


def target_file(tool, workdir):
    """Resolve the tool-specific system-prompt file. Returns the
    absolute path, or None if the tool has no known auto-load file."""
    fname = _TOOL_FILES.get(tool)
    if not fname:
        return None
    return os.path.join(workdir, fname)


def _markers(agent_id):
    begin = "<!-- camc:%s begin -->" % agent_id
    end = "<!-- camc:%s end -->" % agent_id
    return begin, end


def _block_re(agent_id):
    begin, end = _markers(agent_id)
    return re.compile(
        re.escape(begin) + r"\n.*?\n" + re.escape(end) + r"\n?",
        re.DOTALL,
    )


def write_block(file_path, agent_id, prompt):
    """Write or replace the marker-delimited block for ``agent_id``
    in ``file_path``. Creates the file if missing, preserves any
    surrounding content, and is idempotent across re-runs."""
    begin, end = _markers(agent_id)
    block = "%s\n%s\n%s\n" % (begin, prompt.rstrip("\n"), end)
    existing = ""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                existing = f.read()
        except (IOError, OSError):
            existing = ""
    if begin in existing:
        new_text = _block_re(agent_id).sub(block, existing)
    else:
        sep = "" if not existing or existing.endswith("\n\n") else (
            "\n" if existing.endswith("\n") else "\n\n")
        new_text = existing + sep + block
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
    except OSError:
        pass
    with open(file_path, "w") as f:
        f.write(new_text)


def ensure_codex_host_escalation_guidance(workdir):
    """Ensure one CAMC-owned host-escalation block in workspace AGENTS.md.

    Equivalent user-authored guidance wins. A CAMC-managed block is updated
    in place when its canonical text changes and is byte-idempotent otherwise.
    """
    file_path = target_file("codex", workdir)
    existing = ""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                existing = f.read()
        except (IOError, OSError):
            existing = ""

    begin, end = _markers(_CODEX_HOST_ESCALATION_ID)
    canonical = "%s\n%s\n%s\n" % (
        begin, _CODEX_HOST_ESCALATION_GUIDANCE.rstrip("\n"), end)
    if begin in existing:
        if canonical in existing:
            return "existing"
        write_block(file_path, _CODEX_HOST_ESCALATION_ID,
                    _CODEX_HOST_ESCALATION_GUIDANCE)
        return "updated"

    lowered = existing.lower()
    if ("require_escalated" in lowered
            and ("operation not permitted" in lowered
                 or "read-only file system" in lowered
                 or "sandbox" in lowered)):
        return "user_defined"

    write_block(file_path, _CODEX_HOST_ESCALATION_ID,
                _CODEX_HOST_ESCALATION_GUIDANCE)
    return "created"


def strip_block(file_path, agent_id):
    """Remove the marker-delimited block for ``agent_id`` from
    ``file_path``. No-op if the file or block doesn't exist.
    Pre-existing surrounding content is preserved."""
    if not os.path.exists(file_path):
        return False
    try:
        with open(file_path, "r") as f:
            text = f.read()
    except (IOError, OSError):
        return False
    begin, _ = _markers(agent_id)
    if begin not in text:
        return False
    new_text = _block_re(agent_id).sub("", text)
    # Collapse any 3+ consecutive blank lines left behind.
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    new_text = new_text.rstrip() + ("\n" if new_text.strip() else "")
    try:
        with open(file_path, "w") as f:
            f.write(new_text)
        return True
    except (IOError, OSError):
        return False


def has_block(file_path, agent_id):
    """True iff the file contains a block for ``agent_id``."""
    if not file_path or not os.path.exists(file_path):
        return False
    try:
        with open(file_path, "r") as f:
            text = f.read()
    except (IOError, OSError):
        return False
    begin, _ = _markers(agent_id)
    return begin in text


def load_prompt_text(prompt, prompt_file):
    """Resolve either an inline prompt or a file path. Returns the
    text (stripped of trailing whitespace) or empty string. Raises
    FileNotFoundError on a missing --system-file."""
    if prompt_file:
        path = os.path.expanduser(prompt_file)
        with open(path, "r") as f:
            return f.read().rstrip()
    if prompt:
        return prompt.rstrip()
    return ""
