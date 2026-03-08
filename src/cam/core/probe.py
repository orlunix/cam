"""Probe-based completion detection for CAM.

Detects whether an agent is busy (TUI raw mode) or at prompt (cooked mode)
by sending a probe character and observing whether it echoes in the terminal.

TUI apps (Claude Code, Aider, vim) use tty.setraw() while working, which
disables kernel echo. When the agent returns to a prompt, echo is restored.

Algorithm:
  1. Capture baseline output
  2. Send a probe character (without Enter)
  3. Wait briefly, capture again
  4. Classify:
     - Probe visible on last line → COMPLETED (at prompt)
     - Output changed but probe not visible → CONFIRMED (confirmation was consumed)
     - Output unchanged → BUSY (raw mode, echo disabled)
  5. Clean up probe char with BSpace if it was echoed
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

from cam.transport.base import Transport

logger = logging.getLogger(__name__)

PROBE_CHAR = "Z"


class ProbeResult(str, Enum):
    """Result of probing a session's terminal state."""

    COMPLETED = "completed"  # Probe visible → agent at prompt
    CONFIRMED = "confirmed"  # Probe consumed as input (e.g. confirmation)
    BUSY = "busy"  # Probe invisible → agent in raw mode (working)
    SESSION_DEAD = "session_dead"  # Session no longer exists
    ERROR = "error"  # Unexpected failure


async def probe_session(
    transport: Transport,
    session_id: str,
    wait: float = 0.3,
    probe_char: str = "Z",
    send_enter: bool = False,
) -> ProbeResult:
    """Probe a session to determine if agent is busy or at prompt.

    Two modes:

    **Normal** (probe_char != ""): Send a character without Enter.
    If the char echoes on the last line → COMPLETED (at prompt).
    If output changed but char not visible → CONFIRMED (consumed).
    If unchanged → BUSY (raw mode).

    **Enter-only** (probe_char == ""): Send Enter only. No char to
    echo-detect, so COMPLETED is never returned.
    If output changed → CONFIRMED (Enter was consumed).
    If unchanged → BUSY (agent in raw mode, Enter buffered).

    Args:
        transport: Transport instance for the session.
        session_id: TMUX session identifier.
        wait: Seconds to wait after sending probe before capturing.
        probe_char: Character to send (empty = Enter-only mode).
        send_enter: Whether to press Enter after the probe char.

    Returns:
        ProbeResult indicating the session's state.
    """
    # 1. Check session is alive
    try:
        alive = await transport.session_exists(session_id)
    except Exception as e:
        logger.debug("Probe: session_exists failed for %s: %s", session_id, e)
        return ProbeResult.ERROR

    if not alive:
        return ProbeResult.SESSION_DEAD

    # 2. Capture baseline
    try:
        baseline = await transport.capture_output(session_id)
    except Exception as e:
        logger.debug("Probe: baseline capture failed for %s: %s", session_id, e)
        return ProbeResult.ERROR

    baseline = baseline.rstrip("\n")

    # 3. Send probe (char and/or Enter)
    try:
        sent = await transport.send_input(
            session_id, probe_char, send_enter=send_enter,
        )
    except Exception as e:
        logger.debug("Probe: send_input failed for %s: %s", session_id, e)
        return ProbeResult.ERROR

    if not sent:
        logger.debug("Probe: send_input returned False for %s", session_id)
        return ProbeResult.ERROR

    # 4. Wait and recapture
    await asyncio.sleep(wait)

    try:
        after = await transport.capture_output(session_id)
    except Exception as e:
        logger.debug("Probe: post-capture failed for %s: %s", session_id, e)
        return ProbeResult.ERROR

    after = after.rstrip("\n")

    # 5. Classify result
    if probe_char:
        # Normal mode: check if probe char echoed on last line
        after_lines = after.splitlines()
        last_line = after_lines[-1] if after_lines else ""
        baseline_last = baseline.splitlines()[-1] if baseline.splitlines() else ""

        if probe_char in last_line and probe_char not in baseline_last:
            # Probe is visible on the last line — agent is at prompt (echo mode)
            # Clean up: send BSpace to remove the probe char
            try:
                await transport.send_key(session_id, "BSpace")
            except Exception:
                logger.debug("Probe: BSpace cleanup failed for %s", session_id)
            logger.debug("Probe: COMPLETED for %s (probe visible)", session_id)
            return ProbeResult.COMPLETED

    # Both modes: check if output changed
    if after != baseline:
        logger.debug("Probe: CONFIRMED for %s (output changed)", session_id)
        return ProbeResult.CONFIRMED

    # Output unchanged — agent in raw mode, echo disabled
    logger.debug("Probe: BUSY for %s (no echo)", session_id)
    return ProbeResult.BUSY
