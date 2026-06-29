# NVIDIA Inference Hub — Usage Guide

Date: 2026-06-15

NVIDIA Inference Hub exposes an **OpenAI-compatible HTTP API** for chat,
embeddings, and model discovery. Clients that speak the OpenAI SDK or
`/v1/chat/completions` can point at Inference Hub with a bearer token and a
model id from the catalog.

## Base URL

```text
https://inference-api.nvidia.com/v1
```

Common endpoints:

| Path | Method | Purpose |
|------|--------|---------|
| `/v1/models` | GET | List models available to your key |
| `/v1/chat/completions` | POST | Chat / completion (OpenAI-compatible) |
| `/v1/embeddings` | POST | Text embeddings (OpenAI-compatible) |

## Credentials

Store the API key outside git, with owner-only permissions:

```text
~/.cam/inference-hub.env   (chmod 600)
```

Load in a shell:

```bash
set -a
source ~/.cam/inference-hub.env
set +a
```

Environment variables:

| Variable | Purpose |
|----------|---------|
| `INFERENCE_HUB_API_KEY` | Canonical key for curl/scripts |
| `OPENAI_API_KEY` | Same key; many OpenAI-compatible clients read this |
| `INFERENCE_HUB_BASE_URL` | Base URL (`https://inference-api.nvidia.com/v1`) |

Do **not** put raw tokens in repo docs, agent records, or committed config.

## Quick test

```bash
source ~/.cam/inference-hub.env

curl -sS "$INFERENCE_HUB_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $INFERENCE_HUB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/zai-org/eccn-glm-5.1",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Hello! Can you help me?"}
    ],
    "temperature": 0.7,
    "max_tokens": 1024
  }'
```

Verified on 2026-06-15:

- HTTP `200`
- Model: `nvidia/zai-org/eccn-glm-5.1`
- Example reply: *"Hello! I would be happy to help you. What do you need assistance with today?"*
- Latency (from `nvext.timing`): TTFT ~2.3s, total ~8.2s

## List models

```bash
curl -sS "$INFERENCE_HUB_BASE_URL/models" \
  -H "Authorization: Bearer $INFERENCE_HUB_API_KEY" \
  | python3 -m json.tool
```

Each key has an allow-list. If a model is not permitted, the API returns an
auth/permission error rather than silently substituting another model.

Model ids use vendor namespaces, for example:

```text
nvidia/zai-org/eccn-glm-5.1
us/azure/zai-org/eccn-glm-5.1
us/aws/anthropic/bedrock-claude-opus-4-6
us/azure/openai/gpt-5.4
us/gcp/google/gemini-2.5-flash
```

Use the exact id returned by `/v1/models` for your key.

## Request format

### Chat completions

```json
{
  "model": "nvidia/zai-org/eccn-glm-5.1",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Summarize this in one sentence."}
  ],
  "temperature": 0.7,
  "max_tokens": 1024
}
```

Headers:

```http
Authorization: Bearer <INFERENCE_HUB_API_KEY>
Content-Type: application/json
```

### Streaming

Add `"stream": true` to the JSON body. The response is SSE (`data: {...}` lines),
same as OpenAI.

```bash
curl -N "$INFERENCE_HUB_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $INFERENCE_HUB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/zai-org/eccn-glm-5.1",
    "messages": [{"role": "user", "content": "Count to 5."}],
    "stream": true,
    "max_tokens": 256
  }'
```

## Response shape

Standard OpenAI chat completion fields:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "nvidia/zai-org/eccn-glm-5.1",
  "choices": [{
    "index": 0,
    "finish_reason": "stop",
    "message": {
      "role": "assistant",
      "content": "..."
    }
  }],
  "usage": {
    "prompt_tokens": 19,
    "completion_tokens": 211,
    "total_tokens": 230
  }
}
```

Some models also return **`reasoning_content`** inside the assistant message
(chain-of-thought style text separate from the final answer). Treat it as
model metadata, not user-facing output.

NVIDIA-specific timing metadata appears under **`nvext`**:

```json
"nvext": {
  "timing": {
    "ttft_ms": 2331.4,
    "total_time_ms": 8160.6,
    "prefill_time_ms": 2330.6,
    "kv_hit_rate": 0.0
  }
}
```

## Claude Code via GLM-5.1 proxy

Claude Code speaks **Anthropic `/v1/messages`**. GLM-5.1 on Inference Hub is
best reached through **`/v1/chat/completions`**. A local stdlib proxy translates
between the two protocols and rewrites textual `<tool_call>` / DSML / JSON tool markup into
proper Anthropic `tool_use` blocks.

```text
Claude Code
  -> ANTHROPIC_BASE_URL=http://127.0.0.1:18324   (completions_to_messages)
  -> https://inference-api.nvidia.com/v1/chat/completions
  -> model nvidia/zai-org/eccn-glm-5.1
