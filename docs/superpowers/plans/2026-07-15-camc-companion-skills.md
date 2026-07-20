# CAMC Companion Skills Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Ship a validated camc_pkg/skills companion tree with every CAMC release while retaining the byte-identical embedded single-file fallback.

**Architecture:** A focused skill_provider module owns deterministic manifests, hash validation, and external-tree loading. skills.py resolves an explicit override, the tree adjacent to the real executable, the development source tree, or the embedded fallback; build_camc.py stages and verifies both artifacts, and release.sh deploys them as one unit with skills installed before the binary.

**Tech Stack:** Python 3.6-compatible standard library for the standalone CAMC path, pytest, Bash, SSH/SCP.

## Global Constraints

- src/camc_pkg/skills/ remains the only authored skill source.
- dist/camc and dist/camc_pkg/skills/manifest.json plus skill directories are the build release unit.
- External manifests use schema 1, exact camc_version compatibility, normalized relative paths, and SHA-256 over exact bytes.
- Invalid external providers are rejected as a whole; the embedded single-file store remains usable.
- Existing camc invocation and skill-manifest behavior remain backward-compatible.
- camc-misc remains present in this change.
- release.sh never intentionally publishes a new binary without its matching companion skills.
- Do not deploy to ~/.cam, push, tag, or commit unless the user separately authorizes it.

---

### Task 1: Deterministic companion manifest and validator

**Files:**
- Create: src/camc_pkg/skill_provider.py
- Create: tests/test_camc_skill_provider.py
- Modify: build_camc.py: MODULE_ORDER and imports

**Interfaces:**
- Consumes: authored skill directory paths and the current CAMC version string.
- Produces: scan_skill_tree(root) -> dict[str, dict[str, bytes]], build_skill_manifest(root, camc_version, source_commit) -> dict, write_skill_manifest(root, camc_version, source_commit) -> str, and validate_skill_provider(root, camc_version) -> (store, info).

- [ ] **Step 1: Write manifest-generation tests**

~~~python
def test_build_manifest_is_sorted_and_hashes_exact_bytes(tmp_path):
    root = tmp_path / "skills"
    (root / "z-skill").mkdir(parents=True)
    (root / "z-skill" / "SKILL.md").write_bytes(b"z\n")
    (root / "a-skill" / "reference").mkdir(parents=True)
    payload = b"import json, sys\n"
    (root / "a-skill" / "reference" / "usage.md").write_bytes(payload)

    manifest = provider.build_skill_manifest(
        str(root), "1.2.0", "abcdef0")

    assert list(manifest["skills"]) == ["a-skill", "z-skill"]
    assert manifest["schema"] == 1
    assert manifest["camc_version"] == "1.2.0"
    assert manifest["source_commit"] == "abcdef0"
    assert manifest["skills"]["a-skill"]["files"]["reference/usage.md"] == (
        hashlib.sha256(payload).hexdigest()
    )
~~~

- [ ] **Step 2: Run the manifest test and verify RED**

Run: python3 -m pytest tests/test_camc_skill_provider.py::test_build_manifest_is_sorted_and_hashes_exact_bytes -q

Expected: FAIL because camc_pkg.skill_provider does not exist.

- [ ] **Step 3: Implement deterministic scanning and manifest writing**

~~~python
SKILL_MANIFEST_SCHEMA = 1

def scan_skill_tree(root):
    store = {}
    for skill_name in sorted(os.listdir(root)):
        skill_dir = os.path.join(root, skill_name)
        if not os.path.isdir(skill_dir) or os.path.islink(skill_dir):
            continue
        files = {}
        for current, dirs, names in os.walk(skill_dir):
            dirs[:] = sorted(d for d in dirs
                              if not os.path.islink(os.path.join(current, d)))
            for name in sorted(names):
                path = os.path.join(current, name)
                if os.path.islink(path):
                    raise ValueError("skill provider contains symlink: %s" % path)
                rel = os.path.relpath(path, skill_dir).replace(os.sep, "/")
                with open(path, "rb") as handle:
                    files[rel] = handle.read()
        if files:
            store[skill_name] = files
    return store

