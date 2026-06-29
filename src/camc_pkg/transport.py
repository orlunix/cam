"""Transport layer: tmux session management (create, capture, send, kill)."""

import hashlib
import os
import shlex
import shutil
import subprocess

from camc_pkg import CAM_DIR, SOCKETS_DIR, log
from camc_pkg.utils import strip_ansi, _run


# ---------------------------------------------------------------------------
# camc-owned tmux config template (2026-06-23 PDX hardening)
# ---------------------------------------------------------------------------
#
# camc launches every agent into its own tmux server (private socket via
# -S). The new tmux server still reads ~/.tmux.conf by default; on PDX
# a user .tmux.conf has been observed to crash the server during
# startup-command injection. Pointing the new server at a small
# camc-managed config with `-f <path>` makes camc tmux behavior
# independent of user config.
#
# v1 template content kept tmux 2.7 compatible (no `set-option -ga`,
# no `assume-paste-time`). The 'camc-template-version' header lets the
# next slice auto-refresh the template when we bump the version; the
# 'camc-template-sha256' header tracks the hash of the managed body
# so a user-modified file is left alone.

_CAMC_TMUX_CONFIG_VERSION = 1
_CAMC_TMUX_CONFIG_BODY = (
    "# camc-managed: true\n"
    "# camc-template: tmux\n"
    "# camc-template-version: {version}\n"
    "# camc-template-sha256: {sha}\n"
    "#\n"
    "# Edit this file to override; the 'camc-managed' header is what\n"
    "# camc uses to decide whether to refresh on version bumps. Once\n"
    "# you modify the file, change or remove the sha line so camc\n"
    "# leaves it alone.\n"
    "\n"
    "set-option -g history-limit 50000\n"
    "set-option -g status off\n"
    "set-option -g mouse off\n"
    'set-option -g default-terminal "screen-256color"\n'
)


def _camc_tmux_config_path():
    return os.path.join(CAM_DIR, "configs", "tmux.conf")


def _camc_tmux_body_sha(text):
    """Hash a tmux config body while ignoring its sha header line."""
    canonical_for_hash = "\n".join(
        l for l in text.splitlines()
        if not l.startswith("# camc-template-sha256:")
    )
    return hashlib.sha256(
        canonical_for_hash.encode("utf-8")).hexdigest()[:16]


def _camc_tmux_managed_body(version=_CAMC_TMUX_CONFIG_VERSION):
    """Return the canonical body for a given template version. The
    sha line is computed over the *non-sha* lines so the hash is
    deterministic and self-referential without recursion."""
    no_sha = _CAMC_TMUX_CONFIG_BODY.format(version=version, sha="<pending>")
    sha = _camc_tmux_body_sha(no_sha)
    return _CAMC_TMUX_CONFIG_BODY.format(version=version, sha=sha)


def _camc_tmux_existing_meta(path):
    """Return (managed, version, sha) for an existing tmux config
    file, or (False, 0, '') when the file isn't camc-managed."""
    try:
        with open(path, "r") as f:
            head = f.read(2048)
    except (OSError, IOError):
        return False, 0, ""
    managed = ("# camc-managed: true" in head)
    if not managed:
        return False, 0, ""
    version = 0
    sha = ""
    for line in head.splitlines():
        if line.startswith("# camc-template-version:"):
            try:
                version = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                version = 0
        elif line.startswith("# camc-template-sha256:"):
            sha = line.split(":", 1)[1].strip()
    return True, version, sha


