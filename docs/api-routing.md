# API routing — three translator modes

Date: 2026-06-16

`camc run -t claude --api NAME` resolves **`apis.NAME`** → **`providers.*`** →
**translator mode** → env + optional embedded proxy.

Implementation: `src/camc_pkg/api_routing.py` + `api_resolver.py`.

---

## Three examples at a glance

These are the three deployment patterns camc is designed to support. Pick one
per provider; configure it in `~/.cam/api-models.json`.

| # | Mode | Typical use | Claude connects to | camc starts proxy? |
|---|------|-------------|-------------------|-------------------|
| **1** | **embedded** | Corp OpenAI gateway, **NVIDIA IHUB** | `http://127.0.0.1:18324` | ✅ `completions_to_messages` |
| **2** | **direct** | Native Anthropic API, IHUB `/v1/messages` | Provider URL directly | ❌ |
| **3** | **external** | **CC Switch**, **LiteLLM** already running | `client_base_url` (e.g. `:15721`) | ❌ |

```text
Example 1 — embedded (OpenAI Chat Completions upstream):

  Claude Code
    → ANTHROPIC_BASE_URL=http://127.0.0.1:18324
    → camc embedded proxy (completions_to_messages)
    → https://gateway.example.com/v1/chat/completions
    → model on gateway / IHUB

Example 2 — direct (Anthropic Messages upstream):

  Claude Code
    → ANTHROPIC_BASE_URL=https://api.anthropic.com/v1/messages
    → real bearer token (from ~/.cam/token.env)
    → no local proxy

Example 3 — external (CC Switch / LiteLLM):

  Claude Code
    → ANTHROPIC_BASE_URL=http://127.0.0.1:15721
    → CC Switch or LiteLLM (you start it separately)
    → gateway / IHUB / other upstream
```

**Default today:** curated IHUB models use **Example 1** automatically — no manual
proxy, no env wiring.

---

## Config file

All routing is data-driven in **`~/.cam/api-models.json`** (no `--api-url` CLI flags).

Tokens stay in **`~/.cam/token.env`** (never in `api-models.json`).

### Provider fields

| Field | Purpose |
|-------|---------|
| `base_url` | Upstream host (embedded/direct) or metadata |
| `client_base_url` | Claude-facing URL for **external** mode |
| `endpoints` | Map protocol id → path suffix |
| `upstream_protocol` | Default upstream wire format |
| `translator` | `embedded` \| `direct` \| `external` |
| `external_translator` | Hint: same protocol but external hop |
| `catalog_path` | `/models` for `camc api check`; `""` = skip |
| `auth_key` / `env_names` | Token lookup via `~/.cam/token.env` |
| `client_api_key` | Placeholder key Claude sends (default `sk-camc-local`) |

### API fields

| Field | Purpose |
|-------|---------|
| `provider` | Provider id |
| `model` | Upstream model id |
| `url` | Full upstream URL override (embedded/direct upstream) |
| `client_url` | Full Claude-facing URL override (external) |
| `translator` | Per-API override |
| `allow_run` | `true` to opt in custom APIs (curated IHUB still default) |

Seed templates live in `api-models.json` → `_templates` (copy into `providers`).

---

## Example 1 — OpenAI Chat gateway (embedded)

**When to use:** Upstream speaks **OpenAI `/v1/chat/completions`**. Claude Code
needs Anthropic `/v1/messages`. camc runs the stdlib translator locally.

**Traffic:**

```text
Claude → :18324 (camc) → https://llm.internal.nvidia.com/v1/chat/completions
```

### `~/.cam/api-models.json`

```json
{
  "providers": {
    "corp-gateway": {
      "display_name": "Corp LLM Gateway",
      "auth_key": "corp_llm",
      "env_names": ["CORP_LLM_API_KEY", "OPENAI_API_KEY"],
      "base_url": "https://llm.internal.nvidia.com/v1",
      "upstream_protocol": "openai_chat_completions",
      "translator": "embedded",
      "catalog_path": "/models",
      "endpoints": {
        "openai_chat_completions": "/chat/completions"
      }
    }
  },
  "apis": {
    "corp-glm": {
      "provider": "corp-gateway",
      "model": "glm-4-flash",
      "allow_run": true,
      "enabled": true,
      "aliases": ["corp"]
    }
  }
}
```

### `~/.cam/token.env`

```bash
CORP_LLM_API_KEY=sk-your-gateway-key
chmod 600 ~/.cam/token.env
```

### Run

```bash
camc api check
camc run -t claude --api corp-glm -n my-agent --path /path/to/project "fix the bug"
```

### What camc does

- Resolves `translator: embedded` → starts `completions_to_messages` on `:18324`
- Passes **real token** to the proxy process
- Sets Claude env: `ANTHROPIC_BASE_URL=http://127.0.0.1:18324`, `ANTHROPIC_API_KEY=sk-camc-local`
- Does **not** touch your normal `~/.claude/` OAuth login (`CLAUDE_CONFIG_DIR=~/.cam/claude-api/`)

**IHUB curated models** (`glm-5.1`, `deepseek-v4-pro`, …) use this same pattern with
provider `inference-hub` — no `allow_run` needed.

---

## Example 2 — Native Anthropic API (direct)

**When to use:** Upstream already exposes **Anthropic `/v1/messages`**. No protocol
translation needed; Claude talks to the provider directly.

**Traffic:**

```text
Claude → https://api.anthropic.com/v1/messages  (real API key on Claude)
```

### `~/.cam/api-models.json`

