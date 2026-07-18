# CAMC Companion Skills Phase 1 Design

**Status:** approved for planning  
**Date:** 2026-07-15

## Scope

Phase 1 separates CAMC's built-in skill files from the generated single-file
runtime without removing the current standalone fallback. The production
command remains `~/.cam/camc`; a validated companion tree at
`~/.cam/camc_pkg/skills/` becomes the preferred skill provider.

This phase also fixes the two confirmed `build_camc.py` defects:

- `--verify` cannot import `camc_pkg` from the repository root and compares
  the package's `dev` version stamp with the generated artifact's intentional
  build stamp;
- embedded Markdown is injected before import stripping, so unindented code
  examples such as `import json` are removed from generated skill payloads.

It also adds an explicit runtime tool-directory override for `camc run` and
the matching read-only `camc env check` diagnostic.

## Goals

- Release `camc` and `camc_pkg/skills/` together as one required release unit.
- Prefer validated external skill files so the skill library can grow without
  increasing the primary runtime path.
- Keep `camc` fully functional when the companion directory is absent,
  incomplete, incompatible, or corrupt.
- Preserve every skill filename and byte exactly in both the companion tree
  and the embedded fallback.
- Make build and release verification fail before deployment when either
  artifact is inconsistent.
- Let callers select the directory containing the requested tool binary
  without changing adapter configuration or the runtime `PATH`.

## Non-goals

- Do not split the remaining CAMC Python modules in Phase 1.
- Do not remove `camc-misc` in this change.
- Do not change the project skill installation destination.
- Do not deploy to the live `~/.cam` installation without separate user
  authorization.
- Do not remove the embedded fallback until a later compatibility decision.
- Do not recursively search a tool directory or implicitly search a nested
  `bin/` directory.

## Release layout

Each target host receives:

```text
~/.cam/
|-- camc
`-- camc_pkg/
    `-- skills/
        |-- manifest.json
        |-- camc-cron-loop/
        |   `-- SKILL.md
        |-- camc-diagnose/
        |   |-- SKILL.md
        |   `-- reference/
        `-- ...
```

The build staging layout mirrors the target:

```text
dist/
|-- camc
`-- camc_pkg/
    `-- skills/