def build_skill_manifest(root, camc_version, source_commit):
    store = scan_skill_tree(root)
    skills = {}
    for skill_name in sorted(store):
        files = {}
        for rel in sorted(store[skill_name]):
            files[rel] = hashlib.sha256(store[skill_name][rel]).hexdigest()
        skills[skill_name] = {"files": files}
    return {
        "schema": SKILL_MANIFEST_SCHEMA,
        "camc_version": camc_version,
        "source_commit": source_commit,
        "skills": skills,
    }

def write_skill_manifest(root, camc_version, source_commit):
    path = os.path.join(root, "manifest.json")
    manifest = build_skill_manifest(root, camc_version, source_commit)
    with open(path, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path
~~~

Add skill_provider before skills in MODULE_ORDER so its functions are defined first in the standalone bundle.

- [ ] **Step 4: Write validator rejection tests**

~~~python
def stage_valid_tree(root):
    root = pathlib.Path(root)
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo\n---\nbody\n")
    provider.write_skill_manifest(str(root), "1.2.0", "abcdef0")
    return root

@pytest.mark.parametrize("mutation, reason", [
    ("schema", "schema"),
    ("version", "version"),
    ("digest", "digest"),
    ("missing", "missing"),
    ("unexpected", "unexpected"),
    ("traversal", "path"),
])
def test_validate_provider_rejects_complete_tree(tmp_path, mutation, reason):
    root = stage_valid_tree(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    if mutation == "schema":
        manifest["schema"] = 99
    elif mutation == "version":
        manifest["camc_version"] = "0.0.0"
    elif mutation == "digest":
        manifest["skills"]["demo"]["files"]["SKILL.md"] = "0" * 64
    elif mutation == "missing":
        (root / "demo" / "SKILL.md").unlink()
    elif mutation == "unexpected":
        (root / "demo" / "extra.md").write_text("extra")
    elif mutation == "traversal":
        manifest["skills"]["demo"]["files"]["../escape"] = "0" * 64

    if mutation in {"schema", "version", "digest", "traversal"}:
        manifest_path.write_text(json.dumps(manifest))

    store, info = provider.validate_skill_provider(str(root), "1.2.0")
    assert store is None
    assert info["valid"] is False
    assert reason in info["reason"].lower()
~~~

- [ ] **Step 5: Run rejection tests and verify RED**

Run: python3 -m pytest tests/test_camc_skill_provider.py -q

Expected: FAIL because validate_skill_provider is absent.

- [ ] **Step 6: Implement all-or-nothing validation**

~~~python
def _safe_relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    normalized = posixpath.normpath(value)
    return (value == normalized and not value.startswith("/")
            and value != ".." and not value.startswith("../"))

def validate_skill_provider(root, camc_version):
    info = {"root": os.path.realpath(root), "valid": False, "reason": ""}
    try:
        manifest_path = os.path.join(root, "manifest.json")
        with open(manifest_path, "r") as handle:
            manifest = json.load(handle)
        if manifest.get("schema") != SKILL_MANIFEST_SCHEMA:
            raise ValueError("unsupported manifest schema")
        if manifest.get("camc_version") != camc_version:
            raise ValueError("incompatible CAMC version")
        declared = manifest.get("skills")
        if not isinstance(declared, dict):
            raise ValueError("invalid skills mapping")

        expected = {}
        for skill_name, entry in declared.items():
            if not _safe_relative_path(skill_name) or "/" in skill_name:
                raise ValueError("invalid skill path")
            files = entry.get("files") if isinstance(entry, dict) else None
            if not isinstance(files, dict):
                raise ValueError("invalid files mapping")
            for rel, digest in files.items():
                if not _safe_relative_path(rel):
                    raise ValueError("invalid file path")
                expected[(skill_name, rel)] = digest

        actual_store = scan_skill_tree(root)
        actual = {
            (skill, rel): hashlib.sha256(payload).hexdigest()
            for skill, files in actual_store.items()
            for rel, payload in files.items()
        }
        if set(expected) - set(actual):
            raise ValueError("missing declared skill file")
        if set(actual) - set(expected):
            raise ValueError("unexpected skill file")
        for key in sorted(expected):
            if expected[key] != actual[key]:
                raise ValueError("digest mismatch: %s/%s" % key)

        info.update({"valid": True, "reason": "", "manifest": manifest})
        return actual_store, info
    except (IOError, OSError, ValueError, TypeError) as exc:
        info["reason"] = str(exc)
        return None, info
~~~

- [ ] **Step 7: Run focused provider tests and verify GREEN**

Run: python3 -m pytest tests/test_camc_skill_provider.py -q

Expected: PASS.

- [ ] **Step 8: Review checkpoint**

Run: git diff --check && python3 -m py_compile src/camc_pkg/skill_provider.py

Expected: no output from diff check and successful compilation.

---

### Task 2: Runtime provider selection with embedded fallback

**Files:**
- Modify: src/camc_pkg/skills.py
- Modify: src/camc_pkg/cli.py: cmd_skills dispatch and parser
- Test: tests/test_camc_skill_provider.py

**Interfaces:**
- Consumes: validate_skill_provider, sys.argv[0], CAMC_SKILLS_DIR, the development skills directory, and _EMBEDDED_SKILLS.
- Produces: resolve_skill_provider(executable=None, environ=None) -> (text_store, info), get_skill_provider_info() -> dict, and camc skills provider --json.

- [ ] **Step 1: Write provider-precedence and fallback tests**

~~~python
def test_valid_override_wins_over_adjacent(tmp_path, monkeypatch):
    override = stage_valid_tree(tmp_path / "override")
    adjacent = stage_valid_tree(tmp_path / "install" / "camc_pkg" / "skills")
    executable = tmp_path / "install" / "camc"
    executable.write_text("#!/bin/sh\n")

    store, info = skills.resolve_skill_provider(
        executable=str(executable),
        environ={"CAMC_SKILLS_DIR": str(override)},
    )

    assert info["source"] == "override"
    assert info["root"] == os.path.realpath(str(override))
    assert store["demo"]["SKILL.md"].startswith("---")

def test_missing_adjacent_uses_embedded_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_EMBEDDED_SKILLS",
                        {"embedded": {"SKILL.md": "---\nname: embedded\n---\n"}})
    store, info = skills.resolve_skill_provider(
        executable=str(tmp_path / "camc"), environ={})
    assert info["source"] == "embedded"
    assert "embedded" in store

def test_invalid_adjacent_rejects_tree_and_uses_embedded(tmp_path, monkeypatch):
    root = stage_valid_tree(tmp_path / "camc_pkg" / "skills")
    (root / "demo" / "SKILL.md").write_text("corrupt")
    executable = tmp_path / "camc"
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr(skills, "_EMBEDDED_SKILLS",
                        {"embedded": {"SKILL.md": "fallback"}})

    store, info = skills.resolve_skill_provider(
        executable=str(executable), environ={})

    assert info["source"] == "embedded"
    assert "digest" in info["fallback_reason"]
    assert store == skills._EMBEDDED_SKILLS
~~~

- [ ] **Step 2: Run provider-selection tests and verify RED**

Run: python3 -m pytest tests/test_camc_skill_provider.py -k "override or adjacent" -q

Expected: FAIL because resolve_skill_provider does not exist.

- [ ] **Step 3: Implement provider resolution and byte decoding**

~~~python
def _decode_store(byte_store):
    return {
        skill: {rel: payload.decode("utf-8") for rel, payload in files.items()}
        for skill, files in byte_store.items()
    }

def resolve_skill_provider(executable=None, environ=None):
    environ = os.environ if environ is None else environ
    executable = executable or (
        sys.argv[0] if sys.argv and os.path.isfile(sys.argv[0]) else "")
    adjacent = ""
    if executable:
        adjacent = os.path.join(
            os.path.dirname(os.path.realpath(executable)), "camc_pkg", "skills")
    override = environ.get("CAMC_SKILLS_DIR", "")
    dev_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

    candidates = []
    if override:
        candidates.append(("override", override, True))
    if adjacent:
        candidates.append(("adjacent", adjacent, True))
    candidates.append(("development", dev_root, False))

    for source, root, requires_manifest in candidates:
        if not os.path.isdir(root):
            continue
        if requires_manifest:
            byte_store, info = validate_skill_provider(root, __version__)
            if byte_store is None:
                return _EMBEDDED_SKILLS, {
                    "source": "embedded",
                    "root": "",
                    "valid": True,
                    "fallback_reason": info["reason"],
                    "rejected_root": info["root"],
                }
        else:
            byte_store = scan_skill_tree(root)
        if byte_store:
            return _decode_store(byte_store), {
                "source": source,
                "root": os.path.realpath(root),
                "valid": True,
                "fallback_reason": "",
            }

    return _EMBEDDED_SKILLS, {
        "source": "embedded", "root": "", "valid": True,
        "fallback_reason": "",
    }

_SKILL_STORE, _SKILL_PROVIDER_INFO = resolve_skill_provider()

def get_skill_provider_info():
    return dict(_SKILL_PROVIDER_INFO)
~~~

Replace list_skills and _install_one lookups of _EMBEDDED_SKILLS with _SKILL_STORE. Write installed payloads as UTF-8 bytes so staged and embedded providers install identically.

- [ ] **Step 4: Add a stable provider diagnostic**

~~~python
def cmd_skills_provider(args):
    info = get_skill_provider_info()
    if _want_json(args):
        print(json.dumps(info, indent=2, sort_keys=True))
    else:
        print("%s  %s" % (info["source"], info.get("root") or "(embedded)"))
~~~

Add provider to cmd_skills dispatch and add a provider subparser with --json. Keep list/add/rm output unchanged.

- [ ] **Step 5: Test CLI listing and installation through both providers**

~~~python
@pytest.mark.parametrize("source", ["external", "embedded"])
def test_list_and_install_use_resolved_store(tmp_path, monkeypatch, source):
    text = "---\nname: demo\ndescription: Demo skill\n---\nbody\n"
    store = {"demo": {"SKILL.md": text, "reference/usage.md": "usage\n"}}
    monkeypatch.setattr(skills, "_SKILL_STORE", store)
    monkeypatch.setattr(skills, "SKILLS_MANIFEST",
                        str(tmp_path / "home" / "skills.json"))
    skills.save_manifest(["demo"])

    rows = skills.list_skills()
    result = skills.install_manifest_skills(str(tmp_path / "work"))

    assert rows[0]["name"] == "demo"
    assert result == {"demo": "created"}
    assert (tmp_path / "work" / ".claude" / "skills" /
            "demo" / "reference" / "usage.md").read_bytes() == b"usage\n"
~~~

- [ ] **Step 6: Run runtime-provider tests and verify GREEN**

Run: python3 -m pytest tests/test_camc_skill_provider.py -q

Expected: PASS.

- [ ] **Step 7: Review checkpoint**

Run: python3 -m pytest tests/test_camc_skill_provider.py tests/test_scheduler.py -q && git diff --check

Expected: PASS and no whitespace errors.

---

### Task 3: Repair build ordering and stage both artifacts

**Files:**
- Modify: build_camc.py
- Modify: src/camc_pkg/skill_provider.py
- Create: tests/test_build_camc_companion.py
- Generated later: dist/camc, dist/camc_pkg/skills/**, src/camc

**Interfaces:**
- Consumes: transformed module bodies and authored skill bytes.
- Produces: stage_companion_skills(output_dir, version, source_commit) -> str and verify_artifacts(output_path) -> None; CLI flags --verify and --verify-only.

- [ ] **Step 1: Write a regression test for Markdown import preservation**

~~~python
def test_build_preserves_top_level_import_examples(tmp_path):
    output = tmp_path / "camc"
    subprocess.run(
        [sys.executable, str(ROOT / "build_camc.py"),
         "--output", str(output), "--verify"],
        cwd=str(ROOT), check=True, text=True, capture_output=True,
    )
    namespace = runpy.run_path(str(output), run_name="__camc_verify_test__")
    embedded = namespace["_EMBEDDED_SKILLS"]

    diagnose = (ROOT / "src/camc_pkg/skills/camc-diagnose/SKILL.md").read_text()
    sessions = (ROOT / "src/camc_pkg/skills/managing-camc/reference/sessions.md").read_text()
    assert embedded["camc-diagnose"]["SKILL.md"] == diagnose
    assert embedded["managing-camc"]["reference/sessions.md"] == sessions
    assert "import json, sys" in diagnose
    assert "import json, sys, os" in sessions
~~~

- [ ] **Step 2: Run the payload test and verify RED**

Run: python3 -m pytest tests/test_build_camc_companion.py::test_build_preserves_top_level_import_examples -q

Expected: FAIL because the current builder strips the import examples.

- [ ] **Step 3: Move payload injection after source transformation**

~~~python
def read_module(name):
    path = (os.path.join(PKG_DIR, name.replace(".", os.sep) + ".py")
            if "." in name else os.path.join(PKG_DIR, "%s.py" % name))
    with open(path, "r") as handle:
        return handle.read()

def transform_module(name, source):
    body = strip_imports(source, name)
    body = strip_stdlib_imports(body)
    body = strip_docstring(body)
    if name == "adapters":
        body = _inject_embedded_configs(body)
    elif name == "skills":
        body = _inject_embedded_skills(body)
    return body
~~~

Use transform_module only in the assembly loop, after collect_stdlib_imports has inspected raw Python source. Generate embedded string values with repr(content) after decoding authored UTF-8 bytes, not raw triple-quoted literals.

- [ ] **Step 4: Write companion staging and exact-manifest tests**

~~~python
def assert_tree_bytes_equal(source, staged, ignore=frozenset()):
    def files(root):
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and path.name not in ignore
        }
    assert files(source) == files(staged)

def test_build_stages_companion_tree_next_to_output(tmp_path):
    output = tmp_path / "camc"
    subprocess.run(
        [sys.executable, str(ROOT / "build_camc.py"),
         "--output", str(output), "--verify"],
        cwd=str(ROOT), check=True, text=True, capture_output=True,
    )
    staged = tmp_path / "camc_pkg" / "skills"
    manifest = json.loads((staged / "manifest.json").read_text())

    assert manifest["schema"] == 1
    assert manifest["camc_version"] == "1.2.0"
    assert set(manifest["skills"]) == {
        path.name for path in (ROOT / "src/camc_pkg/skills").iterdir()
        if path.is_dir()
    }
    assert_tree_bytes_equal(
        ROOT / "src/camc_pkg/skills", staged, ignore={"manifest.json"})
~~~

- [ ] **Step 5: Implement clean staging beside --output**

~~~python
def stage_companion_skills(output_path, version, source_commit):
    output_dir = os.path.dirname(os.path.abspath(output_path))
    root = os.path.join(output_dir, "camc_pkg", "skills")
    temp = root + ".tmp.%d" % os.getpid()
    if os.path.exists(temp):
        shutil.rmtree(temp)
    os.makedirs(temp)
    store = scan_skill_tree(SKILLS_DIR)
    for skill_name in sorted(store):
        for rel, payload in sorted(store[skill_name].items()):
            destination = os.path.join(temp, skill_name, *rel.split("/"))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as handle:
                handle.write(payload)
    write_skill_manifest(temp, version, source_commit)
    loaded, info = validate_skill_provider(temp, version)
    if loaded is None:
        raise RuntimeError("staged companion invalid: %s" % info["reason"])
    if os.path.exists(root):
        shutil.rmtree(root)
    os.replace(temp, root)
    return root
~~~

Call stage_companion_skills after writing the executable. BUILD_LOG.md must be written beside the chosen output so temporary test builds do not mutate dist/BUILD_LOG.md.

- [ ] **Step 6: Write verification-contract tests**

~~~python
def test_verify_only_rejects_missing_companion(tmp_path):
    output = build_verified_artifact(tmp_path)
    shutil.rmtree(tmp_path / "camc_pkg")
    result = subprocess.run(
        [sys.executable, str(ROOT / "build_camc.py"),
         "--output", str(output), "--verify-only"],
        cwd=str(ROOT), text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert "companion" in (result.stdout + result.stderr).lower()

def test_verify_accepts_only_version_build_stamp_difference(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "build_camc.py"),
         "--output", str(tmp_path / "camc"), "--verify"],
        cwd=str(ROOT), text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verification passed" in result.stdout
~~~

- [ ] **Step 7: Implement artifact verification**

~~~python
def _normalized_cli_output(command, output):
    if command == "version":
        lines = output.splitlines()
        if lines:
            lines[0] = re.sub(r"\s+\([^)]*\)$", " (<build>)", lines[0])
        return "\n".join(lines) + ("\n" if output.endswith("\n") else "")
    return output

def verify_artifacts(output_path):
    env = dict(os.environ)
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + old_path if old_path else "")
    for command in ("--help", "run --help", "list --help", "version"):
        args = command.split()
        package = subprocess.check_output(
            [sys.executable, "-m", "camc_pkg"] + args,
            cwd=os.path.dirname(__file__), env=env,
            stderr=subprocess.STDOUT).decode("utf-8")
        bundled = subprocess.check_output(
            [sys.executable, output_path] + args,
            cwd=os.path.dirname(__file__), env=env,
            stderr=subprocess.STDOUT).decode("utf-8")
        if (_normalized_cli_output(command, package) !=
                _normalized_cli_output(command, bundled)):
            raise RuntimeError("CLI mismatch for %s" % command)

    namespace = runpy.run_path(output_path, run_name="__camc_verify__")
    authored = scan_skill_tree(SKILLS_DIR)
    embedded = {
        skill: {rel: text.encode("utf-8") for rel, text in files.items()}
        for skill, files in namespace["_EMBEDDED_SKILLS"].items()
    }
    if embedded != authored:
        raise RuntimeError("embedded skill payload mismatch")

    companion = os.path.join(
        os.path.dirname(os.path.abspath(output_path)), "camc_pkg", "skills")
    staged, info = validate_skill_provider(
        companion, namespace["__version__"])
    if staged is None:
        raise RuntimeError("companion invalid: %s" % info["reason"])
    if staged != authored:
        raise RuntimeError("companion skill payload mismatch")
~~~

--verify builds, stages, and verifies. --verify-only calls verify_artifacts against existing artifacts without rewriting them or appending a build log.

- [ ] **Step 8: Run builder tests and verify GREEN**

Run: python3 -m pytest tests/test_build_camc_companion.py tests/test_camc_skill_provider.py -q

Expected: PASS.

- [ ] **Step 9: Review checkpoint**

Run: python3 build_camc.py --output /tmp/camc-plan-check/camc --verify && /tmp/camc-plan-check/camc skills provider --json

Expected: build verification passes; provider reports adjacent and /tmp/camc-plan-check/camc_pkg/skills.

---

### Task 4: Release binary and companion tree as one unit

**Files:**
- Modify: scripts/release.sh
- Create: tests/test_release_companion_skills.py

**Interfaces:**
- Consumes: a verified DIST_BIN and DIST_SKILLS tree.
- Produces: paired host install, paired shared-bin layout, paired archive layout, and dry-run output describing both.

- [ ] **Step 1: Write release dry-run tests**

~~~python
def build_artifact(output):
    output.parent.mkdir(parents=True)
    subprocess.run(
        [sys.executable, str(ROOT / "build_camc.py"),
         "--output", str(output), "--verify"],
        cwd=str(ROOT), check=True, text=True, capture_output=True)

def run_release_dry(tmp_path, output):
    machines = tmp_path / "machines.json"
    machines.write_text(json.dumps([{
        "type": "ssh", "name": "demo", "host": "example.invalid",
        "user": "tester", "port": 22,
    }]))
    env = dict(os.environ)
    env.update({
        "CAMC_MACHINES_FILE": str(machines),
        "CAMC_RELEASE_DIST_DIR": str(output.parent),
    })
    return subprocess.run(
        ["bash", str(ROOT / "scripts/release.sh"),
         "--skip-build", "--skip-tests", "--dry-run"],
        cwd=str(ROOT), env=env, text=True, capture_output=True)

def test_release_dry_run_contains_companion_and_binary(tmp_path):
    output = tmp_path / "dist" / "camc"
    build_artifact(output)
    machines = tmp_path / "machines.json"
    machines.write_text(json.dumps([{
        "type": "ssh", "name": "demo", "host": "example.invalid",
        "user": "tester", "port": 22,
    }]))
    env = dict(os.environ)
    env.update({
        "CAMC_MACHINES_FILE": str(machines),
        "CAMC_RELEASE_DIST_DIR": str(output.parent),
    })

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/release.sh"),
         "--skip-build", "--skip-tests", "--dry-run"],
        cwd=str(ROOT), env=env, text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "camc_pkg/skills" in result.stdout
    assert ".camc.new" in result.stdout
    assert ".skills.new" in result.stdout
    assert "skills provider --json" in result.stdout

def test_release_skip_build_rejects_missing_companion(tmp_path):
    output = tmp_path / "dist" / "camc"
    build_artifact(output)
    shutil.rmtree(output.parent / "camc_pkg")
    result = run_release_dry(tmp_path, output)
    assert result.returncode != 0
    assert "companion" in (result.stdout + result.stderr).lower()
~~~

- [ ] **Step 2: Run release tests and verify RED**

Run: python3 -m pytest tests/test_release_companion_skills.py -q

Expected: FAIL because release.sh knows only dist/camc.

- [ ] **Step 3: Add mandatory local artifact verification**

Set DIST_DIR from CAMC_RELEASE_DIST_DIR with the repository dist directory as default. Define DIST_BIN and DIST_SKILLS from it. Normal builds run python3 build_camc.py --output "$DIST_BIN" --verify. --skip-build requires both artifacts and runs python3 build_camc.py --output "$DIST_BIN" --verify-only. No flag may skip this pairing check.

- [ ] **Step 4: Implement per-host skills-first installation**

Use host-local temporary names and this exact ordering inside the machine loop:

~~~bash
remote_bin_tmp="~/.cam/.camc.new.$"
remote_skills_tmp="~/.cam/camc_pkg/.skills.new.$"
remote_skills_old="~/.cam/camc_pkg/.skills.old.$"
ssh "$target" "rm -rf $remote_skills_tmp $remote_skills_old && mkdir -p $remote_skills_tmp"
scp -r "$DIST_SKILLS/." "$target:$remote_skills_tmp/"
scp "$DIST_BIN" "$target:$remote_bin_tmp"
ssh "$target" "chmod 0755 $remote_bin_tmp"
provider_json="$(ssh "$target" "CAMC_SKILLS_DIR=$remote_skills_tmp $remote_bin_tmp skills provider --json")"
printf '%s' "$provider_json" | grep -q '"source": "override"'
ssh "$target" "if test -e $REMOTE_SKILLS; then mv $REMOTE_SKILLS $remote_skills_old; fi && mv $remote_skills_tmp $REMOTE_SKILLS && rm -rf $remote_skills_old"
ssh "$target" "mv -f $remote_bin_tmp $REMOTE_PATH"
remote_ver="$(ssh "$target" "$REMOTE_PATH version" | head -1 | tr -d '\r')"
provider_json="$(ssh "$target" "$REMOTE_PATH skills provider --json")"
test "$remote_ver" = "$LOCAL_VERSION"
printf '%s' "$provider_json" | grep -q '"source": "adjacent"'
~~~

The implementation uses the existing SSH_OPTS and SCP_OPTS arrays on each corresponding ssh/scp command. Dry-run prints all operations in the same order.

- [ ] **Step 5: Pair shared-bin and archive layouts**

The shared layout is /home/prgn_share/bin/camc plus /home/prgn_share/bin/camc_pkg/skills. The archive layout is /home/prgn_share/tools/camc/releases/<archive-name>/camc plus camc_pkg/skills. Copy through temporary directories and replace only after provider validation. A missing or unwritable optional shared/archive destination remains non-fatal, but it is never recorded as a complete archive when only camc was copied.

~~~bash
shared_tmp="$SHARED_BIN_DIR/.camc-release.$"
shared_cmd="rm -rf $shared_tmp && mkdir -p $shared_tmp/camc_pkg && cp -p $REMOTE_PATH $shared_tmp/camc && cp -a $REMOTE_SKILLS $shared_tmp/camc_pkg/skills && rm -rf $SHARED_BIN_DIR/camc_pkg/skills && mkdir -p $SHARED_BIN_DIR/camc_pkg && mv $shared_tmp/camc_pkg/skills $SHARED_BIN_DIR/camc_pkg/skills && mv -f $shared_tmp/camc $SHARED_BIN_PATH && rmdir $shared_tmp/camc_pkg $shared_tmp"
archive_root="$SHARED_RELEASES_DIR/$ARCHIVE_NAME"
archive_cmd="rm -rf $archive_root.tmp.$ && mkdir -p $archive_root.tmp.$/camc_pkg && cp -p $REMOTE_PATH $archive_root.tmp.$/camc && cp -a $REMOTE_SKILLS $archive_root.tmp.$/camc_pkg/skills && rm -rf $archive_root && mv $archive_root.tmp.$ $archive_root && echo OK"
~~~

- [ ] **Step 6: Run release tests and shell syntax verification**

Run: python3 -m pytest tests/test_release_companion_skills.py -q && bash -n scripts/release.sh

Expected: PASS.

- [ ] **Step 7: Review checkpoint**

Run: git diff --check && scripts/release.sh --help

Expected: no whitespace errors and usage text still documents all supported flags.

---

### Task 5: Generate final artifacts and verify backward compatibility

**Files:**
- Generated: dist/camc
- Generated: dist/camc_pkg/skills/**
- Generated: src/camc
- Modify by builder only: dist/BUILD_LOG.md

**Interfaces:**
- Consumes: all completed source changes.
- Produces: the release-ready paired artifact and the synchronized tracked single-file fallback.

- [ ] **Step 1: Run focused tests**

Run: python3 -m pytest tests/test_camc_skill_provider.py tests/test_build_camc_companion.py tests/test_release_companion_skills.py -q

Expected: PASS.

- [ ] **Step 2: Run the existing CAMC test suite**

Run: python3 -m pytest -q

Expected: PASS.

- [ ] **Step 3: Build and verify final artifacts**

Run: python3 build_camc.py --verify

Expected: all CLI comparisons, embedded payload comparisons, and companion validation pass.

- [ ] **Step 4: Synchronize the tracked standalone artifact**

Run: cp dist/camc src/camc

Expected: cmp dist/camc src/camc exits 0.

- [ ] **Step 5: Verify companion-preferred behavior in an isolated install**

Run: HOME=/tmp/camc-companion-home dist/camc skills provider --json

Expected: source is adjacent and root resolves to dist/camc_pkg/skills.

Run: HOME=/tmp/camc-companion-home dist/camc skills add all

Expected: all authored skills are added without error.

- [ ] **Step 6: Verify single-file fallback in isolation**

Run: mkdir -p /tmp/camc-single-file && cp dist/camc /tmp/camc-single-file/camc && HOME=/tmp/camc-fallback-home /tmp/camc-single-file/camc skills provider --json

Expected: source is embedded.

Run: HOME=/tmp/camc-fallback-home /tmp/camc-single-file/camc skills add all

Expected: all authored skills are added without camc_pkg present.

- [ ] **Step 7: Final static checks**

Run: python3 -m py_compile src/camc_pkg/skill_provider.py src/camc_pkg/skills.py src/camc_pkg/runtime_env.py src/camc_pkg/cli.py dist/camc && bash -n scripts/release.sh && git diff --check

Expected: all commands succeed with no output from git diff --check.

- [ ] **Step 8: Report without deploying or committing**

Summarize changed files, focused/full test results, final build verification, adjacent-provider proof, embedded-fallback proof, and any pre-existing unrelated worktree changes. Do not install to ~/.cam, deploy, tag, commit, or push.

