# camc API Proxy & Inference Hub Integration — Plan

Date: 2026-06-16
Status: **plan only** (not implemented)
Extends: `docs/camc-custom-api-profiles.md`, `docs/inference-hub.md`

## Summary

Integrate NVIDIA Inference Hub (120+ models per key) into camc as a first-class **`--api`** workflow with:

1. **Python 3.6**-compatible proxy code (same baseline as camc)
2. **Embedded in the single-file `camc` build** (no separate `scripts/` deploy)
3. A clear **camc ↔ API interface** (resolver + proxy manager + env merge)
4. **Minimal API CLI** — `camc api list | check` for users; `camc api proxy` debug-only
5. **IHUB catalog refresh** via `camc api check` (updates `_catalog` + `enabled`)
6. **Auto-proxy** on `camc run --api` — `ProxyManager.ensure()`; users never start proxy manually

Prototype originally lived in `dev/ihub_proxy/` (Python 3.10+, external process, manual `start-ihub-proxy.sh`). Production code is now in `src/camc_pkg/proxy/` (embedded in `dist/camc`).

---

## Supported today (implemented)

| | |
|---|---|
| **Tool** | **Claude only** — `camc run -t claude --api NAME` |
| **Models** | 6 curated NVIDIA IHUB profiles: `glm-5.1`, `deepseek-v4-pro`, `kimi-k2.6`, `minimax-m2.7`, `qwen3-5-397b`, `nemotron-3-ultra` |
| **Login** | Isolated `CLAUDE_CONFIG_DIR=~/.cam/claude-api/`; subscription login in `~/.claude/` unchanged when `--api` is omitted |
| **Not supported** | `camc run -t codex --api …` and `camc run -t cursor --api …` — resolver prints an error and exits |
| **Custom APIs** | User-added `apis.*` entries are rejected until explicitly allowlisted |

### Codex / Cursor (documented, not implemented)

Future Codex support will use the same **login isolation** pattern as Claude:

- Normal run: `camc run -t codex` → `~/.codex/` OAuth/login unchanged
- API run (future): `CODEX_HOME=~/.cam/codex-api/` + `openai_base_url` → local proxy (`completions_to_responses` on :18325)
- **Do not** mutate `~/.codex/auth.json` for `--api` switching

Until implemented, attempting `--api` with codex/cursor prints:

```text
--api is not supported for tool 'codex'. Only Claude is supported today ...
```

**Debug history (2026-06-16 benchmark):** see `docs/debug-notes/api-bench-ihub-proxy.md`.

---

## Simplified design (canonical)

**Everything is based on one file: `~/.cam/api-models.json`.**

**Providers exist internally** (URLs, auth keys, IHUB vs CC Switch) but are **not**
CLI flags. Users pick **`--api NAME`** only; camc resolves `apis.NAME` → `provider` → endpoints.

**Proxy is not an API property.** `glm-5.1` does not know whether you will run
Claude, Codex, or Cursor. **`camc run -t TOOL --api NAME`** decides:

1. Tool wire protocol (from adapter TOML, e.g. Claude → `anthropic_messages`)
2. API upstream endpoint (from `providers` + `apis.model`)
3. Whether a protocol translator proxy is needed, and which route

**Only `-t claude` is supported today** (6 curated models). Codex/Cursor `--api` prints an error.

No separate `providers.json`. No `--api-provider`, `--api-url`, or `--api-model` on `camc run`.

### Mental model

```text
api-models.json                 camc run
  providers.*  ──upstream──┐    -t claude  ──tool protocol──┐
  apis.glm-5.1 ──model─────┼──► --api glm-5.1               ├──► RunResolver
                           │                                 │      → direct | proxy + route
  (no proxy, no tools)     └─────────────────────────────────┘      → ProxyManager.ensure() (automatic)
```

Users **never** run proxy commands in normal workflow — proxy starts/stops with `camc run`.

Token secrets stay in `~/.cam/token.env` (never in api-models.json).

### CLI (user-facing)

| Use | Command |
|-----|---------|
| Run (proxy auto) | `camc run -t claude --api glm-5.1 "task"` |
| List APIs | `camc api list` |
| IHUB health + enable refresh | `camc api check` |
| **Edit config** | `$EDITOR ~/.cam/api-models.json` |
| **Edit token** | `$EDITOR ~/.cam/token.env` |

**Debug only** (not in default help): `camc api proxy start|status|logs|stop` — see §6 route table.

**Only `--api NAME`** on `camc run`. No manual proxy step (replaces `start-ihub-proxy.sh`).

**No `camc api add/rm/promote/show/sync/validate/auth/alias/init`** — edit JSON directly.

| Exposed on CLI | Hidden in JSON / resolver |
|----------------|----------------------------|
| `--api glm-5.1` | `apis.glm-5.1.provider` → `inference-hub` |
| `camc api list` | reads `apis`, `_aliases`, `enabled` |
| `camc api check` | pings provider, updates `enabled` + `_catalog` |
| *(none)* | `providers.*.endpoints`, `auth_key` |

### `api-models.json` schema

```json
{
  "default": "glm-5.1",
  "default_provider": "inference-hub",
  "providers": {
    "inference-hub": {
      "display_name": "NVIDIA Inference Hub",
      "auth_key": "inference_hub",
      "env_names": ["INFERENCE_HUB_TOKEN", "INFERENCE_HUB_API_KEY", "INFERENCE_API_KEY"],
      "base_url": "https://inference-api.nvidia.com/v1",
      "endpoints": {
        "openai_chat_completions": "/chat/completions",
        "anthropic_messages": "/messages"
      }
    },
    "cc-switch": {
      "display_name": "CC Switch",
      "auth_key": "cc_switch",
      "env_names": ["CC_SWITCH_API_KEY", "OPENAI_API_KEY"],
      "base_url": "http://127.0.0.1:15785/v1",
      "endpoints": {
        "openai_chat_completions": "/chat/completions"
      }
    }
  },
  "apis": {
    "glm-5.1": {
      "provider": "inference-hub",
      "model": "nvidia/zai-org/eccn-glm-5.1",
      "aliases": ["glm"]
    },
    "cc-switch-ds": {
      "provider": "cc-switch",
      "model": "deepseek-chat",
      "url": "http://127.0.0.1:15785/v1/chat/completions"
    }
  },
  "_templates": { },
  "_aliases": { "glm": "glm-5.1" },
  "_catalog": { "synced_at": "…", "ids": ["…"] }
}
```

Per **API**: `model`, `provider`, optional full `url` override, `aliases`. **No `proxy`, no `tools`, no tool-specific dirs.**

Per **provider**: `base_url`, `endpoints` (protocol → path suffix), `auth_key`, `env_names`.
Path does not have to contain `chat` — protocol is explicit (`openai_chat_completions`, `anthropic_messages`, `openai_embeddings`, …). `RunResolver` picks the endpoint for the tool/upstream pair.

