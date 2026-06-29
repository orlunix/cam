#!/usr/bin/env python3
"""Ping curated Inference Hub models via direct upstream (no local proxy).

Reads the camc curated API list from api-models.json (enabled profiles) or
all CURATED_APIS seeds. For each model, POSTs a tiny chat/completions request
to inference-api.nvidia.com and reports which are usable right now.

Usage:
  python3 scripts/ihub_ping_curated.py
  python3 scripts/ihub_ping_curated.py --all --timeout 45
  python3 scripts/ihub_ping_curated.py --json
  ~/.cam/camc api ...  # not required; uses ~/.cam token files

Exit codes: 0 if at least one model OK, 1 if hub unreachable or none OK.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from camc_pkg.api_routing import (  # noqa: E402
    PROTO_OPENAI_CHAT,
    provider_endpoint_url,
    resolve_upstream_protocol,
)
from camc_pkg.api_store import (  # noqa: E402
    CURATED_APIS,
    DEFAULT_PROVIDER,
    IHUB_BASE,
    load_api_models,
)
from camc_pkg.api_token import resolve_token  # noqa: E402


PING_PROMPT = "Reply with exactly: PING_OK"
PING_MAX_TOKENS = 32


def _extract_completion_text(data):
    """Pull assistant text from OpenAI-style chat completion JSON."""
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message") or {}
    if not isinstance(msg, dict):
        return ""
    for key in ("content", "reasoning_content", "text"):
        val = msg.get(key)
        if val:
            return str(val).strip()
    return ""


def _classify(http_code, body_text, elapsed_s, timed_out=False):
    """Return (status, detail) where status is ok|overloaded|auth|timeout|error."""
    if timed_out:
        return "timeout", "no response within timeout (%.1fs)" % elapsed_s
    try:
        data = json.loads(body_text) if body_text else {}
    except ValueError:
        data = {}
    if http_code == 200:
        text = _extract_completion_text(data)
        if text:
            preview = text.replace("\n", " ")[:60]
            return "ok", preview
        return "error", "HTTP 200 but empty completion"
    err = data.get("error") if isinstance(data, dict) else None
    msg = ""
    if isinstance(err, dict):
        msg = str(err.get("message") or err.get("type") or "")
    elif isinstance(err, str):
        msg = err
    if not msg:
        msg = (body_text or "")[:200]
    low = msg.lower()
    if http_code in (401, 403) or "not allowed to access model" in low:
        return "auth", msg[:160]
    if http_code == 503 or "overloaded" in low or "unavailable" in low:
        return "overloaded", msg[:160]
    if http_code == 429 or "rate limit" in low:
        return "rate_limit", msg[:160]
    return "error", "HTTP %d: %s" % (http_code, msg[:140])


def _hub_catalog(token, timeout):
    """Quick GET /v1/models — returns (ok, model_count, elapsed_s, detail)."""
    url = IHUB_BASE.rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer %s" % token},
        method="GET",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            elapsed = time.time() - t0
            data = json.loads(raw) if raw else {}
            count = len(data.get("data") or [])
            return True, count, elapsed, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return False, 0, time.time() - t0, "HTTP %d: %s" % (e.code, body[:120])
    except Exception as e:
        return False, 0, time.time() - t0, str(e)


def _ping_model(url, upstream_model, token, timeout):
    payload = json.dumps({
        "model": upstream_model,
        "max_tokens": PING_MAX_TOKENS,
        "messages": [{"role": "user", "content": PING_PROMPT}],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return _classify(resp.status, raw, time.time() - t0)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return _classify(e.code, body, time.time() - t0)
    except Exception as e:
        elapsed = time.time() - t0
        if "timed out" in str(e).lower():
            return _classify(0, "", elapsed, timed_out=True)
        return "error", str(e)[:160]


def _curated_targets(enabled_only=True):
    """Yield (api_key, upstream_model) for ping targets."""
    data = load_api_models()
    apis = data.get("apis") or {}
    provider = (data.get("providers") or {}).get(DEFAULT_PROVIDER) or {}
    if not provider:
        provider = {
            "base_url": IHUB_BASE,
            "upstream_protocol": PROTO_OPENAI_CHAT,
            "endpoints": {"openai_chat_completions": "/chat/completions"},
        }
    upstream_proto = PROTO_OPENAI_CHAT
    url = provider_endpoint_url(provider, upstream_proto)

    if enabled_only:
        for key, entry in sorted(apis.items()):
            if not entry.get("enabled"):
                continue
            if entry.get("provider") != DEFAULT_PROVIDER:
                continue
            model = entry.get("model") or ""
            if model:
                yield key, model, url
        return

    for key, model, _aliases in CURATED_APIS:
        yield key, model, url


def run_ping(enabled_only=True, timeout=60.0, json_out=False):
    token, token_src = resolve_token("inference_hub", [
        "INFERENCE_HUB_TOKEN",
        "INFERENCE_HUB_API_KEY",
        "INFERENCE_API_KEY",
    ])
    if not token:
        print("No Inference Hub token (set INFERENCE_HUB_API_KEY in ~/.cam/)",
              file=sys.stderr)
        return 1, []

    hub_ok, hub_count, hub_elapsed, hub_err = _hub_catalog(token, min(timeout, 15.0))
    targets = list(_curated_targets(enabled_only=enabled_only))
    if not targets:
        print("No curated APIs to ping (enable some in api-models.json or use --all)",
              file=sys.stderr)
        return 1, []

    rows = []
    for api_key, upstream_model, url in targets:
        status, detail = _ping_model(url, upstream_model, token, timeout)
        rows.append({
            "api": api_key,
            "model": upstream_model,
            "status": status,
            "detail": detail,
        })

    ok_count = sum(1 for r in rows if r["status"] == "ok")
    report = {
        "hub": {
            "reachable": hub_ok,
            "models": hub_count,
            "elapsed_s": round(hub_elapsed, 3),
            "error": hub_err,
        },
        "token_source": token_src,
        "timeout_s": timeout,
        "enabled_only": enabled_only,
        "results": rows,
        "ok_count": ok_count,
    }

    if json_out:
        print(json.dumps(report, indent=2))
    else:
        if hub_ok:
            print("IHUB catalog: OK (%d models, %.2fs)" % (hub_count, hub_elapsed))
        else:
            print("IHUB catalog: FAIL (%.2fs) %s" % (hub_elapsed, hub_err))
        print("Token: %s" % token_src)
        print("")
        print("%-18s %-42s %-12s %s" % ("API", "UPSTREAM_MODEL", "STATUS", "DETAIL"))
        print("-" * 100)
        for row in rows:
            print("%-18s %-42s %-12s %s" % (
                row["api"], row["model"], row["status"], row["detail"]))
        print("")
        print("OK: %d / %d" % (ok_count, len(rows)))

    if not hub_ok:
        return 1, rows
    return 0 if ok_count else 1, rows


def main():
    parser = argparse.ArgumentParser(
        description="Ping curated IHUB models via direct chat/completions")
    parser.add_argument(
        "--all", action="store_true",
        help="Ping all CURATED_APIS seeds (default: only enabled in api-models.json)")
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="Per-model request timeout in seconds (default: 60)")
    parser.add_argument("--json", action="store_true", dest="json_out",
                        help="Emit JSON report")
    args = parser.parse_args()
    code, _ = run_ping(
        enabled_only=not args.all,
        timeout=args.timeout,
        json_out=args.json_out,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