def ensure_camc_tmux_config(path=None):
    """Make sure ``~/.cam/configs/tmux.conf`` exists with the current
    v1 template body.

    Behavior:
      * Missing file -> created with the v1 body (mode 0600).
      * File exists and is camc-managed AND matches the expected
        sha of its declared version -> refresh to current version
        if older (idempotent on the current version).
      * File exists and is camc-managed but has been MODIFIED
        (sha mismatch on its declared version) -> leave alone +
        log a warning.
      * File exists and is NOT camc-managed (user-authored) ->
        leave alone, no log.

    Returns the file path. Safe to call from many sites; the lock
    cost is one fs stat + one read on the file.
    """
    path = path or _camc_tmux_config_path()
    body = _camc_tmux_managed_body(_CAMC_TMUX_CONFIG_VERSION)
    try:
        os.makedirs(os.path.dirname(path))
    except OSError:
        pass
    if not os.path.exists(path):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(body)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            log.info("camc tmux config created at %s (v%d)",
                     path, _CAMC_TMUX_CONFIG_VERSION)
        except OSError as e:
            log.warning("failed to create camc tmux config %s: %s", path, e)
        return path
    # File exists — decide whether to refresh.
    managed, existing_version, existing_sha = _camc_tmux_existing_meta(path)
    if not managed:
        # User-authored file; leave alone.
        return path
    try:
        with open(path, "r") as f:
            existing_text = f.read()
    except (OSError, IOError) as e:
        log.warning("failed to read camc tmux config %s: %s", path, e)
        return path
    actual_sha = _camc_tmux_body_sha(existing_text)
    if existing_sha != actual_sha:
        log.warning(
            "camc tmux config at %s is camc-managed but has been "
            "modified locally (sha mismatch); refusing to refresh",
            path)
        return path
    if existing_version < _CAMC_TMUX_CONFIG_VERSION:
        # Managed AND unmodified AND older — refresh.
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(body)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
            log.info("camc tmux config refreshed: %s (v%d -> v%d)",
                     path, existing_version, _CAMC_TMUX_CONFIG_VERSION)
        except OSError as e:
            log.warning(
                "failed to refresh camc tmux config %s: %s", path, e)
    return path


def _detect_tmux_bin():
    """Current `tmux` on PATH, used for new session creation and as the
    last-resort fallback when we can't figure out a session's tmux."""
    return shutil.which("tmux") or "tmux"


TMUX_BIN = _detect_tmux_bin()


def _find_tmux_socket(session_id):
    server_sock_dir = os.path.expanduser("~/.local/share/cam/sockets")
    for sock_dir in (SOCKETS_DIR, "/tmp/cam-agent-sockets", server_sock_dir):
        sock = "%s/%s.sock" % (sock_dir, session_id)
        if os.path.exists(sock):
            return sock
    return None


def _tmux_server_pid_for_socket(sock_path):
    """Return PID of a tmux server whose argv references this socket path,
    or None. Used to guard os.unlink calls: a socket file may be missing
    from ls while a tmux server process is still bound to it (then tmux
    becomes a zombie unreachable via filesystem). Always check ps before
    unlinking so we don't orphan a running server.
    """
    if not sock_path:
        return None
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(
                        "utf-8", errors="replace")
            except (OSError, IOError):
                continue
            if "tmux" not in cmdline or sock_path not in cmdline:
                continue
            try:
                return int(entry)
            except ValueError:
                return None
    except OSError:
        pass
    return None


def _tmux_bin_from_exe(sock_path):
    """Read the tmux binary from /proc/<server-pid>/exe. None if no live
    server or exe is unreadable."""
    pid = _tmux_server_pid_for_socket(sock_path)
    if not pid:
        return None
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except OSError:
        return None
    if not exe or exe.endswith(" (deleted)"):
        return None
    return exe


def _tmux_bin_for_session(session_id):
    """Resolve which tmux binary owns this session.

    Mismatched tmux client/server versions fail silently (capture returns
    empty, attach hangs). We avoid that by sticking with whatever binary
    started the session:

      1. agents.json record's `tmux_bin` (set at creation time, or
         self-healed from a past /proc probe)
      2. readlink(/proc/<server-pid>/exe)
      3. TMUX_BIN (fallback: no record and no live server)

    On a successful /proc probe we write the result (and `tmux -V`
    string) back to the agent record — subsequent lookups skip the scan.
    """
    from camc_pkg.storage import AgentStore
    store = AgentStore()
    agent = store.get(session_id)
    if agent:
        rec = agent.get("tmux_bin")
        if rec and os.path.exists(rec):
            return rec

    socket = _find_tmux_socket(session_id)
    if socket:
        detected = _tmux_bin_from_exe(socket)
        if detected:
            if agent:
                agent["tmux_bin"] = detected
                try:
                    ver_out = subprocess.check_output(
                        [detected, "-V"], stderr=subprocess.STDOUT, timeout=2
                    ).decode(errors="replace").strip()
                    if ver_out:
                        agent["tmux_version"] = ver_out
                except Exception:
                    pass
                try:
                    store.save(agent)
                except Exception:
                    pass
            return detected
    return TMUX_BIN


def _tmux_base(session_id):
    socket = _find_tmux_socket(session_id)
    tmux = _tmux_bin_for_session(session_id)
    return [tmux, "-u", "-S", socket] if socket else [tmux]