**Proxy decision** happens only in `RunResolver.resolve(tool, api)` at `camc run` time.

### `camc api` — user commands

```bash
camc api list [--all]    # show apis (enabled curated + user)
camc api check           # ping IHUB, refresh enabled + _catalog
```

**Debug subcommand** (hidden from `camc api --help`; for troubleshooting only):

```bash
camc api proxy start ROUTE [options]   # manual start (see route table below)
camc api proxy status | logs [--follow] | stop [route]
```

Normal path: `camc run --api …` calls `ProxyManager.ensure()` — no manual `proxy start`.

Auto-create `api-models.json` on first `camc run --api` or `camc api list` (6 curated + 34 templates).

**Add/change APIs:** edit `~/.cam/api-models.json` (copy from `_templates`, set `model`/`provider`/`aliases`).
**Tokens:** edit `~/.cam/token.env` directly.

## Goals

```bash
camc run -t claude --api glm-5.1 "fix the bug"
camc run -t claude --api glm "fix the bug"    # via _aliases

# edit ~/.cam/api-models.json to add qwen36, then:
camc run -t claude --api qwen36 "task"

camc api list
camc api check
# proxy starts automatically on camc run — no manual step
```

**`--api NAME`** — lookup in `~/.cam/api-models.json` → `apis.NAME` (or alias).

## Non-Goals (phase 1)

- LiteLLM dependency
- SQLite (stay JSON + fcntl like camc v3 direction)
- Storing raw API keys in repo or `agents.json`
- **`--api-provider` or any provider flag on `camc run`** (provider is JSON-only)
- Generic “proxy anything” — only **table-driven routes** with tests
- Cursor API override (until adapter contract verified)

---

## Architecture

```text
camc run -t claude --api glm-5.1
        │
        ▼
┌───────────────────┐
│  RunResolver      │  tool (adapter) + api + provider → ApiPlan
│                   │  • match protocols → direct | proxy + route
│                   │  TokenResolver → bearer
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐     direct URL          ┌─────────────────┐
│  ProxyManager     │ ───────────────────────►│ Upstream API    │
│  (only if plan     │     or shared proxy ───►│ chat/messages   │
│   needs proxy)    │                         └─────────────────┘
└─────────┬─────────┘
          ▼
   runtime_env → tmux
```

### Direct vs proxy (decided at run, not in apis)

| Input | Resolver sees | Result |
|-------|---------------|--------|
| `-t claude` + IHUB chat endpoint | tool=`anthropic_messages`, upstream=`openai_chat_completions` | **proxy** → `completions_to_messages` |
| `-t claude` + endpoint `/messages` | both `anthropic_messages` | **direct** |
| `-t codex` + chat upstream | tool=`openai_responses`, upstream=`openai_chat_completions` | **proxy** → `completions_to_responses` (P1) |

Route table lives in **adapter TOML** (`[[api.proxy_routes]]`) + embedded defaults — not in `apis.*`.

```text
RunResolver.resolve(tool, api):
  provider = providers[api.provider]
  upstream_proto = protocol needed for this tool+api (from adapter + provider endpoints)
  upstream_url = api.url || join(provider.base_url, provider.endpoints[upstream_proto])
  tool_proto = adapter[tool].wire_protocol
  if tool_proto matches upstream_proto → mode=direct, base=upstream_url
  elif route = lookup_proxy_route(tool_proto, upstream_proto) → mode=proxy, ProxyManager.ensure(route, upstream_url)
  else → error (or fail if --no-api-proxy)
  # tool-specific env (e.g. CLAUDE_CONFIG_DIR) applied here — not stored on api
```

---

## 1. Python 3.6 Reorganization

### Current problem

`dev/ihub_proxy/` used Python 3.10+ syntax (`str | None`, bare `dict[str, Any]`, `datetime.now(timezone.utc)`). Production modules under `src/camc_pkg/proxy/` are Py3.6-safe and embedded by `build_camc.py`.

### Target layout (source)

Move into `src/camc_pkg/` as stdlib-only modules:

```text
src/camc_pkg/
  api/
    __init__.py          # re-exports (package; stripped at build)
    protocols.py         # enum-like protocol constants
    providers.py         # load/save ~/.cam/api-models.json
    catalog.py           # optional IHUB sync → _catalog
    token_resolver.py    # 4-source token chain
    resolver.py          # RunResolver: tool + api → ApiPlan (direct vs proxy)
    proxy_manager.py     # start/stop/ensure subprocess proxies
    proxy_routes.py      # route registry (completions_to_messages, …)
  proxy/
    common.py            # HTTP helpers, model aliases
    textual_tools.py     # textual tool markup → tool_use (GLM, DSML, JSON, …)
    debug_log.py         # optional JSONL (Py3.6 safe)
    route_messages.py    # completions → anthropic messages
    route_responses.py   # completions → openai responses
```

### Py3.6 coding rules

| Avoid | Use |
|-------|-----|
| `str \| None`, `dict[str, Any]` | `typing.Optional`, `typing.Dict`, `typing.List` |
| `datetime.now(timezone.utc)` | `datetime.utcnow().isoformat() + "Z"` |
| f-strings (optional ban) | `%` formatting (match existing camc_pkg style) |
| `subprocess.run` timeout on 3.6 | `Popen` + `communicate(timeout=…)` with fallback |
| External deps | stdlib only |

### Tests

- Run proxy unit tests under **3.6 and 3.12** in CI (matrix or container)
- `pytest tests/test_api_resolver.py`, `tests/test_proxy_routes.py`

---

## 2. Embed in Single `camc` File

Follow `build_camc.py` pattern (same as `_monitor`, adapters TOML):

### Build changes

Add to `MODULE_ORDER` (before `cli`):

```text
api/protocols
api/providers
api/catalog
api/resolver
api/proxy_manager
proxy/common
proxy/textual_tools
proxy/debug_log
proxy/route_messages
proxy/route_responses
api/proxy_routes
```

### Hidden CLI entry (like `camc _monitor`)

```bash
camc _proxy completions_to_messages --port 18324 --upstream-model nvidia/zai-org/eccn-glm-5.1 [--debug]
```

`ProxyManager` starts proxies by **re-execing the same camc binary**:

```python
subprocess.Popen([
    sys.argv[0], "_proxy", route_name,
    "--port", str(port),
    "--upstream-model", upstream_model,
    ...
], stdin=DEVNULL, start_new_session=True)
```

Benefits:

- One artifact to `cam sync` (no `~/.cam/ihub-proxy/*.py` drift)
- Same Python version as camc on Py3.6 hosts
- No separate `start-ihub-proxy.sh` required — `ProxyManager.ensure()` on `camc run`

### Embedded route table

Small dict inlined at build time (like `_EMBEDDED_CONFIGS`):