```json
{
  "providers": {
    "anthropic": {
      "display_name": "Anthropic direct",
      "auth_key": "anthropic",
      "env_names": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
      "base_url": "https://api.anthropic.com",
      "upstream_protocol": "anthropic_messages",
      "translator": "direct",
      "catalog_path": "/v1/models",
      "endpoints": {
        "anthropic_messages": "/v1/messages"
      }
    }
  },
  "apis": {
    "sonnet-direct": {
      "provider": "anthropic",
      "model": "claude-sonnet-4-20250514",
      "allow_run": true,
      "enabled": true
    }
  }
}
```

### `~/.cam/token.env`

```bash
ANTHROPIC_API_KEY=sk-ant-your-key
chmod 600 ~/.cam/token.env
```

### Run

```bash
camc api check
camc run -t claude --api sonnet-direct -n sonnet-agent "your task"
```

### What camc does

- Resolves `translator: direct` → **does not** start local proxy
- Sets `ANTHROPIC_BASE_URL` to the provider messages URL
- Injects resolved token into `ANTHROPIC_API_KEY` on the agent
- Same isolated `~/.cam/claude-api/` config dir as other `--api` runs

Also applies when IHUB exposes native `/v1/messages` for a model — point
`base_url` at IHUB and set `upstream_protocol: anthropic_messages`.

---

## Example 3 — CC Switch / LiteLLM (external)

**When to use:** You already run **CC Switch** routing or a **LiteLLM** proxy.
It speaks Anthropic on the client side and handles translation upstream. camc
only points Claude at it.

**Traffic:**

```text
Claude → http://127.0.0.1:15721  (CC Switch)
         └→ CC Switch / LiteLLM → IHUB / gateway / etc.
```

### Prerequisites

Start the external translator **before** `camc run`:

```bash
# CC Switch: enable Claude routing in Settings → Advanced → Routing Service
# Default: http://127.0.0.1:15721

# Or LiteLLM:
# litellm --config dev/ihub_proxy/litellm-glm-proxy.yaml --host 127.0.0.1 --port 18322
```

### `~/.cam/api-models.json`

```json
{
  "providers": {
    "cc-switch": {
      "display_name": "CC Switch local routing",
      "auth_key": "cc_switch",
      "env_names": ["OPENAI_API_KEY", "CC_SWITCH_API_KEY"],
      "base_url": "http://127.0.0.1:15721",
      "client_base_url": "http://127.0.0.1:15721",
      "upstream_protocol": "anthropic_messages",
      "translator": "external",
      "catalog_path": "",
      "endpoints": {
        "anthropic_messages": ""
      }
    }
  },
  "apis": {
    "glm-via-cc": {
      "provider": "cc-switch",
      "model": "glm-5.1",
      "allow_run": true,
      "enabled": true,
      "aliases": ["cc-glm"]
    }
  }
}
```

For LiteLLM on port 18322, set `client_base_url` / `client_url` to
`http://127.0.0.1:18322` instead.

### Token

External translators often use their own auth. Token in `~/.cam/token.env` is
**optional** for this mode — CC Switch / LiteLLM holds the real upstream key.

If CC Switch expects a local placeholder:

```bash
# optional — only if CC Switch requires a client key
OPENAI_API_KEY=sk-cc-switch-local
```

### Run

```bash
camc run -t claude --api glm-via-cc -n cc-agent "your task"
```

### What camc does

- Resolves `translator: external` → **does not** start camc proxy
- Sets `ANTHROPIC_BASE_URL=http://127.0.0.1:15721` (from `client_base_url`)
- Sets `ANTHROPIC_API_KEY=sk-camc-local` (or `client_api_key` if configured)
- Translation quality / tool-call quirks depend on CC Switch or LiteLLM, not camc

---

## Side-by-side comparison

| | Example 1 embedded | Example 2 direct | Example 3 external |
|--|-------------------|------------------|-------------------|
| Upstream protocol | OpenAI Chat Completions | Anthropic Messages | (hidden behind external) |
| Who translates? | camc `:18324` | nobody | CC Switch / LiteLLM |
| Token to upstream | camc proxy process | Claude env (real key) | external process |
| `camc api check` | pings `/models` on gateway | pings provider catalog | skipped if `catalog_path: ""` |
| Headless / camc fleet | ✅ best fit | ✅ works | ⚠️ need external service up |
| GUI hot-switch models | ❌ edit JSON / `--api` | ❌ | ✅ CC Switch GUI |

---

## Product guard (today)

- **Tool:** `-t claude --api` only (codex/cursor unchanged).
- **APIs:** 6 curated IHUB models work out of the box (Example 1, no `allow_run`).
- **Custom:** set `"allow_run": true` on the API entry (Examples 1–3).

Curated IHUB models: `glm-5.1`, `deepseek-v4-pro`, `kimi-k2.6`, `minimax-m2.7`,
`qwen3-5-397b`, `nemotron-3-ultra`.

---

## Auto-selection rules

If `translator` is omitted on provider/API:

1. Tool protocol == upstream protocol → **direct** (or **external** if `external_translator: true`)
2. Known pair (Anthropic → Chat Completions) → **embedded**
3. Else → error (set `translator` explicitly)

---

## Related

- `docs/camc-api-proxy-plan.md` — full product plan
- `docs/camc-custom-api-profiles.md` — `--api` profile design
- `docs/inference-hub.md` — IHUB operator guide (+ CC Switch / LiteLLM alternatives)
- `docs/code-layout.md` — production vs dev trees