def capture_tmux(session_id, lines=100, preserve_ansi=False):
    """Capture tmux pane content.

    lines > 0: last N lines (visible + scrollback up to N).
    lines <= 0 (or None): full scrollback buffer (tmux -S -). The actual
    amount returned is capped by tmux's history-limit (50000 in our
    created sessions). A full-scroll capture of a long-lived agent with
    hundreds of KB of history can take several seconds, so we use a
    generous timeout in that case — the default 5s kills mid-capture
    and returns empty.

    preserve_ansi=False (default): tmux capture-pane plain text;
        output is stripped of any residual ANSI escape sequences before
        return. This is the default used by monitor/detection, mobile,
        and any caller that wants searchable, copy-friendly text.

    preserve_ansi=True: tmux capture-pane is invoked with `-e` so SGR
        colour/style sequences are kept; strip_ansi is bypassed. Used by
        the rich-output API path. Does not change visible cursor or
        movement escapes — tmux only emits SGR in this mode.
    """
    socket = _find_tmux_socket(session_id)
    target = "%s:0.0" % session_id
    # `-S -`  = start of history; `-S -N` = N lines back. We pass one or
    # the other based on the `lines` argument.
    full_scroll = not lines or lines <= 0
    start_flag = "-" if full_scroll else "-%d" % lines
    # Users occasionally run many long-lived agents out of one workdir;
    # their scrollback can reach several MB. Give full-scroll captures
    # plenty of headroom — the operation is bounded and the caller is
    # waiting on output anyway. Bounded captures use the _run default.
    timeout = 60 if full_scroll else 15
    tmux = _tmux_bin_for_session(session_id)
    base_flags = ["capture-pane", "-p", "-J"]
    if preserve_ansi:
        base_flags.append("-e")
    if socket:
        args = [tmux, "-u", "-S", socket] + base_flags + ["-t", target, "-S", start_flag]
    else:
        args = [tmux] + base_flags + ["-t", target, "-S", start_flag]
    try:
        rc, output = _run(args, timeout=timeout)
        if len(output.strip()) < 20:
            alt = list(args)
            alt.insert(alt.index("capture-pane") + 1, "-a")
            rc2, alt_out = _run(alt, timeout=timeout)
            if len(alt_out.strip()) > len(output.strip()):
                output = alt_out
        if preserve_ansi:
            return output.rstrip()
        return strip_ansi(output).rstrip()
    except Exception as e:
        log.debug("capture_tmux failed: %s", e)
        return ""


def _tmux_cmd():
    """Backwards-compat alias; returns the already-resolved TMUX_BIN."""
    return TMUX_BIN


def tmux_session_exists(session_id):
    tmux = _tmux_bin_for_session(session_id)
    socket = _find_tmux_socket(session_id)
    if socket:
        args = [tmux, "-u", "-S", socket, "has-session", "-t", session_id]
    else:
        args = [tmux, "has-session", "-t", session_id]
    rc, _ = _run(args, timeout=15)
    if rc != 0 and socket:
        # Socket-based check failed — retry without socket (maybe socket is stale)
        args = [tmux, "has-session", "-t", session_id]
        rc, _ = _run(args, timeout=15)
    return rc == 0


_TMUX_SEND_CHUNK = 8192            # safely below tmux 3.4's ~16-20KB ARG_MAX
_BRACKET_PASTE_OPEN = "\x1b[200~"
_BRACKET_PASTE_CLOSE = "\x1b[201~"