```python
_EMBEDDED_PROXY_ROUTES = {
    "completions_to_messages": {
        "frontend": "anthropic_messages",
        "upstream": "openai_chat_completions",
        "default_port": 18324,
        "entry": "route_messages",
    },
    "completions_to_responses": {
        "frontend": "openai_responses",
        "upstream": "openai_chat_completions",
        "default_port": 18325,
        "entry": "route_responses",
    },
}
```

---

## 3. camc ↔ API Interface

### Core types (conceptual)

**ApiPlan** — output of resolver, input to launch + agent record:

```json
{
  "name": "glm-5.1",
  "provider": "inference-hub",
  "tool": "claude",
  "model": "nvidia/zai-org/eccn-glm-5.1",
  "mode": "proxy",
  "route": "completions_to_messages",
  "upstream_url": "https://inference-api.nvidia.com/v1/chat/completions",
  "local_base_url": "http://127.0.0.1:18324",
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:18324",
    "ANTHROPIC_API_KEY": "sk-camc-local",
    "ANTHROPIC_MODEL": "glm-5.1",
    "CLAUDE_CONFIG_DIR": "~/.cam/claude-glm-5.1"
  },
  "auth": {
    "source": "token.env",
    "key_name": "inference_hub"
  }
}
```

`CLAUDE_CONFIG_DIR` is **computed at run** (`~/.cam/claude-<api-name>/`), not stored on the API entry.

**Never persist** token values in `agents.json` — only `name`, `tool`, `mode`, `route`, URLs, and `auth.source`/`auth.key_name`.

### 3.1 `camc run` — `--api` only

All backend config comes from **`~/.cam/api-models.json`**.

| Flag | Purpose |
|------|---------|
| `--api NAME` | Key in `apis` (or `_aliases`) |
| `--api-token TOKEN` | One-off bearer (optional) |
| `--no-api-proxy` | Fail if resolver would use a proxy (force direct-only) |
| `--proxy-debug` | JSONL debug logs |

#### Config changes (JSON only — no `camc api add`)

```bash
$EDITOR ~/.cam/api-models.json    # add apis, aliases, providers
$EDITOR ~/.cam/token.env          # INFERENCE_HUB_TOKEN=...
```

Copy a `_templates` entry into `apis` to enable a new model. Rebuild `_aliases` happens on next `camc api list` or `camc run --api` (read path validates + rebuilds index).

#### Resolution (provider is internal)

```text
--api NAME  →  apis[resolve_alias(NAME)]
               provider = api.provider || default_provider
               url   = api.url || provider.base_url + provider.endpoints[upstream_proto]
               auth  = providers[provider].auth_key  → TokenResolver env_names
               model = api.model
```

User never types `inference-hub` on `camc run` — only API names like `glm-5.1`.

Preview resolved env: `camc env --tool claude --api glm-5.1`

### Integration point in `cmd_run`

```text
1. plan = RunResolver.resolve(api-models.json, --api NAME, -t TOOL)
2. token = TokenResolver(plan.provider.auth_key)
3. if plan.mode == "proxy": ProxyManager.ensure(plan.route, plan.upstream_url)
4. merge env → preflight → tmux
5. agents.json["api"] = { name, model, provider, tool, mode, route }   # no secrets
```

`camc env --tool claude --api glm-5.1` runs the same resolver without launching tmux.

### Adapter TOML (`claude.toml`)

```toml
[api]
preferred_protocols = ["anthropic_messages"]
direct_protocols = ["anthropic_messages"]

[api.env.anthropic_messages]
base_url = "ANTHROPIC_BASE_URL"
token = "ANTHROPIC_API_KEY"
token_alt = "ANTHROPIC_AUTH_TOKEN"
model = "ANTHROPIC_MODEL"
fast_model = "ANTHROPIC_FAST_MODEL"
isolated_config_dir = "CLAUDE_CONFIG_DIR"

[[api.proxy_routes]]
from = "openai_chat_completions"
to = "anthropic_messages"
route = "completions_to_messages"
```

---

## 4. Token Resolution (multi-source fallback)

Tokens are **never** stored in `api-models.json` or `agents.json`. A dedicated
`TokenResolver` tries sources in order and stops at the first hit.

### Priority chain (highest first)

| # | Source | Location / flag | Notes |
|---|--------|-----------------|-------|
| 1 | **CLI pass-through** | `camc run --api-token TOKEN` | One-off; not written to disk |
| 2 | **Environment** | `INFERENCE_HUB_TOKEN`, `INFERENCE_HUB_API_KEY`, `INFERENCE_API_KEY`, `OPENAI_API_KEY` | First non-empty wins (profile may narrow list) |
| 3 | **CAM token file** | `~/.cam/token.env` | Primary persistent store; chmod 600 |
| 4 | **User YAML** | `~/.my_tokens.yaml` | Flat `key: value` only (stdlib parser, no PyYAML) |

Legacy compat: `~/.cam/inference-hub.env` is read as an alias of source #3 if
`token.env` is missing (same `KEY=value` format).

### `~/.cam/token.env` format (CAM standard)

Shell-compatible, one assignment per line:

```bash
# ~/.cam/token.env  (chmod 600)
inference_hub=nvapi-xxxxxxxx
# aliases for the same key are ok:
INFERENCE_HUB_TOKEN=nvapi-xxxxxxxx
```

Profile references the logical key name, not the secret:

```json
"auth_key": "inference_hub"
```

### `~/.my_tokens.yaml` format (flat subset)

Stdlib-only parser: top-level `key: value` lines only (no nesting in P0):

```yaml
# ~/.my_tokens.yaml
inference_hub: nvapi-xxxxxxxx
openai: sk-...
```

Key lookup uses profile `auth_key` with normalisation (`inference_hub`,
`inference-hub`, `INFERENCE_HUB_TOKEN` treated as equivalent).

### Auth keys (via `providers.*`, no secrets in JSON)

Each provider declares `auth_key` + `env_names`. Values live in `token.env` only.
Token lookup uses `auth_key` with normalisation (`inference_hub`, `inference-hub`,
`INFERENCE_HUB_TOKEN` treated as equivalent).

### Diagnostics

```bash
camc api check
# → inference-hub: reachable, 42 models on key
# → glm-5.1: enabled | nemotron-3-ultra: disabled (id_not_on_key)
# → updated _catalog.ids

camc api list --all
```

On token failure: print provider name, tried env names, hint to edit `~/.cam/token.env`.

---

## 5. `api-models.json` — single source of truth

**Answer: hybrid — not 100% user, not 100% code-fixed.**

Three layers with clear rules:

```text
┌─────────────────────────────────────────────────────────────┐
│ Layer A: Embedded presets (in camc binary)                    │
│   Small, versioned seed — copied once, never read at runtime  │
│   after ~/.cam/api-models.json exists                         │
├─────────────────────────────────────────────────────────────┤
│ Layer B: User runtime file (~/.cam/api-models.json)           │
│   SOURCE OF TRUTH for camc run --api                          │
│   User edits freely; camc never overwrites without consent    │
├─────────────────────────────────────────────────────────────┤
│ Layer C: Optional IHUB catalog (_catalog in same file)        │
│   Reference only — ids from GET /v1/models, not profiles      │
└─────────────────────────────────────────────────────────────┘
```

