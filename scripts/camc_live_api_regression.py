#!/usr/bin/env python3
"""Real-provider CamC lifecycle regression.

This is intentionally separate from pytest: it starts a real coding agent and
uses the configured internal API profile.  Python 3.6+, standard library only.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


TASK = """# CamC real API regression task

Using only the Python 3.6+ standard library, implement `event_summary.py`.
It accepts one JSONL input path, ignores blank lines, validates string
`service`, non-negative numeric `duration_ms` (not bool), and boolean `ok`.
Malformed or invalid lines must produce a line-numbered stderr error and a
nonzero exit.

For valid input print deterministic JSON with top-level `services` and
`total_events`. `services` maps lexicographically sorted service keys to
`count`, `failures`, and `max_duration_ms`.

Also create `test_event_summary.py` with unittest coverage for success, blank
lines, malformed JSON, and invalid types; `README.md` with usage; and
`RESULT.md` with the exact verification command/result. Run the tests. Do not
use network/external packages, commit/push, or edit supplied files.
"""

SAMPLE = """{"service":"fetch","duration_ms":12,"ok":true}

{"service":"decode","duration_ms":7,"ok":false}
{"service":"fetch","duration_ms":21,"ok":false}
"""

PROMPT = (
    "Read TASK.md and complete every required deliverable. Work autonomously "
    "until all tests pass and RESULT.md is written."
)

EXPECTED = {
    "services": {
        "decode": {"count": 1, "failures": 1, "max_duration_ms": 7},
        "fetch": {"count": 2, "failures": 1, "max_duration_ms": 21},
    },
    "total_events": 3,
}


def run(argv, timeout=30, cwd=None, check=True):
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True)
    try:
        output = proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        output = proc.communicate()[0]
        raise RuntimeError("command timed out: %s\n%s" % (" ".join(argv), output))
    if check and proc.returncode != 0:
        raise RuntimeError("command failed (%d): %s\n%s" % (
            proc.returncode, " ".join(argv), output))
    return proc.returncode, output


def camc(binary, *args, **kwargs):
    return run([binary] + list(args), **kwargs)


def parse_agent_id(output):
    match = re.search(r"(?m)^\s*ID:\s*([0-9a-f]{8})\b", output)
    if not match:
        raise RuntimeError("camc run did not report an agent ID:\n" + output)
    return match.group(1)


def read_agent_events(agent_id):
    path = os.path.expanduser("~/.cam/events.jsonl")
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("agent_id") == agent_id:
                rows.append(row)
    return rows


def wait_for_deliverables(binary, agent_id, workdir, deadline):
    required = ["event_summary.py", "test_event_summary.py", "README.md", "RESULT.md"]
    last_capture = ""
    while time.time() < deadline:
        _, last_capture = camc(binary, "capture", agent_id, "--lines", "60",
                               timeout=20, check=False)
        if all(os.path.isfile(os.path.join(workdir, name)) for name in required):
            code, output = run(
                [sys.executable, "-m", "unittest", "-v", "test_event_summary.py"],
                timeout=60, cwd=workdir, check=False)
            if code == 0:
                return output
        time.sleep(3)
    raise RuntimeError("agent did not complete deliverables before timeout\n" + last_capture)


def wait_for_text(binary, agent_id, expected, deadline):
    last_capture = ""
    while time.time() < deadline:
        _, last_capture = camc(binary, "capture", agent_id, "--lines", "80",
                               timeout=20, check=False)
        if expected in last_capture:
            return
        time.sleep(2)
    raise RuntimeError("agent did not acknowledge follow-up\n" + last_capture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--camc", default=os.environ.get("CAMC_BIN", "dist/camc"),
        help="candidate CamC binary (default: CAMC_BIN or dist/camc)")
    parser.add_argument(
        "--api", default=os.environ.get("CAMC_LIVE_API", "kimi-k3"),
        help="real API profile (default: kimi-k3)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="task timeout in seconds (default: 600)")
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args()

    binary = os.path.abspath(args.camc)
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        parser.error("CamC binary is not executable: %s" % binary)

    workdir = tempfile.mkdtemp(prefix="camc-live-api-regression-")
    agent_id = None
    success = False
    try:
        with open(os.path.join(workdir, "TASK.md"), "w") as stream:
            stream.write(TASK)
        with open(os.path.join(workdir, "sample.jsonl"), "w") as stream:
            stream.write(SAMPLE)

        _, output = camc(
            binary, "run", "--tool", "codex", "--api", args.api,
            "--tool-launcher", "none",
            "--path", workdir, "--name", "camc-live-api-regression", PROMPT,
            timeout=60)
        agent_id = parse_agent_id(output)
        print("agent_id=%s api=%s workdir=%s" % (agent_id, args.api, workdir))

        deadline = time.time() + args.timeout
        test_output = wait_for_deliverables(binary, agent_id, workdir, deadline)
        _, sample_output = run(
            [sys.executable, "event_summary.py", "sample.jsonl"],
            timeout=30, cwd=workdir)
        if json.loads(sample_output) != EXPECTED:
            raise RuntimeError("sample result mismatch:\n" + sample_output)

        events = read_agent_events(agent_id)
        confirms = [row for row in events if row.get("type") == "auto_confirm"]
        ready = [row for row in events if row.get("type") == "boot_ready"]
        if len(confirms) != 1:
            raise RuntimeError("expected one auto_confirm, got %d" % len(confirms))
        if len(ready) != 1 or not ready[0].get("detail", {}).get("prompt_sent"):
            raise RuntimeError("expected one prompt-sent boot_ready event")

        followup = (
            "Confirm RESULT.md exists and rerun the unittest. Then reply with "
            "the three words ACK LIVE REGRESSION joined by hyphens, and nothing "
            "else. Do not modify files."
        )
        camc(binary, "msg", "send", agent_id, "-t", followup, "--no-wait",
             timeout=30)
        wait_for_text(binary, agent_id, "ACK-LIVE-REGRESSION", deadline)

        _, rm_output = camc(binary, "rm", agent_id, "--archive", timeout=60)
        agent_id = None
        archive_match = re.search(r"(/\S+\.tar\.gz)", rm_output)
        if not archive_match or not os.path.isfile(archive_match.group(1)):
            raise RuntimeError("rm --archive did not produce an archive:\n" + rm_output)

        print(test_output.rstrip())
        print("PASS archive=%s" % archive_match.group(1))
        success = True
        return 0
    except Exception as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if agent_id:
            camc(binary, "rm", agent_id, timeout=60, check=False)
        if success and not args.keep_workdir:
            shutil.rmtree(workdir)
        else:
            print("workdir retained: %s" % workdir, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
