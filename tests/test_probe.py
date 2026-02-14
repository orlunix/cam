"""Unit tests for probe-based completion detection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from cam.core.probe import PROBE_CHAR, ProbeResult, probe_session


def _make_transport(
    session_alive: bool = True,
    baseline: str = "❯ \n",
    after: str | None = None,
    send_input_ok: bool = True,
    send_key_ok: bool = True,
):
    """Create a mock Transport with configurable behavior."""
    transport = AsyncMock()
    transport.session_exists = AsyncMock(return_value=session_alive)

    capture_calls = [0]
    baseline_val = baseline
    after_val = after if after is not None else baseline

    async def mock_capture(sid, lines=50):
        capture_calls[0] += 1
        if capture_calls[0] == 1:
            return baseline_val
        return after_val

    transport.capture_output = AsyncMock(side_effect=mock_capture)
    transport.send_input = AsyncMock(return_value=send_input_ok)
    transport.send_key = AsyncMock(return_value=send_key_ok)
    return transport


class TestProbeCompleted:
    """Probe visible on last line → COMPLETED."""

    @pytest.mark.asyncio
    async def test_probe_visible_means_completed(self):
        transport = _make_transport(
            baseline="❯ \n",
            after=f"❯ {PROBE_CHAR}\n",
        )
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.COMPLETED

    @pytest.mark.asyncio
    async def test_bspace_sent_on_completed(self):
        transport = _make_transport(
            baseline="❯ \n",
            after=f"❯ {PROBE_CHAR}\n",
        )
        await probe_session(transport, "cam-test", wait=0.01)
        transport.send_key.assert_awaited_once_with("cam-test", "BSpace")

    @pytest.mark.asyncio
    async def test_probe_on_bash_prompt(self):
        transport = _make_transport(
            baseline="user@host:~$ \n",
            after=f"user@host:~$ {PROBE_CHAR}\n",
        )
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.COMPLETED


class TestProbeBusy:
    """Output unchanged → BUSY (raw mode, echo disabled)."""

    @pytest.mark.asyncio
    async def test_no_echo_means_busy(self):
        output = "Working on task...\nProcessing files\n"
        transport = _make_transport(
            baseline=output,
            after=output,  # unchanged
        )
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.BUSY

    @pytest.mark.asyncio
    async def test_no_bspace_on_busy(self):
        output = "Working on task...\n"
        transport = _make_transport(
            baseline=output,
            after=output,
        )
        await probe_session(transport, "cam-test", wait=0.01)
        transport.send_key.assert_not_awaited()


class TestProbeConfirmed:
    """Output changed but probe not visible → CONFIRMED."""

    @pytest.mark.asyncio
    async def test_output_changed_means_confirmed(self):
        transport = _make_transport(
            baseline="Do you want to proceed? [y/N]\n",
            after="Proceeding with task...\nRunning step 1\n",
        )
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.CONFIRMED

    @pytest.mark.asyncio
    async def test_no_bspace_on_confirmed(self):
        transport = _make_transport(
            baseline="Waiting...\n",
            after="Now doing something else\n",
        )
        await probe_session(transport, "cam-test", wait=0.01)
        transport.send_key.assert_not_awaited()


class TestProbeSessionDead:
    """Session gone → SESSION_DEAD."""

    @pytest.mark.asyncio
    async def test_session_not_alive(self):
        transport = _make_transport(session_alive=False)
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.SESSION_DEAD
        # Should not attempt capture or send
        transport.capture_output.assert_not_awaited()
        transport.send_input.assert_not_awaited()


class TestProbeError:
    """Error conditions → ERROR."""

    @pytest.mark.asyncio
    async def test_session_exists_raises(self):
        transport = AsyncMock()
        transport.session_exists = AsyncMock(side_effect=RuntimeError("connection lost"))
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.ERROR

    @pytest.mark.asyncio
    async def test_send_input_fails(self):
        transport = _make_transport(send_input_ok=False)
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.ERROR

    @pytest.mark.asyncio
    async def test_capture_raises(self):
        transport = AsyncMock()
        transport.session_exists = AsyncMock(return_value=True)
        transport.capture_output = AsyncMock(side_effect=RuntimeError("capture fail"))
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.ERROR


class TestProbeEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_baseline(self):
        """Empty baseline + probe visible = COMPLETED."""
        transport = _make_transport(
            baseline="",
            after=f"{PROBE_CHAR}\n",
        )
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.COMPLETED

    @pytest.mark.asyncio
    async def test_probe_char_already_in_baseline(self):
        """If probe char is already on the last line of baseline, don't false-positive."""
        transport = _make_transport(
            baseline=f"Some output with {PROBE_CHAR} already\n",
            after=f"Some output with {PROBE_CHAR} already\n",
        )
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.BUSY

    @pytest.mark.asyncio
    async def test_bspace_failure_doesnt_affect_result(self):
        """BSpace cleanup failure shouldn't change COMPLETED result."""
        transport = _make_transport(
            baseline="❯ \n",
            after=f"❯ {PROBE_CHAR}\n",
            send_key_ok=False,
        )
        transport.send_key = AsyncMock(side_effect=RuntimeError("send_key failed"))
        result = await probe_session(transport, "cam-test", wait=0.01)
        assert result == ProbeResult.COMPLETED