def tmux_send_input(session_id, text, send_enter=True):
    base = _tmux_base(session_id)
    target = "%s:0.0" % session_id
    try:
        if text:
            # Multi-line text: wrap with bracketed-paste markers so the
            # receiving TUI (Claude/Codex Ink, etc.) treats it as one
            # atomic paste rather than streamed keystrokes. Without
            # this, embedded LFs get inserted as soft returns and the
            # trailing Enter races with tmux's still-flushing literal
            # write — long multi-line inputs sit unsubmitted.
            #
            # Chunking: tmux 3.4's send-keys command-line ARG_MAX is
            # ~16-20KB on Linux; payloads ≥20KB error out with
            # "command too long" and the prompt is silently lost
            # (camflow planner's 21KB workflow_designer prompt). Split
            # the body into 8KB chunks. Bracketed paste guarantees the
            # TUI buffers everything between OPEN/CLOSE atomically, so
            # multi-call delivery is equivalent to a single send.
            multiline = "\n" in text
            if multiline:
                _run(base + ["send-keys", "-t", target, "-l", "--",
                             _BRACKET_PASTE_OPEN], check=True)
                for i in range(0, len(text), _TMUX_SEND_CHUNK):
                    chunk = text[i:i + _TMUX_SEND_CHUNK]
                    _run(base + ["send-keys", "-t", target, "-l", "--",
                                 chunk], check=True)
                _run(base + ["send-keys", "-t", target, "-l", "--",
                             _BRACKET_PASTE_CLOSE], check=True)
            elif len(text) > _TMUX_SEND_CHUNK:
                # Single-line very long input: chunk without bracketed
                # paste (no embedded LFs to compose, just raw chars).
                for i in range(0, len(text), _TMUX_SEND_CHUNK):
                    chunk = text[i:i + _TMUX_SEND_CHUNK]
                    _run(base + ["send-keys", "-t", target, "-l", "--",
                                 chunk], check=True)
            else:
                _run(base + ["send-keys", "-t", target, "-l", "--",
                             text], check=True)
        if send_enter:
            # Pause so the literal text is flushed into the pane buffer
            # before Enter arrives. Scale with payload size — for big
            # multi-line prompts (tens of KB), the fixed 0.15s window
            # was too short and Enter raced ahead, leaving the input
            # typed but unsubmitted. ~1ms per 50 chars, floor 0.15s,
            # ceiling 2.0s.
            if text:
                import time as _t
                _t.sleep(min(2.0, max(0.15, len(text) / 50000.0 + 0.15)))
            _run(base + ["send-keys", "-t", target, "Enter"], check=True)
        return True
    except Exception as e:
        log.warning("tmux_send_input failed: %s", e)
        return False


def tmux_send_key(session_id, key):
    """Send a tmux key (e.g. 'BSpace', 'Enter', 'Escape') to a session."""
    base = _tmux_base(session_id)
    target = "%s:0.0" % session_id
    try:
        _run(base + ["send-keys", "-t", target, key], check=True)
        return True
    except Exception as e:
        log.warning("tmux_send_key failed: %s", e)
        return False


def tmux_is_attached(session_id):
    """Check if a user is attached to this tmux session."""
    base = _tmux_base(session_id)
    try:
        rc, output = _run(base + ["display-message", "-p", "-t", session_id,
                                  "#{session_attached}"])
        if rc == 0 and output.strip():
            return int(output.strip()) > 0
    except (ValueError, Exception):
        pass
    return False


def tmux_kill_session(session_id):
    socket = _find_tmux_socket(session_id)
    tmux = _tmux_bin_for_session(session_id)
    if socket:
        args = [tmux, "-u", "-S", socket, "kill-session", "-t", session_id]
    else:
        args = [tmux, "kill-session", "-t", session_id]
    rc, _ = _run(args)
    return rc == 0


def _tmux_paste_startup_command(tmux, socket, target, text):
    """Paste a startup command into a freshly-created shell pane.

    Some PDX tmux builds have been observed to drop the whole server on
    ``send-keys -l -- <long command>`` before Enter is even sent. The
    buffer/paste path avoids that literal-key injection code path while
    preserving the conservative two-step launch model: create shell
    first, paste command second.
    """
    try:
        _run([tmux, "-u", "-S", socket, "set-buffer", text], check=True)
        _run([tmux, "-u", "-S", socket, "paste-buffer", "-t", target],
             check=True)
        return True
    except Exception as e:
        log.debug("tmux startup paste-buffer failed, falling back: %s", e)
        return False


