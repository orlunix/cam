"""Tests for scripts/ihub_ping_curated.py helpers."""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import ihub_ping_curated as ping  # noqa: E402


def test_classify_ok():
    body = json.dumps({
        "choices": [{"message": {"content": "PING_OK"}}],
    })
    status, detail = ping._classify(200, body, 1.2)
    assert status == "ok"
    assert "PING_OK" in detail


def test_classify_overloaded():
    body = json.dumps({
        "error": {"message": "Service temporarily overloaded"},
    })
    status, _ = ping._classify(503, body, 0.5)
    assert status == "overloaded"


def test_classify_auth():
    body = json.dumps({
        "error": {"message": "key not allowed to access model"},
    })
    status, _ = ping._classify(401, body, 0.2)
    assert status == "auth"


def test_classify_ok_reasoning():
    body = json.dumps({
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": "PING_OK reasoning",
            },
        }],
    })
    status, detail = ping._classify(200, body, 1.0)
    assert status == "ok"
    assert "PING_OK" in detail


def test_classify_timeout():
    status, detail = ping._classify(0, "", 60.0, timed_out=True)
    assert status == "timeout"
    assert "60" in detail
