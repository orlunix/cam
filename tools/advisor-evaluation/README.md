# Advisor evaluation — actual evidence and remaining work

## Result

The Advisor's effectiveness has NOT been established.

On 2026-11-06, 16 isolated regression/characterization tests of real CAM
Loop code and 9 evaluator-integrity tests passed. No real LLM Advisor or
Worker was executed. The environment has no camc, coding-agent CLI,
model API credentials, or connection to the user's running CAM nodes.

Source: orlunix/cam, commit 898d21935f5696e58920556b5689d5c6a9ff4841.
The source snapshot was read without modification. No deployment or
repository changes were made. The proposed advisor.spec template is not
implemented in this snapshot.

The tests use temporary directories. Owner records, transport results,
and clocks are injected; scheduling, files, retry state, and the host
scheduler lock are real production code. These are NOT live cron,
tmux, model, or end-to-end tests. The repository's original pytest suite
was NOT run: pytest was unavailable and installation failed.

## Confirmed mechanism behavior

- Due + running + idle owner dispatches and advances schedule.
- Busy owner defers, preserves the due time and does not spend retries.
- A later idle owner receives the deferred prompt.
- Sequential ticks do not re-dispatch the same due interval.
- Missed intervals do not cause a catch-up flood.
- Disabled loops do not dispatch; transport failure counts and eventually
  disables the loop; successful dispatch resets failures.
- Host scheduler respects an already-held tick lock.

## Gaps exposed by passing characterization tests

- `sent` proves only transport acknowledgement, not completed review.
- A killed owner remains deferred; no automatic restart is performed.
- Three consecutive dispatch failures disable the loop, ending supervision
  unless another component repairs or alerts on this condition.
- The loop scheduler does not inspect a supervised ToDo's human-pause state.
  This must be enforced separately by the Advisor/template lifecycle.

These are limits relative to the proposed Advisor contract, not claims
that generic Cron Loop code promises these missing features.

## Run the source regression tests

Requires Python 3.10+ and the CAM checkout; no extra Python dependencies.

```bash
cd advisor-evaluation
CAM_SOURCE=/absolute/path/to/cam/src python -m unittest -v test_loop_mechanism test_eval_validator
```

## Run real-agent decision probes

Use a dedicated test Advisor, with no production assignment. Its replies
must be written through the real CAM reply mechanism. These probes do not
mutate a Worker, ToDo, workspace or Cron. They send synthetic observations
to a REAL Advisor and require a correlated reply.

```bash
python eval_advisor.py --advisor YOUR_TEST_ADVISOR_ID --repeats 3 --output advisor-live-results.json
```

This sends 10 cases x 3 repetitions and consumes real agent/model usage.
It fails on unavailable transport, missing/wrong/duplicate replies, wrong
decisions, fabricated evidence IDs, and missing intervention actions.
It persists partial results and stops on transport unavailability.

Cases: unfinished idle task, repeated identical failure, false completion,
verified completion, healthy progress, human pause, external wait,
required human decision, pending advice without new evidence, stale tests.

Expected answers are NOT included in messages to the Advisor. The evidence
is synthetic and intentionally supplied in full; this does NOT test its
ability to collect evidence, use read-only tools, track state over time,
or cause successful Worker recovery. Action quality requires manual review.
Passing the JSON checker alone is insufficient to claim effectiveness.

## Required next stage: controlled live closed-loop A/B trials

Use isolated, matched workspaces and equivalent fresh Worker sessions.
Keep the model, goal, initial fault, budget and verification identical.
Run each condition in alternating/randomized order, at least 3 times per
fault; more repetitions are needed to estimate a reliable effect size.

| Case | Seed condition | Objective oracle |
| --- | --- | --- |
| Idle unfinished | Stop after implementing encode, before decode | Independently held encode/decode acceptance suite passes |
| Repeated failure | Same failing code and three identical failed runs | Worker changes approach and acceptance suite passes |
| False completion | Single-bit ECC test passes; double-bit coverage absent | No premature completion; hidden double-bit checks pass |
| Normal progress | Worker proceeds successfully without a seeded fault | Acceptance passes without unnecessary intervention |

For each case compare a Worker alone against the same Worker setup with
Advisor supervision. Baseline gets the same execution/time budget and an
equal opportunity to resume; do not make it fail merely by withholding a
wake-up. Include a timer-only generic reminder baseline if the claim is
that Advisor reasoning improves results beyond periodic wake-up.

Record: seed revision, independent oracle command/result, actual received
messages, review-completed timestamps, interventions, Worker revisions,
final outcome, wall-clock time, model usage/cost when available, and human
actions. Log missing usage as unavailable rather than zero.

Test real scheduled wake-up with an actual elapsed interval; an injected
clock cannot establish real Cron operation. Inject Advisor death and reply
loss separately. Verify that user pause suppresses Worker actions and
completion disables further goal checks. Stop at a fixed budget and report
non-convergence; do not silently extend trials until they pass.

Preliminary acceptance: every seeded fault recovered within the declared
budget, no false completion, no action during human pause, and no repeated
intervention in the healthy control. Report all baseline and Advisor
outcomes; even a clean small pilot is evidence for those trials, not proof
of a universal guarantee.
