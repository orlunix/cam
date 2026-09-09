"""Focused tests for the legacy unobserved tmux submission seam."""

from camc_pkg.transport import tmux_submit_input


def test_tmux_submit_input_types_once_and_uses_bounded_fallback_enter():
    events = []

    result = tmux_submit_input(
        "cam-test",
        "hello",
        submit_delay=0.5,
        send_input_fn=lambda session, text, send_enter=False: (
            events.append(("text", session, text, send_enter)) or True),
        send_key_fn=lambda session, key: (
            events.append(("key", session, key)) or True),
        sleep_fn=lambda delay: events.append(("sleep", delay)),
    )

    assert result is True
    assert events == [
        ("text", "cam-test", "hello", False),
        ("sleep", 0.15),
        ("key", "cam-test", "Enter"),
        ("sleep", 0.35),
        ("key", "cam-test", "Enter"),
    ]


def test_tmux_submit_input_stops_when_text_delivery_fails():
    events = []

    result = tmux_submit_input(
        "cam-test",
        "hello",
        send_input_fn=lambda session, text, send_enter=False: False,
        send_key_fn=lambda session, key: events.append((session, key)) or True,
        sleep_fn=lambda delay: events.append(delay),
    )

    assert result is False
    assert events == []
