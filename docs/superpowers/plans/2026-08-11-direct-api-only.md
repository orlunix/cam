# Direct API Profiles Implementation Plan

**Goal:** Keep one provider base URL and run API profiles through each tool's
native endpoint without CAMC proxy conversion or a CAMC-owned local API server.

**Architecture:** `api-models.json` stores provider authentication, one
canonical `base_url`, model IDs, and endpoint capabilities. Adapter TOML owns
the native wire endpoint and `base_url_suffix`; the resolver derives the
client URL and rejects profiles without the required native endpoint.

**Constraints:** No compatibility fields or proxy fallback; tokens remain
outside model JSON; no commits or pushes in this task.

### Task 1: Clean profile schema

- [x] Add RED tests for one `base_url`, no `default_provider`, no client/API
  URL fields, and provider token-file lookup using `auth_key` by default.
- [x] Remove legacy fields from the seed/merge path and use each API entry's
  explicit `provider`.
- [x] Keep ordered `defaults.<tool>` arrays; first entry is primary.

### Task 2: Native-only routing

- [x] Add RED tests proving Claude uses Messages, Codex uses Responses, and a
  chat-only profile fails before launch.
- [x] Make routing direct-only and derive URLs from the single base plus the
  adapter suffix.

### Task 3: Remove proxy runtime

- [x] Add RED CLI assertions for the absence of proxy flags/commands.
- [x] Remove proxy startup, hidden worker, bundled proxy modules, and proxy
  state/tests; retain only direct API commands and JSON metadata.

### Task 4: Verify bundle

- [x] Run focused API/adapter tests, build `dist/camc`, smoke-test help/version,
  and run `git diff --check`.