```

Codex uses **OpenAI `/v1/responses`**. Production route **`completions_to_responses`** translates that wire to IHUB chat completions:

```text
Codex CLI  (camc run -t codex --api NAME)
  -> OPENAI_BASE_URL=http://127.0.0.1:18325/v1   (completions_to_responses)
  -> https://inference-api.nvidia.com/v1/chat/completions
  -> model nvidia/zai-org/eccn-glm-5.1
```

> **Production `src/camc_pkg/proxy/`** ships **`completions_to_messages`** (`:18324`, Claude)
> and **`completions_to_responses`** (`:18325`, Codex + IHUB). Dev copies remain under
> `dev/ihub_proxy/` for manual experiments only.

### Protocol routes

| Route | Frontend | Upstream | Status |
|-------|----------|----------|--------|
| `completions_to_messages` | Anthropic `/v1/messages` | IHUB `/v1/chat/completions` | **Production** (`src/camc_pkg/proxy/messages.py`) |
| `completions_to_responses` | OpenAI `/v1/responses` | IHUB `/v1/chat/completions` | **Production** (`src/camc_pkg/proxy/responses.py`, Codex `--api`) |

Source: **`src/camc_pkg/proxy/`** (embedded in `dist/camc`). Optional standalone
copy for manual debug: `dev/ihub_proxy/` → `~/.cam/ihub-proxy/` (not auto-synced;
see `docs/code-layout.md`).

### Files

| File | Purpose |
|------|---------|
| `~/.cam/inference-hub.env` | API key + proxy URLs (chmod 600) |
| `~/.cam/ihub-proxy/completions_to_messages.py` | Anthropic messages → chat completions |
| `~/.cam/ihub-proxy/completions_to_responses.py` | OpenAI responses → chat completions |
| `~/.cam/start-ihub-proxy.sh` | Start/stop both routes |
| `~/.cam/context.json` | `env_setup` for camc Claude agents |

Legacy LiteLLM stack (`~/.cam/start-glm-proxy.sh`, ports 18322–18323) is
deprecated; use `start-ihub-proxy.sh` instead.

### Start proxy

```bash
~/.cam/start-ihub-proxy.sh start
~/.cam/start-ihub-proxy.sh status
```

Debug mode (JSONL LLM request logs — model, tokens, latency, previews; no full prompts):

```bash
~/.cam/start-ihub-proxy.sh restart --debug
tail -f ~/.cam/logs/ihub-messages-llm.jsonl
```

`ensure` remembers debug mode after `start --debug` via `~/.cam/logs/ihub-proxy.debug`.
Or set `IHUB_PROXY_DEBUG=1` in the environment.

No extra pip dependencies — stdlib Python only.

### Keep `/login` Claude separate from GLM

Three layers should not mix:

| Layer | Official Claude (`/login`) | GLM via IHUB |
|-------|------------------------------|--------------|
| **Launch** | `camc run -t claude ...` | `~/.cam/camc-glm-run ...` |
| **Auth** | OAuth in `~/.claude/` | Local proxy + `ANTHROPIC_API_KEY=sk-ihub-local` |
| **Env profile** | `~/.cam/context.json` (`env_setup: null`) | `~/.cam/context.glm.json` (swapped in by wrapper) |

`camc-glm-run` temporarily replaces `context.json` with `context.glm.json`, runs
with `--no-inherit-env`, then restores your default context. **No logout**
required — GLM agents unset `ANTHROPIC_AUTH_TOKEN` and never touch your login
session.

Do **not** export `ANTHROPIC_*` in `~/.bashrc` / `~/.zshrc`; that would leak
proxy settings into every agent.

### Login session vs ENV boot

camc builds agent env in two steps:

1. **Preflight capture** — login shell + `env_setup` (finds `claude` on PATH)
2. **Tmux launch** — with `--no-inherit-env`, runs `env_setup && claude` in a
   **non-login** bash (`bash -c`, not `bash -l -c`) so your `/login` shell rc
   does not re-inject `ANTHROPIC_*` before Claude starts

| Profile | `env_setup` | Claude config dir |
|---------|-------------|-------------------|
| Login (Sonnet/Opus) | `~/.cam/env_setup/login.sh` — **unsets** all `ANTHROPIC_*` | default `~/.claude/` (OAuth) |
| GLM (IHUB) | `~/.cam/env_setup/glm.sh` — proxy vars + `CLAUDE_CONFIG_DIR` | `~/.cam/claude-glm/` (no OAuth) |

`CLAUDE_CONFIG_DIR` is the official Claude Code knob for side-by-side accounts.
GLM agents never read your logged-in OAuth session from `~/.claude/`.

Example template: `dev/ihub_proxy/context.glm.json.example`

### camc env_setup (GLM profile only — `context.glm.json`)

```bash
source ~/.cam/inference-hub.env
~/.cam/start-ihub-proxy.sh ensure
export ANTHROPIC_BASE_URL="$IHUB_MESSAGES_PROXY_URL"
unset ANTHROPIC_AUTH_TOKEN
export ANTHROPIC_API_KEY="$GLM_PROXY_KEY"
export ANTHROPIC_MODEL=glm-5.1
export ANTHROPIC_FAST_MODEL=glm-5.1
```

Use **`ANTHROPIC_API_KEY`** (local placeholder) instead of **`ANTHROPIC_AUTH_TOKEN`**
so Claude Code does not fight your existing `/login` session — **no logout
required**. The real Inference Hub key stays in `inference-hub.env` and is
used by the proxy process itself, not forwarded from Claude's env.

### Launch Claude agent on GLM

Use **`--no-inherit-env`** so `env_setup` runs inside the tmux launch
wrapper (otherwise ANTHROPIC vars may not reach Claude).

```bash
~/.cam/start-ihub-proxy.sh ensure

