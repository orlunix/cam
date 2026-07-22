# Embedded Skills and Heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make camc's bundled skills the sole source of truth and have `camc heal` synchronize them for every locally recorded agent workspace.

**Architecture:** `install_manifest_skills` becomes a compatibility-named installer that enumerates `_EMBEDDED_SKILLS` directly. No code reads, writes, or imports `~/.cam/skills.json`. `cmd_heal` performs its normal monitor repair, then uses each local agent record's context path and adapter `config_dir` to force-refresh the bundled skill tree.

**Tech Stack:** Python 3, pytest, existing `AgentStore`, adapter configuration, embedded build generator.

## Global Constraints

- Do not read, create, or mutate `~/.cam/skills.json`.
- All bundled skills install under `<workdir>/<config_dir>/skills`.
- `heal` continues when one agent has a missing/unusable workdir and reports it as a warning.
- Do not commit or push under this task.

---

### Task 1: Make embedded skills the sole install selection

**Files:**
- Modify: `src/camc_pkg/skills.py`
- Modify: `tests/test_skill_install_paths.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_builtin_skills_install_without_user_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_EMBEDDED_SKILLS", {"demo": {"SKILL.md": "demo"}})
    monkeypatch.setattr(skills, "SKILLS_MANIFEST", str(tmp_path / "skills.json"))
    assert skills.install_manifest_skills(str(tmp_path), config_dir=".codex") == {"demo": "created"}
    assert not (tmp_path / "skills.json").exists()
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src /tmp/camc-venv/bin/python -m pytest -q tests/test_skill_install_paths.py`

Expected: FAIL because the present implementation obtains no skill without the manifest.

- [ ] **Step 3: Implement minimum behavior**

Replace the manifest load/save selector with `embedded_skill_names()` returning `sorted(_EMBEDDED_SKILLS)`. Have `list_skills()` mark every bundled skill installed-by-default and have the installer enumerate that list.

- [ ] **Step 4: Run GREEN**

Run the Task 1 command; expected all tests pass.

### Task 2: Synchronize bundled skills from `camc heal`

**Files:**
- Modify: `src/camc_pkg/cli.py`
- Modify: `tests/test_skill_install_paths.py`

- [ ] **Step 1: Write the failing test**

```python
def test_heal_refreshes_embedded_skills_for_every_local_agent(monkeypatch, tmp_path):
    # Substitute AgentStore.list with two local agent records at two workdirs,
    # stub monitor repair, and assert installer calls use force=True plus each
    # tool's adapter config_dir.
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src /tmp/camc-venv/bin/python -m pytest -q tests/test_skill_install_paths.py -k heal`

Expected: FAIL because ordinary `cmd_heal` currently makes no install calls.

- [ ] **Step 3: Implement minimum behavior**

Add a helper called after `_do_heal()` that visits local agent records (regardless of status), skips inaccessible workdirs with a warning, and calls the bundled installer using `_load_config(tool).config_dir` and `force=True`.

- [ ] **Step 4: Run GREEN**

Run the Task 2 command; expected pass.

### Task 3: Rebuild and verify the released single-file bundle

**Files:**
- Modify when generated: `src/camc`, `dist/camc`, `dist/BUILD_LOG.md`

- [ ] **Step 1: Run focused skills and heal tests**

Run: `PYTHONPATH=src /tmp/camc-venv/bin/python -m pytest -q tests/test_builtin_skills_inventory.py tests/test_skill_install_paths.py`

- [ ] **Step 2: Build and verify bundle**

Run: `PYTHONPATH=src python3 build_camc.py && cmp -s src/camc dist/camc && ./dist/camc skills list`

- [ ] **Step 3: Check diff hygiene**

Run: `git diff --check`