def create_tmux_session(session_id, command, workdir, env_setup=None,
                        inherit_env=True, env=None, tmux_bin=None,
                        tmux_config=None):
    """Create a detached tmux session named `session_id`.

    F-08: `env` and `tmux_bin` are optional. When provided (the new
    ``cmd_run`` path), the function uses the SAME effective env and
    tmux binary that the readiness check just validated — eliminating
    the "works in preflight, fails in launch" PATH discrepancy. When
    omitted (scheduler.py, tests, any legacy caller), behavior is
    bit-identical to the pre-F-08 implementation: env is sourced from
    ``os.environ.copy()`` and the binary is the module-level
    ``TMUX_BIN`` resolved at import time.

    Regardless of how `env` arrives, ``TMUX`` / ``TMUX_PANE`` /
    ``CLAUDECODE`` are stripped here so a nested-launch fails-safe."""
    try:
        os.makedirs(SOCKETS_DIR)
    except OSError:
        pass
    socket = "%s/%s.sock" % (SOCKETS_DIR, session_id)

    # Source env: explicit arg wins; else current process env.
    env = dict(env) if env is not None else os.environ.copy()
    # Always strip nest markers — protects callers that pass the raw
    # parent env (legacy path) AND the new build_runtime_env() path
    # (it already strips, but defensive double-clear is cheap).
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env.pop("CLAUDECODE", None)

    # Source tmux binary: explicit arg wins; else module-level default.
    tmux = tmux_bin or TMUX_BIN

    # 2026-06-23 PDX hardening: ensure the new tmux server starts with
    # camc's own config (`-f <path>`) instead of inheriting the user's
    # ~/.tmux.conf, which on PDX has crashed servers mid-startup.
    # ``tmux_config`` defaults to ``ensure_camc_tmux_config()``; pass
    # an empty string to opt out (e.g. test fixtures that don't want
    # to touch disk). For follow-up operations (capture/send/attach)
    # the existing -S socket isolation is sufficient — tmux client
    # commands don't need -f since the config was loaded at server
    # start.
    if tmux_config is None:
        try:
            tmux_config = ensure_camc_tmux_config()
        except Exception as e:
            log.warning("camc tmux config ensure failed; "
                        "new-session will inherit user ~/.tmux.conf: %s", e)
            tmux_config = ""

    def _tmux_new_session_argv(extra_args):
        """Build the tmux new-session argv with -f <config> injected
        before -S so it applies to the newly created server."""
        argv = [tmux, "-u"]
        if tmux_config:
            argv += ["-f", tmux_config]
        argv += ["-S", socket, "new-session",
                 "-d", "-x", "220", "-y", "50",
                 "-s", session_id, "-c", workdir]
        argv += extra_args
        return argv

    if inherit_env:
        # Shell mode: start tmux with user's default shell, inherit all env.
        # Then send the command via send-keys.
        try:
            tmux_cmd = _tmux_new_session_argv([])
            proc = subprocess.Popen(
                tmux_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            _, stderr = proc.communicate(timeout=10)
            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip() if stderr else "exit code %d" % proc.returncode
                log.error("tmux new-session failed: %s", err_msg)
                return False
            _run([tmux, "-u", "-S", socket, "set-option", "-t", session_id,
                  "history-limit", "50000"])
            # Paste the command through a tmux buffer instead of
            # send-keys -l. On some PDX tmux builds, literal send-keys
            # can drop the whole server before Enter is sent.
            inner_cmd = " ".join(shlex.quote(arg) for arg in command)
            target = "%s:0.0" % session_id
            if not _tmux_paste_startup_command(tmux, socket, target,
                                               inner_cmd):
                _run([tmux, "-u", "-S", socket, "send-keys", "-t", target,
                      "-l", "--", inner_cmd])
            import time as _t
            _t.sleep(0.15)
            _run([tmux, "-u", "-S", socket, "send-keys", "-t", target, "Enter"])
            return True
        except subprocess.TimeoutExpired:
            log.error("tmux new-session timed out after 10s for %s", session_id)
            return False
        except Exception as e:
            log.error("Failed to create shell session %s: %s", session_id, e)
            return False

    inner_cmd = " ".join(shlex.quote(arg) for arg in command)
    if not env_setup:
        # Pull PATH from the explicit env if provided, otherwise from
        # the calling process. Either way we land an explicit `export
        # PATH=...` inside the bash invocation so the launched command
        # sees the same PATH preflight checked against.
        env_setup = "export PATH=%s" % shlex.quote(env.get("PATH", os.environ.get("PATH", "")))
    # Non-login bash: env_setup is the sole source of Anthropic/proxy
    # overrides. `bash -l` would re-source ~/.bashrc and re-inject login
    # session / ANTHROPIC_* exports before env_setup can win.
    command_str = "env -u CLAUDECODE bash -c %s" % shlex.quote(env_setup + " && exec " + inner_cmd)

    try:
        proc = subprocess.Popen(
            _tmux_new_session_argv([command_str]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        _, stderr = proc.communicate(timeout=10)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip() if stderr else "exit code %d" % proc.returncode
            log.error("tmux new-session failed: %s", err_msg)
            return False
        _run([tmux, "-u", "-S", socket, "set-option", "-t", session_id,
              "history-limit", "50000"])
        return True
    except subprocess.TimeoutExpired:
        log.error("tmux new-session timed out after 10s for %s", session_id)
        return False
    except Exception as e:
        log.error("Failed to create session %s: %s", session_id, e)
        return False