~/.cam/camc run --no-inherit-env -t claude -n glm-proxy-test --tag glm-proxy \
  --path /home/hren/.openclaw/workspace/cam \
  "Your task prompt here"
```

Or use the helper:

```bash
~/.cam/camc-glm-run -n my-agent --tag glm-proxy --path /path/to/project "prompt"
```

If Claude still warns about auth, confirm `ANTHROPIC_AUTH_TOKEN` is unset in
the agent session (`unset ANTHROPIC_AUTH_TOKEN`). You do **not** need
`claude /logout` when using `ANTHROPIC_API_KEY` + local proxy.

### Notes

- GLM-5.1 can be slow on cold start. Increase camc startup wait or send
  prompts manually with `camc send <id> "..."`.
- The proxy drops Claude-only params (`output_config`, `context_management`)
  before calling chat/completions. See `docs/api-model-metadata.md` for how
  model context limits are synced and exposed to agents.
- Anthropic-family models on IHUB can also be used **without** this proxy by
  pointing `ANTHROPIC_BASE_URL=https://inference-api.nvidia.com` directly at
  native `/v1/messages`. GLM-5.1 needs the local proxy for tool calling.

## Alternative: CC Switch and LiteLLM

> **Full walkthrough with config JSON:** `docs/api-routing.md` — Examples 1 (embedded),
> 2 (direct), 3 (external).

The stdlib routes above are the CAM default (fast, no pip deps). Two common
community alternatives solve the same protocol mismatch with different tradeoffs.

### CC Switch (GUI provider manager + local routing)

