# Code layout — production vs test vs dev

Date: 2026-06-16

Top-level directories are **physically separated**. Do not mix concerns across trees.

```text
cam/
├── src/camc_pkg/proxy/     # production IHUB proxy (embedded in dist/camc)
├── dev/ihub_proxy/         # dev/reference standalone proxies (NOT shipped)
├── benchmarks/             # E2E harnesses (NOT imported by camc)
├── tests/proxy/            # automated tests for src/camc_pkg/proxy/
└── docs/debug-notes/       # post-mortems
```

## Production (shipped)

| Path | Role |
|------|------|
| `src/cam/` | Full cam server |
| `src/camc_pkg/` | Standalone camc core (cli, monitor, api_store, …) |
| `src/camc_pkg/proxy/` | **IHUB proxy** — `common.py`, `messages.py`, `textual_tools.py`, `manager.py` |
| `src/cam/adapters/configs/*.toml` | Adapter configs (embedded by `build_camc.py`) |
| `dist/camc` | Built single-file deploy |

Fix proxy bugs in `src/camc_pkg/proxy/`, then:

```bash
python3 build_camc.py && cp dist/camc ~/.cam/camc
```

## Tests (automated)

| Path | Role |
|------|------|
| `tests/` | Unit tests (CLI, adapters, storage, …) |
| `tests/proxy/` | Proxy translator + model resolution tests |

```bash
pytest
pytest tests/proxy
```

Tests import **`camc_pkg.proxy.*` only**, never `dev/ihub_proxy/`.

## Benchmarks (manual)

| Path | Role |
|------|------|
| `benchmarks/api-model-compare/` | Agent E2E compare harness |
| `benchmarks/api-model-compare/runs/` | **Generated** — gitignored |

See `benchmarks/README.md`.

## Dev / reference (not production)

| Path | Role |
|------|------|
| `dev/ihub_proxy/` | Standalone proxy scripts for manual curl / CC Switch comparison |
| `dev/ihub_proxy/litellm-glm-proxy.yaml` | LiteLLM reference config |

**Do not edit `dev/ihub_proxy/` for production fixes.** It may drift from `src/camc_pkg/proxy/`.

## Docs

| Path | Role |
|------|------|
| `docs/camc-api-proxy-plan.md` | Product plan |
| `docs/inference-hub.md` | Operator guide |
| `docs/debug-notes/` | Debug session logs |

## Decision rule

| Change | Where |
|--------|--------|
| Proxy bug | `src/camc_pkg/proxy/` → rebuild camc |
| Regression test | `tests/proxy/` |
| Benchmark task | `benchmarks/…/` |
| Manual experiment | `dev/ihub_proxy/` |
| Debug write-up | `docs/debug-notes/` |
