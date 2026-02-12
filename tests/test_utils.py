"""Tests for utility modules."""

from __future__ import annotations

from cam.utils.ansi import strip_ansi


class TestStripAnsi:
    def test_no_ansi(self):
        assert strip_ansi("Hello world") == "Hello world"

    def test_color_codes(self):
        assert strip_ansi("\x1B[1;32mGreen\x1B[0m text") == "Green text"

    def test_cursor_movement(self):
        assert strip_ansi("\x1B[2J\x1B[HHello") == "Hello"

    def test_osc_sequences(self):
        assert strip_ansi("\x1B]0;Window Title\x07Content") == "Content"

    def test_empty_string(self):
        assert strip_ansi("") == ""

    def test_complex_ansi(self):
        text = "\x1B[1m1. Yes\x1B[0m  \x1B[2m2. No\x1B[0m"
        assert strip_ansi(text) == "1. Yes  2. No"

    def test_csi_with_question_mark(self):
        # CSI ? sequences (e.g. cursor show/hide)
        assert strip_ansi("\x1B[?25hVisible") == "Visible"
