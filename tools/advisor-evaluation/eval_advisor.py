"""Run bounded, read-only decision probes against a REAL connected Advisor.

This evaluates decision behavior on supplied evidence. It does NOT prove
file investigation, periodic wake-up, Worker recovery, or task convergence.
No model/agent results are synthesized if the transport is unavailable.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time


def parse_reply(text, nonce):
    # Require one correlated object; reject missing, malformed or duplicate replies.
    found = []
    decoder = json.JSONDecoder()
    for pos, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[pos:])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("test_id") == nonce:
            found.append(obj)
    if len(found) != 1:
        raise ValueError("expected exactly one correlated JSON reply, got %d" % len(found))
    return found[0]


def validate(reply, case):
    errors = []
    if reply.get("decision") != case["expected"]:
        errors.append("decision %r != %r" % (reply.get("decision"), case["expected"]))
    refs = reply.get("evidence_ids")
    known = {x["id"] for x in case["input"]["evidence"]}
    if not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x in known for x in refs):
        errors.append("evidence_ids must cite supplied evidence")
    action = reply.get("next_action")
    if not isinstance(action, str):
        errors.append("next_action must be a string")
    elif case["expected"] in ("intervene", "escalate") and not action.strip():
        errors.append("intervention/escalation requires an action")
    elif case["expected"] in ("continue", "complete") and action.strip():
        errors.append("no Worker action allowed for this case")
    if not isinstance(reply.get("reason"), str) or not reply["reason"].strip():
        errors.append("reason required")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--advisor", required=True, help="dedicated test Advisor ID/name")
    parser.add_argument("--camc", default="camc")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--output", type=Path, default=Path("advisor-live-results.json"))
    args = parser.parse_args()
    if args.repeats < 1 or args.timeout < 1:
        parser.error("repeats and timeout must be positive")
    cases = json.loads(args.cases.read_text())
    results = []
    run_id = str(time.time_ns())
    unavailable = False
    for repeat in range(args.repeats):
        for case in cases:
            nonce = run_id + ":" + case["id"] + ":" + str(repeat)
            prompt = (
                "ISOLATED ADVISOR EVALUATION. This is a synthetic test task, not your live assignment. "
                "Use only the supplied evidence. Do not modify files, send Worker messages, "
                "change ToDos or schedules. State what you would do. "
                "Return JSON with test_id, decision (continue/intervene/escalate/complete), "
                "reason, evidence_ids, next_action. continue means no intervention now, "
                "including human pause, justified external wait and pending advice. "
                "complete requires evidence for every acceptance condition at the current revision. "
                "For continue/complete set next_action to an empty string. "
                "If the transport instructs camc msg reply, put this JSON in that reply.\n"
                + json.dumps({"test_id": nonce, "task": case["input"]}, ensure_ascii=False)
            )
            command = [args.camc, "msg", "send", args.advisor, "--text", prompt,
                       "--expect-reply", "--timeout", str(args.timeout)]
            start = time.monotonic()
            item = {"id": case["id"], "repeat": repeat, "test_id": nonce}
            try:
                proc = subprocess.run(command, text=True, capture_output=True,
                                      timeout=args.timeout + 15)
                item.update(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
                if proc.returncode:
                    raise RuntimeError("transport exit %d" % proc.returncode)
                reply = parse_reply(proc.stdout, nonce)
                errors = validate(reply, case)
                item.update(reply=reply, errors=errors, status="fail" if errors else "pass")
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                item.update(status="unavailable", error=str(exc))
                unavailable = True
            except ValueError as exc:
                item.update(status="fail", error=str(exc))
            item["seconds"] = round(time.monotonic() - start, 3)
            results.append(item)
            report = {"scope": "real-agent decision probes; synthetic evidence; NOT end-to-end",
                      "manual_review_required": "inspect action correctness and evidence reasoning",
                      "results": results,
                      "counts": {s: sum(x["status"] == s for x in results)
                                 for s in ("pass", "fail", "unavailable")}}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(case["id"], item["status"], flush=True)
            if unavailable:
                return 2
    return 1 if any(x["status"] != "pass" for x in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
