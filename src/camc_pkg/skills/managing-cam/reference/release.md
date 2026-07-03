# `cam release` — fleet deployment pipeline

`cam release` (alias: `bash scripts/release.sh`) builds dist/camc, runs
tests, and ships to every machine in `~/.cam/machines.json`.

## Full flow

```bash
cam release                          # full: build → test → deploy → tag
cam release --skip-tests             # skip pytest
cam release --skip-build             # reuse existing dist/camc
cam release --only pdx-098,dc7-26    # subset of machines
cam release --dry-run                # print plan, don't deploy
```

## Steps (per scripts/release.sh)

1. **Build** — `python3 build_camc.py` regenerates `dist/camc` (single-file)
2. **Test** — `pytest tests/test_camc_session_id.py -q`
3. **Per-machine loop**:
   - `mkdir -p ~/.cam` on remote
   - `scp dist/camc → remote:~/.cam/camc`
   - `chmod +x`
   - `ssh remote 'camc version'` to verify
   - If `/home/prgn_share/bin` writable: also install there (atomic temp+mv)
   - **Archive** — copy to `/home/prgn_share/tools/camc/releases/camc-vX.Y.Z-<short-hash>`
4. **Tag** — `git tag -a deploy-YYYYMMDDHHMMSS` with verified hosts in the message

## Archive store

Every successful deploy lands an artifact at:

```
/home/prgn_share/tools/camc/releases/camc-v1.2.0-abc1234
```

Versioned + commit-hashed filename. NFS-shared on PDX → all PDX hosts
see the same archive. DC has its own NFS, archive lives there too.

```bash
ls /home/prgn_share/tools/camc/releases/
# camc-v1.2.0-855dc5a
# camc-v1.2.0-d1ffa7c
# camc-v1.2.0-8755702
```

## Rollback

```bash
# 1. Pick a known-good archive
ls /home/prgn_share/tools/camc/releases/

# 2. Restore on each host
ssh pdx-098 "cp /home/prgn_share/tools/camc/releases/camc-v1.2.0-855dc5a ~/.cam/camc"
ssh pdx-098 "cp /home/prgn_share/tools/camc/releases/camc-v1.2.0-855dc5a /home/prgn_share/bin/camc"
```

Or rebuild from the matching git tag:

```bash
git tag --list 'deploy-*'                   # see all deploys
git show deploy-20260423234209              # who, when, what hosts
git checkout <tag>                          # source-level rollback
python3 build_camc.py                       # rebuild
cam release --skip-tests --skip-build       # ship
```

## Tag message format

```
camc deploy 20260423234209

version: camc v1.2.0 (d1ffa7c-dirty 2026-04-23 23:22)
verified: 4 / failed: 0
hosts:
   - pdx-container-xterm-098
   - pdx-container-xterm-110
   - dc7-container-xterm-26
   - dc2-container-xterm-037
```

Tags only fire when `verified > 0` and not `--dry-run`. Auto-skipped if
the tag name (timestamp) is already taken — re-runs in the same second
won't clobber.

## Machines file

`~/.cam/machines.json`:

```json
[
  {"name": "pdx-098", "type": "ssh",
   "host": "pdx-container-xterm-098.prd.it.nvidia.com",
   "user": "hren", "port": 3422},
  {"name": "local",   "type": "local"}
]
```

`cam release` skips entries with no `host` (local-only). Override the
machines file with `CAMC_MACHINES_FILE=/path/to/x.json`.

## SSH ControlMaster reuse

Each ssh/scp call piggy-backs on a multiplexed ControlMaster socket
(same one `SSHTransport` and `CamcDelegate` use):

```
/tmp/cam-ssh-<sha256(user@host:port)[:12]>
```

So 7-host releases over Kerberos-auth'd containers still complete
fast — only one auth per host for the whole pipeline.

## Failure recovery

- Per-host failures are **non-fatal** — the pipeline continues to the
  next host. Final summary lists `failed hosts`.
- A failure does NOT prevent the deploy tag (tag only requires `verified > 0`).
- Re-run with `--only <failed-host>` to retry one machine.