```

## Skill source and providers

`src/camc_pkg/skills/` remains the only authored source tree. Runtime skill
lookups use one provider interface and resolve providers in this order:

1. `CAMC_SKILLS_DIR`, when explicitly configured and valid;
2. `camc_pkg/skills/` beside the real path of the running `camc` executable;
3. the repository development tree when running from source;
4. the embedded single-file fallback.

External providers are all-or-nothing. CAMC validates the manifest before
using any external skill. A missing file, unexpected path, invalid digest,
unsupported schema, or incompatible CAMC version rejects the complete
external provider and selects the embedded fallback. Normal command output
does not change; rejection details appear only under verbose diagnostics.

`camc skills list`, manifest selection, `camc run`, scheduler launches, and
upgrade-time reinstall all consume the same resolved provider.

## Companion manifest

`build_camc.py` generates `manifest.json` deterministically. It contains:

```json
{
  "schema": 1,
  "camc_version": "1.2.0",
  "source_commit": "9909fb1",
  "skills": {
    "managing-camc": {
      "files": {
        "SKILL.md": "<sha256>",
        "reference/sessions.md": "<sha256>"
      }
    }
  }
}
```

Skill and file names are sorted. Paths must be relative, normalized, and
remain beneath the skill root. SHA-256 is computed over exact file bytes.
The manifest itself is not exposed as an installable skill file.

## Build behavior

`build_camc.py` performs these steps:

1. Read ordinary Python modules without injecting adapter or skill payloads.
2. Collect and strip actual Python imports and module docstrings.
3. Inject embedded configs and the embedded fallback only after source
   transformations, preserving payload content exactly.
4. Build `dist/camc`.
5. Stage an exact companion copy under `dist/camc_pkg/skills/` and generate
   its manifest.
6. Under `--verify`, compare CLI contracts, normalizing only the expected
   package-versus-build version stamp difference.
7. Load the generated fallback and compare every embedded skill path and byte
   with the authored source.
8. Validate the staged external provider and compare every staged path and
   byte with the authored source.

Package verification runs with the repository `src/` directory prepended to
`PYTHONPATH` while preserving any existing value.

## Mandatory release behavior

`scripts/release.sh` always releases `dist/camc` and
`dist/camc_pkg/skills/` together. There is no release flag that publishes a
new CAMC binary while intentionally omitting its companion skills.

For each target host, release ordering is:

1. Run `python3 build_camc.py --verify` unless the release explicitly reuses
   an already verified build.
2. Upload the complete companion tree to a temporary host-local path.
3. Validate its manifest and exact file hashes on the target.
4. Replace `~/.cam/camc_pkg/skills/` with the validated tree.
5. Upload `camc` through its existing temporary/atomic replacement path.
6. Verify the remote CAMC version and ask the remote binary to report that it
   selected the external skill provider.
7. Mark the host successful only when both runtime and companion verification
   pass.

Deploying skills first is backward-compatible: the previous CAMC ignores the
new directory. If the subsequent binary upload fails, the old CAMC remains
usable. If the companion directory is later removed or damaged, the new CAMC
uses its embedded fallback.

The shared-bin and release-archive paths also receive the matching companion
tree. A release archive is complete only when it contains both `camc` and
`camc_pkg/skills/`; rollback restores both from the same archived build.

`--dry-run` prints both artifact deployments. `--skip-build` may reuse the
current artifacts only after their manifest and fallback checks pass locally.
`--only` and normal fleet selection apply identically to both artifacts.

## Explicit tool directory

`camc run` adds `--tool-dir ABSOLUTE_DIR`. The existing `-t`/`--tool` option
continues to select the tool name; `--tool-dir` only controls where CAMC first
looks for that selected tool's executable. `camc env check` accepts the same
option so callers can inspect the result without starting an agent.

The supplied directory is expanded for `~`, normalized, and then required to
be absolute. CAMC searches only that directory. It does not recurse and does
not inspect an implicit `ABSOLUTE_DIR/bin/`. Candidate filenames are the
adapter readiness binary basename followed by the selected tool's existing
aliases, with duplicates removed. This preserves adapter-specific binary
names and current Cursor alias compatibility without choosing an arbitrary
executable from the directory.

When `--tool-dir` is present, executable resolution order is:

1. a matching executable directly inside `--tool-dir`;
2. the configured adapter executable;
3. the selected tool's golden paths;
4. the runtime `PATH` aliases.

`--tool-dir` also remains first when combined with `--use-env-tool`. If no
matching executable is found there, CAMC emits a warning and continues with
the fallback flow permitted by `--use-env-tool`: runtime `PATH` only, skipping
configured and golden paths. A missing directory, non-directory path, or
non-executable match is likewise non-fatal when a later fallback succeeds.
A relative `--tool-dir` is a CLI usage error rather than being interpreted
relative to the caller's current directory.

A successful override has `tool_resolution.source = "tool-dir"`. Readiness
probing and launch-command rewriting use the resolved absolute binary exactly
as they do for existing sources. JSON diagnostics and the per-agent runtime
manifest record the normalized requested directory, resolved binary, source,
and any fallback warnings. Omitting `--tool-dir` preserves the current
resolution behavior and output shape.

## Backward compatibility

- Existing calls to `~/.cam/camc` do not change.
- Existing `-t`/`--tool` and `--use-env-tool` behavior does not change when
  `--tool-dir` is omitted.
- Copying only `camc` to an old or constrained machine remains supported
  through the embedded fallback.
- Existing skills manifests remain valid; unavailable names continue to use
  the current non-blocking error behavior.
- Existing Desktop, Mobile, cron, scheduler, and remote command paths do not
  need a new executable location.
- External skills do not modify CAMC state or user manifests during provider
  discovery.

## Error handling

- Build payload mismatch: fail `build_camc.py --verify` with the first
  differing skill path and a concise reason.
- External provider invalid at runtime: use the embedded fallback; emit detail
  only in verbose mode.
- Companion upload or remote verification failure: fail that host's release
  and do not replace its CAMC binary.
- Binary upload failure after companion replacement: retain the old CAMC,
  which ignores or safely coexists with the new companion tree.
- Embedded fallback mismatch: fail the build; never release that artifact.
- Relative `--tool-dir`: fail argument validation with a concise absolute-path
  requirement.
- Unusable absolute `--tool-dir`: warn, retain the reason in tool-resolution
  diagnostics, and continue through the applicable existing fallback flow.

## Test strategy

Tests are written before implementation and cover:

- source skill code blocks containing top-level `import` lines survive the
  generated embedded fallback byte-for-byte;
- `build_camc.py --verify` can import the package and accepts only the expected
  version-stamp difference;
- the staged companion tree and manifest exactly describe the source tree;
- a valid adjacent companion provider is preferred;
- a missing companion provider uses the embedded fallback;
- missing files, digest mismatches, path traversal, schema mismatch, and
  version incompatibility use the embedded fallback;
- manifest selection and installation work identically through external and
  embedded providers;
- release dry-run includes both artifacts and release validation rejects a
  missing or mismatched companion tree.
- `--tool-dir` wins over configured, golden, and `PATH` executables for
  Claude, Codex, and both current Cursor aliases;
- `--tool-dir` still wins with `--use-env-tool`, while a missing override
  falls back to `PATH` only in that mode;
- relative, missing, non-directory, and non-executable tool-directory cases
  produce the specified validation or warning behavior;
- readiness probing, launch argv rewriting, JSON diagnostics, and the agent
  runtime manifest all use and report the same `tool-dir` resolution.

Final verification includes focused builder/provider tests, the existing CAMC
test suite, `python3 build_camc.py --verify`, Python syntax checks,
`cmp dist/camc src/camc` after synchronization, isolated-HOME external and
fallback skill installation checks, and `git diff --check`.

