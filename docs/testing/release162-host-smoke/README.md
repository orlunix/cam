# v1.2.162 CLI auto launcher smoke, 2026-10-12

Exact artifact SHA256: e976b7fe14d7bc4418366e5d2e4440da637cb970f8bc93f0721589aeaa261873.

Each host ran the candidate with:

```text
python3 <candidate> run -t <claude|codex> -n release162-<host>-<tool> -p <disposable-workdir> --tool-launcher auto --no-default-api 'Reply exactly CAMC_FALLBACK_<HOST>_<TOOL>_OK. Do not use tools or change files.'
```

| Host | Actual path | Claude reply | Codex reply |
|---|---|---|---|
| Local | battach, existing job 1796124140 | passed | passed |
| SITE-E | LSF tools unavailable, local fallback | passed after test-workspace trust confirmation | passed |
| SITE-C | battach, existing job 1042549372 | failed: not logged in after first-run trust and autorun confirmations | failed: old 0.147.0 CLI, gpt-5.6-luna rejected for ChatGPT account |

SITE-C's separate test with `--tool-args '--model gpt-5.4'` was also
rejected for the ChatGPT account. Both Codex tests required confirming a
rebuilt isolated log database. No donor database was manually changed.

No qsub jobs were created. Scheduler wait/submission output did not appear
in the captured `camc run` stdout. This run verifies SITE-E's unavailable-LSF
local fallback and local/SITE-C battach startup, but does not verify live SSH
or qsub fallback stages. It is not a complete shared release gate pass.

All seven test agents were killed and removed; test tmux sessions were
checked absent. Existing allocations were preserved. Production entry
paths were not changed.
