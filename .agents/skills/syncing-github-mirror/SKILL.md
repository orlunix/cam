---
name: syncing-github-mirror
description: Mirror GitLab branches to the public GitHub repo (orlunix/cam) with hostname, service-account and code-name scrubbing. Use after pulling/merging from GitLab, when asked to sync or align with GitHub, or when the user says 脱敏.
---

# Syncing the public GitHub mirror

`github` (github.com/orlunix/cam) is a **privacy-scrubbed mirror** of the
internal GitLab repo (`origin`). Its history is a rewritten copy with **no
common ancestor**: never merge the two histories, never push a GitLab ref to
`github`, never merge `github/*` back into a local branch. Branches mirror by
name (`camui-desktop-v2`, `camui-mobile-v2`, ...).

The mirror is rebuilt from **tracked content only** on every sync; untracked
files never sync. Everything that reaches the GitHub tip must first pass the
scrub script, including text embedded in the tracked tar archives.

## Scrub mapping

Run `scripts/scrub.sh <dir>`; it applies all of these and then fails if any
deny-listed literal remains.

| Match | Replace |
|---|---|
| `hlren[.]duckdns[.]org` | `relay.example.com` |
| `<host>[.]nvidia[.]com` | `node.example.com` (whole host) |
| `svc[r]vgit` | `svcuser` |
| `PD[X]` / `pd[x]` (+ node digits) | `SITE-A` / `site-a` |
| `KI[X]` / `ki[x]` (+ node digits) | `SITE-B` / `site-b` |
| `DC[7]` / `dc[7]` | `SITE-C` / `site-c` |
| `DC[6]` / `dc[6]` | `SITE-D` / `site-d` |
| `PRG[N]` / `site-e`, `site-e[_]share` | `SITE-E` / `site-e`, `site_share` |
| `RN10[2]` (+ optional `N`/`S`) | `PROJ-A` |
| `FN10[0]` | `MOD-A` |
| `FN21[1]` | `MOD-B` |
| `XRI[S]CV` | `PROJ-B` |
| `NVB[u]g`, `nvbug[s]`, `bugtracker-cli` | `bugtracker` |
| `gsp[-]riscv[-]pilot[-]` | `workspace-` |
| `NR[1]0` | `TAG10` |
| standalone `S[C]` | `SITE-F` |

Notes:

- `hren@...` usernames stay; prose mentions of "duckdns" stay.
- Company-name prose (NVIDIA), RFC1918 test addresses, runtime redaction
  patterns (`glpat-...`, `ghp_...`, `sk-...`) and fake test tokens stay.
  They are not code names or secrets.
- The table writes tokens as `PD[X]`, `nvidia[.]com`, ... so this file does
  not match its own scrub and survives mirroring. Keep that convention.

## Code-name audit (do this when new content is added)

Before a sync that adds files or large docs, re-run the audit — the scrub
script only covers names it knows about:

```sh
# Candidates: letters + digits + optional trailing letters, text files only.
git grep -h -o -E '\b[A-Z]{2,6}[0-9]{1,4}[A-Z]?\b' -- \
  ':!web/vendor' ':!extensions/vendor' ':!dist' ':!*.bundle' \
  | sort | uniq -c | sort -rn | head -60
```

Review every candidate; technical tokens (`UTF8`, `SHA256`, `RFC6455`,
`VT100`, `WSL2`, `DC1`-`DC4`, commit hashes like `dc78e41`, Unicode ranges
`FF10`-`FF19`) are fine. Anything that looks like a site, node, cluster or
project code name gets a mapping in `scripts/scrub.sh` **and** a line in the
table above. Also grep case-insensitively for known families (`site-a`, `site-b`,
`site-c`, `site-e`, `proj-a`, `nvbug`) because lowercase forms leak as easily.

## Procedure

Assume the working branch is already updated and merged from GitLab:

```sh
git fetch origin --prune
git status --short            # must be clean
```

Export the local tracked tree onto the GitHub branch in a throwaway worktree:

```sh
BR=camui-desktop-v2
WT=$(mktemp -d /tmp/cam-gh-sync.XXXXXX)
git worktree add --detach "$WT" "github/$BR"
cd "$WT"
git ls-files -z | xargs -0 rm -f      # drop the old GitHub tree
git archive "$BR" | tar -x -C .       # export local tracked tree
```

Scrub (text files, code-name file/directory names, plus text members inside
`cam-server.bundle` and the cam-assist `.tar.gz`; the bundle's sha256/size
manifest is refreshed):

```sh
sh "$OLDPWD/.agents/skills/syncing-github-mirror/scripts/scrub.sh" .
```

Commit with the subject of the change being mirrored (one commit per sync
run), after checking that only scrub substitutions differ from the local
branch:

```sh
git add -A
git diff --cached --stat
git diff --cached "$BR" -- | grep '^[+-][^+-]' | head -40
git commit -m "<same subject as the GitLab commit(s)>"
```

Push. Linux git often has no GitHub credential; the Windows git in the same
checkout uses Credential Manager, so push the committed SHA from the main
repo root (Windows-accessible):

```sh
SHA=$(git -C "$WT" rev-parse HEAD)
cd "$OLDPWD"                            # main repo root
git push github "$SHA:refs/heads/$BR" 2>/dev/null \
  || git.exe push github "$SHA:refs/heads/$BR"
```

Clean up:

```sh
git worktree remove --force "$WT"
git worktree prune
```

## Verification

```sh
git fetch github --prune
git ls-remote github "refs/heads/$BR"
git diff "$BR" "github/$BR" --stat
```

The last diff must show **only** scrub substitutions. Any additional file
means a change was not mirrored or a new sensitive string slipped in — extend
`scripts/scrub.sh` and rerun before pushing.

## Failure modes

- `could not read Username for 'https://github.com'`: Linux git has no
  credential helper; use the `git.exe push` fallback above.
- Push rejected (non-fast-forward): someone pushed to the GitHub branch.
  Inspect `git log HEAD..github/$BR` and reconcile deliberately; do not
  force-push without a decision.
- `scrub: sensitive patterns remain`: the script found a deny-listed literal;
  add the missing replacement and rerun. Never bypass this gate.
- Tag sets deliberately differ (`camc-v1.2.*` live only on GitLab,
  `cam-desktop-*` only on GitHub). Do not mirror tags as part of this workflow.