| Approach | Verdict | Why |
|----------|---------|-----|
| **Totally user-created** | ❌ too harsh | Empty install → nobody knows GLM URLs/proxy mode |
| **Totally code-fixed** | ❌ wrong | 120+ IHUB models change; URLs/modes differ per model; users add private endpoints |
| **Hybrid (chosen)** | ✅ | Seed gets you running; user owns live config; sync is opt-in scaffolding |

### Layer A — Curated defaults (6 models, `nvidia/*` only)

Ship **6 NVIDIA-hosted models** (final list, 2026-06-16). No Azure/AWS/GCP routes.
GPT-OSS was dropped from curated defaults after benchmark showed unreliable tool-call
format via the Anthropic proxy path; it remains available under `_templates` if needed.
Curated APIs only set `provider` + `model`; proxy for Claude is chosen at `camc run -t claude`.

Shared endpoints live on the provider (not repeated per API):

```text
providers.inference-hub.base_url:   https://inference-api.nvidia.com/v1
providers.inference-hub.endpoints:
  openai_chat_completions → /chat/completions
  anthropic_messages      → /messages
```

| API key | `model` | Short aliases |
|---------|---------|---------------|
| `glm-5.1` | `nvidia/zai-org/eccn-glm-5.1` | `glm`, `glm51` |
| `deepseek-v4-pro` | `nvidia/deepseek-ai/eccn-deepseek-v4-pro` | `deepseek`, `ds-v4` |
| `kimi-k2.6` | `nvidia/moonshotai/eccn-kimi-k2.6` | `kimi`, `k2.6` |
| `minimax-m2.7` | `nvidia/minimaxai/eccn-minimax-m2.7` | `minimax`, `m2.7` |
| `qwen3-5-397b` | `nvidia/qwen/eccn-qwen3-5-397b-a17b` | `qwen397`, `qwen3.5` |
| `nemotron-3-ultra` | `nvidia/nvidia/eccn-nemotron-3-ultra` | `nemotron`, `nemo-ultra` |

**ID corrections** (typo in source list → catalog id):

| As typed | Canonical IHUB id |
|----------|-------------------|
| `nvidia/qwen/eccn-qwen3-5-397-a17b` | `nvidia/qwen/eccn-qwen3-5-397b-a17b` |

Default API when user runs without `--api` (future): top-level `"default": "glm-5.1"`.
All curated NVIDIA models use `"provider": "inference-hub"` internally.

#### Final curated roster (locked)

```json
"glm-5.1": {
  "provider": "inference-hub",
  "model": "nvidia/zai-org/eccn-glm-5.1",
  "enabled": false,
  "aliases": ["glm", "glm51"]
}
```

**Priority rule:** when two profiles share an alias or compete for the same
`(tool, endpoint_url)` slot, **higher `priority` wins**. Curated = `100`, user
additions = `50` (override with `"priority": 110` if user explicitly wants to
replace a default).

User-added profiles never auto-delete curated entries; they coexist. User wins
only if they set higher priority or use a unique API key / alias.

#### Template profiles (remaining `nvidia/*` models)

The other **34** `nvidia/*` ids (40 total − 6 curated) ship in the same
`api-models.json` under **`_templates`** — **not** under `profiles`.

Purpose: copy-paste starting point for users; no need to hand-type model ids.

| Property | `apis` (curated/user) | `_templates` |
|----------|----------------------|--------------|
| Usable with `camc run --api` | yes (if enabled) | **no** |
| In `_aliases` index | yes (when enabled) | **no** |
| Auto `enabled` on IHUB check | curated only | **never** |
| `tier` | `curated` / `user` | `template` |
| Editable by user | yes | yes (safe to tweak before promote) |

Template entry shape (same URL block as curated; `kind` hints usage):

```json
"_templates": {
  "nvidia/zai-org/eccn-glm-5": {
    "provider": "inference-hub",
    "model": "nvidia/zai-org/eccn-glm-5",
    "kind": "chat",
    "suggested_api_key": "glm-5"
  },
  "nvidia/nvidia/eccn-llama-embed-nemotron-8b": {
    "provider": "inference-hub",
    "model": "nvidia/nvidia/eccn-llama-embed-nemotron-8b",
    "kind": "embedding",
    "suggested_api_key": "llama-embed-nemotron-8b"
  }
}
```

**`kind` values** (metadata for promote / list only — not proxy or tool binding):

- `chat` — chat/completions upstream (Claude may need proxy at run time)
- `embedding` — embeddings endpoint
- `rerank` — rerank endpoint

**Promote template → active profile:**

```bash
# Copy template into apis.my-name (editable), enabled=true
camc api models promote nvidia/zai-org/eccn-glm-5
camc api models promote nvidia/zai-org/eccn-glm-5 --as glm-5

# List templates only
camc api models list --templates
camc api models list --templates --kind chat
```

Promote copies the template dict into `apis.<key>`, sets `enabled: true`,
rebuilds `_aliases`, and does **not** remove the template (template stays for
re-copy). User then edits `apis.<key>` (aliases, url).

Hand-edit workflow (no CLI): duplicate a `_templates` block into `apis`
under a new key, set `enabled: true`.

**Init file layout:**

```text
api-models.json
  default_provider    # inference-hub (internal default for new apis)
  providers           # gateway plugins: URLs + auth (not CLI targets)
  apis                # 6 curated + user APIs (what --api references)
  _templates          # 33 nvidia/* stubs (promote → apis)
  _catalog / _aliases
```

### Auto-bootstrap (no manual `api init` required)

**You should not need `camc api init`.** Config is created lazily the first time
you use an API feature.

**Triggers** (if `~/.cam/api-models.json` is missing):

| Command | Auto-bootstrap? |
|---------|-----------------|
| `camc run --api …` | yes — write seed + enable check |
| `camc api list` / `check` | yes |
| `camc run -t claude` (no `--api`) | no — subscription/login path unchanged |

**Bootstrap sequence** (`ApiStore.ensure_ready()`):

```text
1. mkdir -p ~/.cam/
2. If api-models.json missing → write embedded preset (providers + 7 apis + 33 _templates)
3. If token found (TokenResolver):
     GET https://inference-api.nvidia.com/v1/models (5s timeout)
     If OK → set apis.*.enabled=true/false per id on your key
     If fail → apis stay enabled=false; one-line warn (network or auth)
5. Else (no token) → all curated apis enabled=false; hint:
     "Set token in ~/.cam/token.env (INFERENCE_HUB_TOKEN=...)"
6. Rebuild _aliases; write api-models.json once
```

