#!/usr/bin/env python3
"""hello-ext remote tool — SPEC §3 contract:
  python3 main.py <method> '<json-args>'  →  one JSON object on stdout
python3.6 stdlib only; runs on any remote host the app manages.
"""
import json
import os
import platform
import sys
import time


def sysinfo(_args):
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python":  sys.version.split()[0],
        "home":    os.path.expanduser("~"),
        "time":    int(time.time()),
    }


def echo(args):
    return {"echo": args}


METHODS = {"sysinfo": sysinfo, "echo": echo}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "missing_method", "detail": "usage: main.py <method> '<json>'"}))
        return 2
    method = sys.argv[1]
    fn = METHODS.get(method)
    if not fn:
        print(json.dumps({"error": "unknown_method", "detail": "known: " + ", ".join(sorted(METHODS))}))
        return 2
    try:
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except Exception as e:
        print(json.dumps({"error": "bad_args", "detail": str(e)}))
        return 2
    try:
        print(json.dumps(fn(args)))
        return 0
    except Exception as e:
        print(json.dumps({"error": "tool_failed", "detail": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
