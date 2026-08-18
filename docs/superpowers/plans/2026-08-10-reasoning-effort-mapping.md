# Claude Reasoning Effort Mapping Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Record per-model reasoning effort capabilities and loss-aware Claude mappings in `api-models.json` without silently dropping a model's highest available effort.

**Architecture:** Keep the durable model matrix in each API profile's independent `reasoning` JSON object, separate from synced `metadata` so `camc api check` cannot erase it. Python owns schema normalization and compatibility defaults. Claude and Codex API profiles are supported; Claude effort mapping remains the only tool-specific reasoning mapping in this phase. Unknown model capability is represented explicitly as `verified: false`, not guessed away.

**Tech Stack:** Python 3 stdlib, JSON, pytest, `build_camc.py` bundle generation.

## Global Constraints

- Scope effort-mapping support to Claude; Codex uses its native Responses wire path and generated catalog, while Cursor and arbitrary-tool API behavior remain unsupported.
- Preserve existing `reasoning_levels`, Codex catalog generation, API profile names, and normal login defaults.
- Do not silently downgrade a requested maximum effort; mappings must either preserve the level, use an explicit documented saturation fallback, or report it as unsupported.
- `camc run -t cursor --api NAME` and arbitrary-tool API runs must fail with a clear unsupported-tool error before starting a proxy or agent.
- NVIDIA model/API requests are read-only; no credentials are stored in source or tests.
- Do not commit or push during implementation.

---

### Task 1: Define the Claude reasoning profile schema and normalization

**Files:**
- Modify: `src/camc_pkg/api_metadata.py`
- Modify: `src/camc_pkg/api_store.py`
- Modify: `src/camc_pkg/api_resolver.py`
- Test: `tests/test_api_metadata.py`
- Test: `tests/test_camc_api.py`

**Interfaces:**
- `normalize_reasoning_profile(value) -> dict` returns a stable Claude profile with `supported`, `mapping`, `verified`, and optional `source` fields.
- `resolve_claude_effort(profile, requested) -> str` returns the mapped Claude value or raises a clear `ValueError` when no safe mapping exists.
- `validate_api_run()` rejects any API profile when `tool != "claude"`.
- `merge_curated_apis()` preserves custom `reasoning` data and seeds only missing curated defaults.

- [ ] **Step 1: Write the failing tests**

  Add tests for: missing profile normalization; Claude identity mappings; preserving `max` instead of truncating it; non-Claude API rejection; and `merge_curated_apis` retaining a custom profile across refresh.

- [ ] **Step 2: Run the focused tests to verify RED**

  Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/cam-node-transport-venv/bin/python -m pytest -q tests/test_api_metadata.py tests/test_camc_api.py`

  Expected: FAIL because the normalization and resolver interfaces do not exist.

- [ ] **Step 3: Implement the minimal schema helpers**

  Add Claude's canonical agent options:

  ```python
  AGENT_EFFORT_OPTIONS = {
      "claude": ("low", "medium", "high", "xhigh", "max"),
  }
  ```

  Normalize only known string values, preserve every mapping entry, and reject a requested level whose mapping is absent. Do not convert `max` or `ultra` to `high` implicitly.

- [ ] **Step 4: Run the focused tests to verify GREEN**

  Run the same pytest command. Expected: all focused API tests pass.

---

### Task 2: Add the Claude model capability/mapping matrix to JSON seed data

**Files:**
- Modify: `src/camc_pkg/api_store.py`
- Modify: `src/camc_pkg/api_metadata.py`
- Test: `tests/test_api_metadata.py`
- Test: `tests/test_camc_api.py`

**Interfaces:**
- Curated seed profiles expose `reasoning` for Claude only.
- `ensure_ready()` writes the same profile into `~/.cam/api-models.json` without changing the selected default.

- [ ] **Step 1: Write failing seed/matrix tests**

  Assert that every current curated profile has a Claude mapping, that `max` remains representable, and that a custom JSON `reasoning` object survives `ensure_ready()`/`merge_curated_apis()`.

- [ ] **Step 2: Run tests to capture RED**

  Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/cam-node-transport-venv/bin/python -m pytest -q tests/test_api_metadata.py tests/test_camc_api.py`

  Expected: FAIL because curated seed entries currently have no `reasoning` object.

- [ ] **Step 3: Add conservative, explicit matrix data**

  Store the model's declared levels separately from Claude's agent options. For models whose upstream levels are not published by the cost map, use `verified: false` and retain the full Claude option mapping as a direct, inspectable candidate mapping; do not claim a live request succeeded. Keep the new DeepSeek Flash and Kimi K3 variants out of curated defaults until a token-backed test confirms their exact access and model IDs.

- [ ] **Step 4: Run tests to verify GREEN**

  Run the focused command again and assert that JSON round-tripping retains `reasoning` after metadata sync.

---

### Task 3: Preserve compatibility and bundle the Claude-only schema

**Files:**
- Modify only if needed: `src/camc_pkg/api_resolver.py`
- Test: `tests/test_api_metadata.py`
- Generated: `dist/camc`, `dist/BUILD_LOG.md` only if the normal generator changes them

**Interfaces:**
- Existing `reasoning_levels` remains the Codex catalog field.
- New `reasoning` data is available for future launch-option wiring but does not alter `camc run` defaults in this phase.
- Existing non-API Codex behavior remains unchanged; API-mode Codex uses isolated `CODEX_HOME=~/.codex-api/`.

- [ ] **Step 1: Add compatibility tests**

  Verify old profiles with no `reasoning` still resolve; profiles with `reasoning` do not change `context_window`, `max_output_tokens`, or the generated Codex catalog; and `camc run -t codex --api NAME` resolves native Responses or its completion fallback before startup.

- [ ] **Step 2: Run RED, then implement only required compatibility code**

  Run the focused tests, make the smallest changes, and rerun until green.

- [ ] **Step 3: Build and smoke-test the bundle**

  Run: `PYTHONPATH=src python3 build_camc.py`

  Then verify `dist/camc --version`, `dist/camc api --help`, and that the generated bundle contains the new schema helpers.

- [ ] **Step 4: Full relevant verification**

  Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/cam-node-transport-venv/bin/python -m pytest -q tests/test_api_metadata.py tests/test_camc_api.py tests/test_api_routing.py`

  Run: `git diff --check`.

## Capability verification boundary

The public NVIDIA cost map exposes context/token metadata but does not reliably enumerate reasoning effort values for these deployments. A later token-backed Claude probe may mark individual profiles `verified: true`; this plan deliberately records uncertainty instead of fabricating successful model support. The probe must never print or persist the API token.
