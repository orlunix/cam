# MAS pre-submission review — rejection risks & demo plan

Date: 2026-07-18. Scope: everything that must be true before uploading the
mas `.pkg` to App Store Connect, ordered by rejection probability.
Companion: `MAS-SPIKE.md` (sandbox smoke checklist).

## High risk (likely rejection if unaddressed)

### 1. No demo path — reviewer cannot use the app

The app manages agents on the user's own SSH hosts. A reviewer with no
host sees "add an SSH node" and nothing works — the classic tool-app
rejection ("app could not be used").

**Decision: built-in demo mode (Option A).** A `demoTransport`
implementing the hub's existing `sshTransport` interface
(`execRemote / writeRemoteFile / openTerminalChannel`) with simulated,
local-only responses — no network, no credentials, no agent CLI:

- `camc --json list` → 3 fake agents (running / completed / idle)
- `camc run` → creates a fake agent record
- `camc capture <id>` → canned terminal output
- `openTerminalChannel` → a scripted fake stream (a local child process
  printing planning → editing → testing → done on a timer; plain text,
  no sandbox concerns)

Reviewer flow: Settings → "Demo (built-in)" → agents appear → open a
terminal that "runs" → start a demo agent → it completes → switch tabs.
Every core feature is demonstrable offline. Also useful for our own
screenshots and demos.

Rejected alternative: a real hosted SSH node + credentials in review
notes — leaks credentials, leaks paid agent-CLI accounts, and invites
abuse (mining on our demo box).

### 2. Missing store metadata

- Privacy policy URL and support URL (GitHub Pages is fine)
- App description + keywords, 1280×800 screenshots (demo mode provides them)
- Privacy labels: "no data collected; SSH credentials stored only in the
  device's Keychain"
- Export compliance: standard SSH/TLS encryption → exemption

## Medium risk (may be questioned / returned)

### 3. `net:probe` — arbitrary URL fetch from the renderer

`main.cjs` (`ipcMain.handle('net:probe')`) lets the renderer make the
main process GET any http(s) URL. Reviewers may read this as an open
network surface.
**Decision (2026-07-18): keep, no constraint** — the probe returns only
`{status, bytes, ms}` (no body content), so nothing can be exfiltrated;
it exists for Relay connectivity checks against user-configured (often
public) relay URLs, and restricting it to private ranges would break
the Relay feature itself. Explain in review notes: "status-only
reachability probe for the user's own relay; returns no content."

### 4. Clipboard attachment reads

`files:readClipboardAttachments` reads file paths found on the
clipboard and returns their contents. User-gesture driven (explicit
paste), but reviewers may ask why the app reads files via the clipboard.
**Fix**: document in review notes ("only on explicit paste action"),
or add a confirmation step.

### 5. Legacy local-write branches (dead but present)

`embedded-hub.cjs` still carries two `isLocal` filesystem-write branches
(workspace write ~:2085, system-prompt write ~:2315). Local sessions are
retired so these are unreachable, but a sandbox denial in logs would be
a question.
**Fix**: make both branches refuse like the rest of the local surface
(small change, removes the entire local-write surface).
**DONE (2026-07-24)**: all three local-write branches (workspace write,
system-prompt write, and image upload in `_uploadAgentFile`) now refuse
with `local_unsupported`; no local filesystem write surface remains.

## Low risk (standard hygiene, already OK)

- No auto-update mechanism ✓ (MAS hard requirement)
- No private APIs, no local child binaries (ssh2 is pure JS) ✓
- Keychain access group `ULL2CR6L6J.com.hren.cam.mas` (TeamID.BundleID) ✓
- Unfinished surfaces (Bots / Todos / Workflow) hidden —
  click through every visible button on the Mac build before submitting ✓
- camc/skillm uploads run remotely on the user's own hosts; state in
  review notes that no network-downloaded code executes locally ✓

## User-side (non-code)

1. App Store Connect app record for `com.hren.cam.mas` + MAS provisioning
   profile → `MAS_PROVISIONING_PROFILE` secret.
2. "3rd Party Mac Developer Installer" certificate (pkg signing).
3. Privacy policy + support pages (GitHub Pages).

## Suggested order

1. Code fixes now (items 3, 5 — small).
2. Demo mode (item 1 — the big one, ~1 day).
3. Metadata + secrets (user).
4. Sandbox smoke per `MAS-SPIKE.md`, then TestFlight, then review.