[CC Switch](https://github.com/farion1231/cc-switch) is a cross-platform desktop
app for switching Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, and
Hermes between 50+ provider presets (including **NVIDIA**). It can rewrite CLI
config files and run a **local routing service** that performs the same kind of
format conversion as our proxies:

| CC Switch concept | CAM equivalent |
|-------------------|----------------|
| Claude routing → `127.0.0.1:15721` | `completions_to_messages` → `:18324` |
| Codex routing + `apiFormat = openai_chat` | `completions_to_responses` → `:18325` |
| Provider hot-switch without CLI restart | camc `env_setup` + proxy ensure |

CC Switch documents the Codex **Responses → Chat Completions → Responses**
loop explicitly (needed when upstream only exposes chat/completions, e.g.
Inference Hub GLM):

- [App routing](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.2-routing.md)
- [Codex + Chat-format provider guide](https://github.com/farion1231/cc-switch/blob/main/docs/guides/codex-deepseek-routing-guide-en.md)

Typical CC Switch flow for Inference Hub GLM:

1. Add provider preset **Nvidia** (or custom) with Inference Hub base URL and key.
2. Start **Settings → Advanced → Routing Service**.
3. Enable **Claude routing** and/or **Codex routing** (Codex needs routing when
   upstream has no native `/v1/responses`).
4. CC Switch points `ANTHROPIC_BASE_URL` or Codex `base_url` at the local route
   (`http://127.0.0.1:15721` by default).

Official site: [ccswitch.io](https://ccswitch.io)

### LiteLLM (self-hosted protocol proxy)

[LiteLLM](https://docs.litellm.ai/docs/proxy/quick_start) exposes Anthropic
`/v1/messages` on a local port and forwards to OpenAI-compatible upstreams.
This was the first CAM integration (`~/.cam/start-glm-proxy.sh`, ports
18322–18323) and remains available as a reference config:

```bash
pip install 'litellm[proxy]'
source ~/.cam/inference-hub.env
litellm --config dev/ihub_proxy/litellm-glm-proxy.yaml \
  --host 127.0.0.1 --port 18322
```

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:18322
export ANTHROPIC_AUTH_TOKEN=sk-litellm-local
export ANTHROPIC_MODEL=glm-5.1
```

LiteLLM is useful when you need proxy callbacks (e.g.
[`async_pre_call_hook`](https://docs.litellm.ai/docs/proxy/call_hooks) to strip
Claude-only fields like `output_config` / `thinking` conflicts — see
[anthropics/claude-code#65863](https://github.com/anthropics/claude-code/issues/65863)).
For GLM on Inference Hub, the stdlib route is usually faster (~8s vs minutes).

### CC Switch + LiteLLM together

A common pattern in the wild (not required for CAM):

```text
Claude Code / Codex
  -> CC Switch routing (profile switch, logging, failover)
  -> LiteLLM or CAM ihub_proxy (protocol translation)
  -> Inference Hub /v1/chat/completions
```

CC Switch manages **which** provider and **where** CLI tools point; LiteLLM or
`completions_to_*` handles **wire-format** translation. CAM agents use
`~/.cam/context.json` `env_setup` instead of CC Switch when running headless
via camc.

### Comparison

| Approach | Pros | Cons |
|----------|------|------|
| CAM `completions_to_*` | Stdlib, fast, explicit routes | Manual env / camc setup |
| CC Switch routing | GUI, hot-switch, failover, logs | Desktop app; default port 15721 |
| LiteLLM | Ecosystem, hooks, aliases | Extra dep; slower for GLM in testing |


### curl / shell scripts

Use `INFERENCE_HUB_API_KEY` and `INFERENCE_HUB_BASE_URL` from
`~/.cam/inference-hub.env`.

### OpenAI Python SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["INFERENCE_HUB_API_KEY"],
    base_url=os.environ["INFERENCE_HUB_BASE_URL"],
)

resp = client.chat.completions.create(
    model="nvidia/zai-org/eccn-glm-5.1",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

### Hermes Agent (OpenAI-compatible custom provider)

```yaml
# config.yaml
model:
  provider: "custom"
  default: "<model-id-from-/v1/models>"
  base_url: "https://inference-api.nvidia.com/v1"
  api_mode: "chat_completions"
```

For Hermes runtime auth, set **`OPENAI_API_KEY`** in the env file (not only
`CUSTOM_API_KEY`). Hermes checks `OPENAI_API_KEY` at inference time for
`provider: custom`.

### CAM / camc

See `docs/camc-custom-api-profiles.md` and `docs/camc-api-proxy-plan.md`. Use `camc run -t claude|codex --api NAME` or opt-in `camc api default set`. Tokens live in `~/.cam/token.env`, not inline.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `401` / auth error | Missing or wrong bearer token | Check `~/.cam/inference-hub.env`, reload shell |
| `401` model not allowed | Key allow-list excludes model | Run `/v1/models`, pick an allowed id |
| Client says "no key" | Wrong env var name | Use `OPENAI_API_KEY` for OpenAI-compatible clients |
| Slow first token | Cold prefill / large model | Normal; check `nvext.timing.ttft_ms` |
| Extra `reasoning_content` field | Model exposes reasoning trace | Read `message.content` for the final answer |

## Security

- Keep `~/.cam/inference-hub.env` at **`chmod 600`**
- Rotate the key if it was pasted into chat, logs, or tickets
- Never commit tokens to git or store them in `agents.json`

## Related docs

- `docs/camc-custom-api-profiles.md` — planned camc API provider integration
- **`docs/debug-notes/api-bench-ihub-proxy.md`** — debug log for `--api` benchmark (401, Qwen system order, DSML, etc.)
- [CC Switch](https://github.com/farion1231/cc-switch) — GUI provider switching + local routing
- [LiteLLM proxy](https://docs.litellm.ai/docs/proxy/quick_start) — self-hosted protocol translation
- Hermes install notes: `hermes-research/reports/install-log.md`