If IHUB **is** reachable on first bootstrap → curated models are **enabled
immediately** (same as today's `init` + `check` combined). User goes straight to:

```bash
camc run -t claude --api glm-5.1 "task"    # works on first try
```

**Optional pre-create:** run `camc api list` once (triggers `ensure_ready()`). No `camc api init` command.

`ensure_ready()` is **idempotent**: never overwrites user-edited `apis` fields; only `enabled` / `_catalog` refresh via `camc api check`.

**Periodic refresh:** `camc api check` (or first `camc run --api` per day, optional 24h TTL) re-runs IHUB enable + `_catalog` update.

```text
1. TokenResolver → bearer (any source)
2. GET {base_url}/models (timeout 5s)
3. If unreachable → curated profiles stay enabled=false (or last known state);
   warn once: "inference-hub unreachable; curated models not enabled"
4. If reachable:
     for each curated profile:
       if api.model in response.data[].id:
         profile.enabled = true
         profile.enabled_reason = "ihub_catalog"
         merge profile into api-models.json if missing (never overwrite user edits)
       else:
         profile.enabled = false
         profile.enabled_reason = "id_not_on_key"
5. Rebuild _aliases (enabled **profiles** only — templates excluded)
6. Write api-models.json
```

**User edits protected:** if user changed a curated profile field (detected via
`user_modified: true` or hash mismatch from init), auto-enable only toggles
`enabled` / `enabled_reason` — does not reset URLs, aliases, or mode.

**Disabled curated models:** `--api nemotron-3-ultra` fails with
`profile disabled (id_not_on_key)` if that id is not on your key.

### Layer A usage (when preset is copied)

**Used when:**

- First API use triggers **`ensure_ready()`** (see Auto-bootstrap above)
- User runs `camc api init --reset` (explicit re-seed with backup)
- IHUB reachable on bootstrap/refresh → merge missing curated keys + update `enabled`

**Not used when:** resolving `--api` on every run — always read disk `api-models.json`
after bootstrap (including `enabled`, `priority`, URLs).

Preset updates ship with camc releases; they do **not** auto-merge into an
existing user file.

### Layer B — User runtime file (disk)

**Path:** `~/.cam/api-models.json`
**Owner:** user (or team via git/rsync to `~/.cam/api/`)

camc treats this as authoritative at runtime, with curated tier managed as above:

- **Curated profiles** — seeded from camc; `enabled` refreshed from IHUB catalog
- **User profiles** — `tier: "user"`, lower default priority; add freely
- Hand-edit in `$EDITOR` or via CLI (`camc api models add`, `alias add`)
- Safe to commit to a **private** dotfiles repo (no tokens in this file)

**camc will never silently overwrite** user-edited profile fields on upgrade or sync.
Only `enabled` / `enabled_reason` on curated entries may auto-update when IHUB is checked.

Validation on load:

- schema version
- unique aliases
- required fields per profile
- warn (not fail) on unknown fields for forward compat

### Layer C — Catalog refresh (via `camc api check`)

`camc api check` also writes/updates `_catalog.ids` + `_catalog.synced_at`.
Does **not** create new `apis.*` entries — user copies from `_templates` in JSON.

### Recommended workflows

**Solo user (default)**

1. Set token: `$EDITOR ~/.cam/token.env`
2. `camc api check` — enable curated models on your key
3. Run: `camc run -t claude --api glm-5.1 "…"`
4. New model: copy `_templates` → `apis` in JSON, or hand-add block

**Team / fleet**

- Maintain `api-models.json` in git
- Deploy with `cam sync` or rsync to `~/.cam/` on each machine
- Tokens stay per-host in `~/.cam/token.env` (not in git)
- Same profiles everywhere; keys local

**Power user**

- Ignore seed after init; build profiles entirely by hand
- Or ignore `_catalog` entirely if they know upstream ids

### What lives where (summary)

| Data | Location | Who maintains |
|------|----------|---------------|
| Curated APIs (**7**) | `apis.*` | CAM seed; **auto-enable** on IHUB check |
| Template stubs (**33**) | `_templates.*` | CAM seed; user **promote** → `apis` |
| User APIs | `apis.*` | User (`camc api add` or hand-edit) |
| `enabled` / `enabled_reason` | `apis.*` | **Auto** on `camc api check` |
| IHUB id list | `_catalog` | **Auto** on `camc api check` |
| API tokens | `~/.cam/token.env` | **User** |
| Auth env var names | `providers.*.env_names` | Seed + user (edit JSON) |

### File lifecycle

```text
first use (--api or camc api …):
  ensure_ready()
    → write ~/.cam/api-models.json (6 apis + 34 templates) if missing
    → if IHUB reachable → enable curated apis on your key

daily use:
  camc run --api glm-5.1     → read api-models.json

new API:
  edit ~/.cam/api-models.json (copy _templates → apis)
  camc api check                  # optional: refresh enabled
  camc run --api qwen36

broken file:
  fix JSON by hand; camc api list will error with line hint
  restore from backup / re-seed: delete file + camc api list
```

---

## 5.1 Schema — `api-models.json` (full example)

**Runtime source of truth:** `~/.cam/api-models.json`. Canonical shape is at the top
of this doc; below is a fuller example with curated seed + templates.

Optional `camc api sync` merges ids from `GET /v1/models` into `_catalog` only —
it does **not** replace user `apis`.

### File: `~/.cam/api-models.json`

```json
{
  "version": 1,
  "default": "glm-5.1",
  "default_provider": "inference-hub",
  "providers": {
    "inference-hub": {
      "display_name": "NVIDIA Inference Hub",
      "auth_key": "inference_hub",
      "env_names": ["INFERENCE_HUB_TOKEN", "INFERENCE_HUB_API_KEY", "INFERENCE_API_KEY"],
      "base_url": "https://inference-api.nvidia.com/v1",
      "endpoints": {
        "openai_chat_completions": "/chat/completions",
        "anthropic_messages": "/messages"
      }
    }
  },
  "apis": {
    "glm-5.1": {
      "provider": "inference-hub",
      "model": "nvidia/zai-org/eccn-glm-5.1",
      "enabled": true,
      "aliases": ["glm", "glm51"]
    },
    "deepseek-v4-pro": {
      "provider": "inference-hub",
      "model": "nvidia/deepseek-ai/eccn-deepseek-v4-pro",
      "aliases": ["deepseek", "ds-v4"]
    },
    "my-custom": {
      "provider": "inference-hub",
      "model": "nvidia/meta/eccn-llama-3.3-70b-instruct"
    }
  },
  "_templates": {
    "nvidia/zai-org/eccn-glm-5": {
      "provider": "inference-hub",
      "model": "nvidia/zai-org/eccn-glm-5",
      "kind": "chat",
      "suggested_api_key": "glm-5"
    },
    "nvidia/qwen/eccn-qwen-235b": { "provider": "inference-hub", "model": "nvidia/qwen/eccn-qwen-235b", "kind": "chat" },
    "nvidia/nvidia/eccn-llama-embed-nemotron-8b": {
      "provider": "inference-hub",
      "model": "nvidia/nvidia/eccn-llama-embed-nemotron-8b",
      "kind": "embedding",
      "suggested_api_key": "llama-embed-nemotron-8b"
    }
  },
  "_catalog": {
    "synced_at": "2026-06-16T03:00:00Z",
    "provider": "inference-hub",
    "ids": ["nvidia/zai-org/eccn-glm-5.1", "…"]
  },
  "_aliases": {
    "glm-5.1": "glm-5.1",
    "glm": "glm-5.1",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek": "deepseek-v4-pro",
    "kimi-k2.6": "kimi-k2.6",
    "nemotron-3-ultra": "nemotron-3-ultra",
    "nvidia/zai-org/eccn-glm-5.1": "glm-5.1"
  }
}
```

`_aliases` is **rebuilt automatically** on `camc api` write / `models add` /
`alias add` — users edit `apis.*.aliases`; camc maintains the flat `_aliases` index.

### API aliases

Each API supports `aliases[]` plus **implicit aliases** registered at load time:

| Alias source | Example | Maps to |
|--------------|---------|---------|
| API key (canonical) | `glm-5.1` | `glm-5.1` |
| `aliases[]` | `glm`, `glm51` | `glm-5.1` |
| `model` id | `nvidia/zai-org/eccn-glm-5.1` | `glm-5.1` |
| Last path segment of `model` | `eccn-glm-5.1` | `glm-5.1` |
| Normalised forms | `glm_5_1`, `GLM-5.1` | `glm-5.1` |

**Normalisation** (before lookup): lowercase; `.` ↔ `_` equivalent for matching
only (canonical key unchanged). No whitespace.

**Conflict rule:** two profiles must not share the same alias after
normalisation. `camc api alias add` and load-time validation fail with:

```text
alias "glm" already maps to glm-5.1; cannot assign to glm-5.2
```

**`default`** (top-level string) is the fallback when `--api` is omitted in future;
today `--api` is required for API-backed runs.

### Resolver lookup

```text
resolve_api(name):
  1. key = normalise(name)
  2. canonical = _aliases.get(key) or apis.get(name) and name
  3. if not found → error with "did you mean …" from prefix match
  4. return apis[canonical]
```

Agent record stores **canonical** API name (`glm-5.1`), not the alias used on the CLI.

### API entry fields (user-maintained)

| Field | Required | Purpose |
|-------|----------|---------|
| `model` | yes | Upstream model id |
| `provider` | yes* | Links to `providers.*` (*default: `default_provider`) |
| `url` | no | Full upstream URL override (any path — not assumed to be `/chat/...`) |
| `aliases` | no | Extra names for `--api` lookup |
| `enabled` | curated | Auto from IHUB check |

No `claude_config_dir` on API — `RunResolver` sets `CLAUDE_CONFIG_DIR` when `-t claude --api`.
To add a new gateway: add a `providers.*` block in JSON, then `camc api add` with matching `--url`.

### CLI (P0)

```bash
camc api list [--all]
camc api check
```

Debug only:

```bash
camc api proxy start ROUTE [--port N] [--upstream-url URL] [--upstream-model ID] [--api NAME] [--debug]
camc api proxy status
camc api proxy logs messages [--follow]
camc api proxy stop [route]
```

### API cheat sheet

```bash
camc run -t claude --api glm-5.1 "task"

camc api add qwen36 --model nvidia/qwen/eccn-qwen3.6-35b-a3b
camc run -t claude --api qwen36 "task"

camc env --tool claude --api glm-5.1
```

---

## 6. Auto-Proxy Lifecycle

### When `camc run --api glm-5.1 -t claude` runs

```text
1. RunResolver: tool=claude + api=glm-5.1 → mode=proxy, route=completions_to_messages
2. TokenResolver → bearer via provider.auth_key
3. ProxyManager.ensure(route, upstream_url, model) → local_base_url http://127.0.0.1:<port>
4. Merge env; unset ANTHROPIC_AUTH_TOKEN; set CLAUDE_CONFIG_DIR from api
5. Launch tmux with --no-inherit-env
```

### ProxyManager responsibilities

| Concern | Implementation |
|---------|----------------|
| **Idempotent ensure** | If port in `proxy-runs.json` + health OK → reuse |
| **Port allocation** | Default from route table; collision → next free port in 18324–18339 |
| **Process** | `camc _proxy <route> …` subprocess, `start_new_session=True` |
| **PID / state** | `~/.cam/api/proxy-runs.json` |
| **Logs** | `~/.cam/logs/proxy-<route>.log`, optional `proxy-<route>-llm.jsonl` |
| **Shutdown** | Automatic when last consumer exits (or process idle TTL); `cam heal` restarts dead proxies for running agents |
| **User CLI** | **none** — proxy is internal to `camc run` |
| **Debug CLI** | `camc api proxy start|status|logs|stop`; `camc run --proxy-debug` |

### `camc api proxy start` (debug only)

Start a protocol translator **without** launching an agent. Same subprocess as
`ProxyManager.ensure()` (`camc _proxy …`). Use when isolating proxy vs Claude issues.

#### Routes (from → to)

| `ROUTE` | Listens (frontend) | Forwards to (upstream) | Default port |
|---------|-------------------|------------------------|--------------|
| `completions_to_messages` | Claude `POST /v1/messages` | OpenAI `POST …/chat/completions` | 18324 |
| `completions_to_responses` | Codex `POST /v1/responses` | OpenAI `POST …/chat/completions` | 18325 |

Shorthand aliases: `messages` → `completions_to_messages`, `responses` → `completions_to_responses`.

#### Options

| Flag | Default | Purpose |
|------|---------|---------|
| `ROUTE` | *(required)* | Which translation (see table) |
| `--port PORT` | 18324 / 18325 | Local listen port |
| `--host HOST` | `127.0.0.1` | Bind address |
| `--upstream-url URL` | IHUB chat URL from `api-models.json` | Full upstream endpoint |
| `--upstream-model ID` | *(from `--api` or flag)* | Model id sent upstream (e.g. `nvidia/zai-org/eccn-glm-5.1`) |
| `--model-alias NAME` | same as upstream model | Client-facing model name (Claude `ANTHROPIC_MODEL`) |
| `--api NAME` | — | Resolve `--upstream-url` / model from `apis.NAME` (optional) |
| `--debug` | off | JSONL log at `~/.cam/logs/proxy-<route>-llm.jsonl` |

Token: `TokenResolver` (same chain as `camc run`); override with env or `--api-token` if added later.

#### Examples

```bash
# Claude → IHUB GLM (most common debug case)
camc api proxy start completions_to_messages \
  --upstream-url https://inference-api.nvidia.com/v1/chat/completions \
  --upstream-model nvidia/zai-org/eccn-glm-5.1 \
  --model-alias glm-5.1

# Shorthand route name + port
camc api proxy start messages --port 18324 --api glm-5.1

# Codex → IHUB chat (P1)
camc api proxy start completions_to_responses --port 18325 --api deepseek-v4-pro

# Then point Claude at the proxy manually:
export ANTHROPIC_BASE_URL=http://127.0.0.1:18324 ANTHROPIC_API_KEY=sk-camc-local
claude   # or camc run without --api for isolated test

camc api proxy status
camc api proxy logs messages -f
camc api proxy stop messages
```

### Flags

```bash
camc run -t claude --api glm-5.1 --no-api-proxy           # error if resolver needs proxy
camc run -t claude --api glm-5.1 --api-token "$TOKEN"      # one-off token
camc run -t claude --api glm-5.1 --proxy-debug            # JSONL LLM logs
```

### Login isolation (`CLAUDE_CONFIG_DIR` — run-time only, not on API)

When `-t claude` + `--api` (API-key path, not `/login`), `RunResolver` sets:

- `unset ANTHROPIC_AUTH_TOKEN` (so OAuth from default `~/.claude/` does not win)
- `CLAUDE_CONFIG_DIR=~/.cam/claude-<api-name>/` (derived from `--api`, not stored in JSON)

**Why:** Claude Code stores OAuth under `~/.claude/`. API-key agents need a separate config dir so GLM/IHUB does not pick up subscription login. This is a **Claude + `--api` run concern** — same reason `glm-5.1` should not carry `claude_config_dir` in `api-models.json`.

Codex/Cursor would get their own tool-specific isolation (`CODEX_HOME`, etc.)
when implemented — **not supported yet**; use normal login without `--api`.

---

## Storage Summary

```text
~/.cam/
  api-models.json              # ALL API config (apis, global, templates, catalog)
  token.env                    # secrets only (600)
  proxy-runs.json              # local proxy process state
  agents.json                  # per-agent api.name reference (no secrets)
  logs/proxy-*.log
  claude-<api-name>/           # isolated CLAUDE_CONFIG_DIR
```

**Rule:** If it's about which model/URL to call, it's in `api-models.json`. If it's a secret, it's in `token.env`.

---

## Implementation Phases

### Phase 0 — Py3.6 port (done)

- Moved `dev/ihub_proxy/` → `src/camc_pkg/proxy/` (+ `api_store`, `api_resolver`, …)
- Fix typing/syntax for 3.6
- Unit tests in `tests/proxy/`

### Phase 1 — Embed + `_proxy` subcommand (1 day)

- Extend `build_camc.py` MODULE_ORDER
- `camc _proxy …` hidden command
- `ProxyManager` with `proxy-runs.json`

### Phase 2 — Resolver + `--api` on `cam run` (2 days)

- Built-in `inference-hub` provider
- `ApiResolver` + merge into `cmd_run`
- Auto-proxy for Claude + GLM
- Agent record `api` metadata

### Phase 3 — Token + models CLI (1–2 days)

- `TokenResolver` (4-source chain)
- `camc api auth set|check|sources`
- Seed `api-models.json`; `camc api models add|list|sync`

### Phase 4 — Polish (ongoing)

- `cam heal` proxy restart
- Codex path (`completions_to_responses`) — **deferred**; design in *Supported today*
- Probe command for unknown models
- Deprecate manual `dev/ihub_proxy/` deploy and `start-ihub-proxy.sh` (thin wrapper only)

---

## Migration from Current Setup

| Today | After plan |
|-------|------------|
| `~/.cam/ihub-proxy/*.py` | embedded in `camc` |
| `~/.cam/start-ihub-proxy.sh` | automatic via `camc run --api` (`ProxyManager.ensure`) |
| `~/.cam/context.glm.json` + `camc-glm-run` | `camc run --api glm-5.1 -t claude …` |
| `~/.cam/inference-hub.env` | `~/.cam/token.env` (+ legacy fallback) |
| `~/.cam/inference-hub-api-models.json` (raw IHUB) | `api-models.json` `_catalog` (optional sync) |
| Manual env_setup scripts | profile URLs + resolver env |

Backward compat: `camc-glm-run` → `camc run --api glm-5.1 …`; read `inference-hub.env` if `token.env` absent.

---

## Open Questions

1. **Port range** — fixed 18324/18325 vs dynamic?
2. **Shared proxy** — one proxy per route per host (recommended) vs per-agent?
3. **Curated list** — locked at 7 `nvidia/*` models (see §5); user adds others at priority 50

---

## Success Criteria

- [ ] `python3.6 -m camc_pkg` tests pass on proxy/resolver modules
- [ ] Single `dist/camc` runs `_proxy` on Py3.6 host without extra files
- [ ] `camc run -t claude --api glm-5.1` auto-starts proxy and agent replies
- [ ] Token resolves from env, `token.env`, or `~/.my_tokens.yaml` (in that order after CLI)
- [ ] `camc run -t claude --api glm` resolves same as `--api glm-5.1`
- [ ] First `camc run --api glm-5.1` auto-creates `api-models.json` + enables curated if IHUB OK
- [ ] `camc api add --model …` then `camc run --api NAME` works
- [ ] No `--api-url` / `--api-model` on `camc run`
- [ ] `camc api init` is optional/idempotent; `--reset` for recovery only
- [ ] `camc api models promote …` copies template to `profiles` without removing template
- [ ] `/login` Claude agents unaffected (`camc run -t claude` without `--api`)
- [ ] Debug: `camc api proxy logs messages -f` shows JSONL per request

---

## Appendix A — Complete API Reference (options, CLI, storage)

Status: plan summary — **not implemented yet**.

### A.1 Architecture flow

```text
camc run [API flags] -t TOOL
  → ApiResolver (api-models.json lookup)
  → TokenResolver (4-source chain)
  → ProxyManager (if mode=proxy)
  → runtime_env merge
  → preflight → tmux
  → agents.json api metadata (no secrets)
```

### A.2 `camc run` / `camc env` flags

| Flag | Purpose |
|------|---------|
| `--api NAME` | Named API in `api-models.json` → `apis` (required for API-backed run) |
| `--api-token TOKEN` | One-off bearer (optional) |
| `--no-api-proxy` | Fail if resolver would use a proxy (force direct-only) |
| `--proxy-debug` | JSONL debug logs |

**Not on `camc run`:** `--api-url`, `--api-model`, **`--api-provider`**.

**Not on `camc api add`:** `--provider` (inferred from URL or `default_provider`).

Provider ids (`inference-hub`, `cc-switch`) appear only in JSON and in resolver
diagnostics (`camc api show`, `camc env --json`), never as a run-time CLI switch.

```bash
camc run -t claude --api glm-5.1 "task"
camc api add qwen36 --model nvidia/qwen/…
camc run -t claude --api qwen36 "task"
camc env --tool claude --api glm-5.1
```

### A.3 `camc api` CLI commands

#### User commands

| Command | Purpose |
|---------|---------|
| `camc api list` | Enabled APIs |
| `camc api list --all` | Include disabled curated |
| `camc api check` | Ping `default_provider`; refresh `enabled` + `_catalog` |

Edit `~/.cam/api-models.json` and `~/.cam/token.env` directly for all other changes.

Auto-bootstrap: first `camc run --api …` or `camc api list` creates seed config.

#### Debug only (`camc api proxy …`)

Not shown in default help. For troubleshooting when `camc run --api` fails or for JSONL inspection.

| Command | Purpose |
|---------|---------|
| `camc api proxy start ROUTE [opts]` | Manual start — see route table §6 |
| `camc api proxy status` | Running routes, ports, PIDs |
| `camc api proxy logs messages\|responses [--follow]` | Proxy / JSONL logs |
| `camc api proxy stop [route]` | Manual teardown |

**Routes:** `completions_to_messages` (Claude `/v1/messages` → chat/completions), `completions_to_responses` (Codex `/v1/responses` → chat/completions).

**Start options:** `--port`, `--host`, `--upstream-url`, `--upstream-model`, `--model-alias`, `--api NAME`, `--debug`.

Production path: `camc run --api` → `ProxyManager.ensure()` (no manual start).

#### Hidden (internal)

| Command | Purpose |
|---------|---------|
| `camc _proxy ROUTE --port N --upstream-model ID [--debug]` | Re-exec same camc binary |

### A.4 File system layout (`~/.cam/`)

```text
~/.cam/
  api-models.json           # providers + apis + _templates + _catalog + _aliases
  token.env                 # secrets (600) — primary
  inference-hub.env         # legacy fallback (600)
  proxy-runs.json           # active local proxy state
  logs/proxy-*.log
  claude-<api-name>/        # isolated CLAUDE_CONFIG_DIR
  agents.json               # includes api{} metadata per agent (no tokens)
```

**No SQLite.** One config file: `api-models.json` (includes internal `providers`).

### A.5 `api-models.json` sections

| Section | Purpose | Writable by user | Used at runtime |
|---------|---------|------------------|-----------------|
| `version` | Schema version | no | yes |
| `default` | Default API name | yes | yes |
| `default_provider` | Provider for new APIs | yes | yes |
| `providers.*` | Gateway plugins (URLs, auth) | yes | yes (resolver) |
| `apis.*` | Runnable APIs (6 curated + user) | yes | yes (`--api`) |
| `_templates.*` | 33 copy/paste stubs | yes | **no** (promote first) |
| `_catalog` | IHUB id list from sync | auto | reference only |
| `_aliases` | Flat alias index | auto-rebuilt | yes |

**Provider fields** (internal — not CLI flags)

| Field | Required | Notes |
|-------|----------|-------|
| `display_name` | no | Shown in `camc api list` verbose |
| `auth_key` | yes | TokenResolver logical key |
| `env_names` | yes | Env vars to try |
| `base_url` | yes | Root URL for this gateway |
| `endpoints` | yes | Protocol id → path (any path, not assumed `/chat/...`) |

**API entry fields**

| Field | Required | Notes |
|-------|----------|-------|
| `model` | yes | Upstream model id |
| `provider` | yes* | → `providers.*` (*default: `default_provider`) |
| `url` | no | Full upstream URL override (any path) |
| `aliases` | no | Extra `--api` names |
| `enabled` | curated | auto from IHUB check |

No `proxy`, no `claude_config_dir` on API — both are run-time (`RunResolver` + `-t`).

### A.6 Providers vs APIs (UX rule)

| Concept | User selects? | Example |
|---------|---------------|---------|
| **API** | yes (`--api glm-5.1`) | Named model endpoint |
| **Provider** | no (JSON only) | `inference-hub`, `cc-switch` |

Run: `camc run --api glm-5.1` — never `camc run --api-provider inference-hub`.

### A.7 `proxy-runs.json` schema

| Field | Purpose |
|-------|---------|
| `route` | e.g. `completions_to_messages` |
| `port` | Local port (default 18324/18325) |
| `pid` | Subprocess pid |
| `upstream_model` | Current backend id (if pinned) |
| `started_at` | ISO timestamp |
| `health` | last check result |

### A.8 Token storage (not in api-models.json)

| Priority | Source |
|----------|--------|
| 1 | `--api-token` (CLI) |
| 2 | Environment (`INFERENCE_HUB_TOKEN`, `OPENAI_API_KEY`, … per provider) |
| 3 | `~/.cam/token.env` (`KEY=value`, chmod 600) |
| 4 | `~/.my_tokens.yaml` (flat `key: value`) |
| legacy | `~/.cam/inference-hub.env` if `token.env` missing |

Logical keys: `inference_hub`, `cc_switch`, … (via provider `auth_key`).

### A.9 Proxy routes (embedded)

| Route | Frontend | Upstream | Default port |
|-------|----------|----------|--------------|
| `completions_to_messages` | anthropic_messages | openai_chat_completions | 18324 |
| `completions_to_responses` | openai_responses | openai_chat_completions | 18325 |

Auto-start when: `RunResolver` chooses `mode=proxy` for `tool + api` pair.

### A.10 Agent record `api` block (`agents.json`)

Stored (no secrets):

```json
"api": {
  "name": "glm-5.1",
  "tool": "claude",
  "provider": "inference-hub",
  "model": "nvidia/zai-org/eccn-glm-5.1",
  "mode": "proxy",
  "route": "completions_to_messages",
  "upstream_url": "https://inference-api.nvidia.com/v1/chat/completions",
  "local_base_url": "http://127.0.0.1:18324",
  "auth": { "source": "token.env", "key_name": "inference_hub" }
}
```

`mode` / `route` / `local_base_url` reflect **this run** (`-t` + `--api`), not fields on the API definition.

### A.11 Runtime env injected (Claude proxy example)

| Variable | Value |
|----------|-------|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:18324` |
| `ANTHROPIC_API_KEY` | local placeholder (`sk-camc-local`) |
| `ANTHROPIC_MODEL` | profile `client_model` |
| `CLAUDE_CONFIG_DIR` | `~/.cam/claude-<profile>/` |
| unset | `ANTHROPIC_AUTH_TOKEN` |

Real bearer used by proxy process only (from TokenResolver).

### A.12 Decision matrix

| Need | Use |
|------|-----|
| Daily default | `camc run -t claude --api glm-5.1` (proxy automatic) |
| List APIs | `camc api list` |
| Refresh IHUB enabled | `camc api check` |
| Add/change API | edit `~/.cam/api-models.json` |
| Set token | edit `~/.cam/token.env` |
| Debug proxy | `camc api proxy start messages --api glm-5.1` then `proxy logs` |
| Subscription Claude | `camc run -t claude` (no `--api`) |

---

## Related Docs

- `docs/camc-custom-api-profiles.md` — original resolver/storage design
- `docs/inference-hub.md` — IHUB credentials and manual proxy (legacy)
- `dev/ihub_proxy/README.md` — dev/reference prototype (not shipped)
