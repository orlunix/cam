"""Embedded official skills — injected by build_camc.py at build time.

Skills ship inside the camc binary (like adapter TOML configs).  This module
provides the embedded store, a dev-mode fallback loader that reads from the
source tree, an inventory list, and automatic installation at agent launch.
The bundled release is the sole skill selection source.
"""

import os

from camc_pkg import log

# ---------------------------------------------------------------------------
# Embedded skills — injected by build_camc.py at build time.
# DO NOT edit here. Edit the source files in src/camc_pkg/skills/.
# Shape: {skill_name: {"relative/path": "file content", ...}}
# ---------------------------------------------------------------------------

_EMBEDDED_SKILLS = {}  # populated by build_camc.py


def _load_dev_skills_fallback():
    """When running camc from the dev source tree, read skill files from
    src/camc_pkg/skills/ on disk.  Silent no-op outside the dev tree.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    skills_root = os.path.normpath(os.path.join(here, "skills"))
    if not os.path.isdir(skills_root):
        return
    for skill_name in sorted(os.listdir(skills_root)):
        skill_dir = os.path.join(skills_root, skill_name)
        if not os.path.isdir(skill_dir):
            continue
        files = {}
        for root, _dirs, filenames in os.walk(skill_dir):
            for fn in filenames:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, skill_dir)
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        files[rel] = f.read()
                except OSError:
                    pass
        if files:
            _EMBEDDED_SKILLS[skill_name] = files


if not _EMBEDDED_SKILLS:
    _load_dev_skills_fallback()


# ---------------------------------------------------------------------------
# Minimal YAML frontmatter parser (name + description only)
# ---------------------------------------------------------------------------

def _parse_skill_frontmatter(text):
    """Extract name and description from SKILL.md YAML frontmatter."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm = text[3:end]
    result = {}
    lines = fm.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if ": " in stripped:
            k, v = stripped.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == ">":
                # Folded block scalar — description value spans indented
                # continuation lines.
                parts = []
                while i < len(lines) and (not lines[i].strip() or lines[i].startswith(" ") or lines[i].startswith("\t")):
                    c = lines[i].strip()
                    if c:
                        parts.append(c)
                    i += 1
                result[k] = " ".join(parts)
            elif v.strip() == "|":
                # Literal block scalar — skip indented block.
                while i < len(lines) and (not lines[i].strip() or lines[i].startswith(" ") or lines[i].startswith("\t")):
                    i += 1
            else:
                result[k] = v
        # Skip nested keys (e.g. metadata:, compatibility:) — only
        # top-level scalars matter for the list display.
    return result


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def list_skills():
    """Return [{name, description, in_manifest}, ...] for bundled skills."""
    rows = []
    for name, files in sorted(_EMBEDDED_SKILLS.items()):
        skill_md = files.get("SKILL.md", "")
        fm = _parse_skill_frontmatter(skill_md)
        description = fm.get("description", "(no description)")
        rows.append({
            "name": name,
            "description": description,
            "in_manifest": True,
        })
    return rows


# ---------------------------------------------------------------------------
# Auto-install bundled skills to project directory
# ---------------------------------------------------------------------------

def install_manifest_skills(workdir, force=False, config_dir=".claude"):
    """Install bundled skills into <workdir>/<tool-config-dir>/skills/.

    Called automatically by camc run and scheduler before agent launch.
    The historical function name remains for internal compatibility.
    """
    out_dir = os.path.join(workdir, config_dir, "skills")
    results = {}
    for name in sorted(_EMBEDDED_SKILLS):
        results.update(_install_one(name, out_dir, force))
    return results


# ---------------------------------------------------------------------------
# Internal: write one skill to disk
# ---------------------------------------------------------------------------

def _install_one(name, out_dir, force):
    """Install a single skill's files to out_dir.

    Returns {name: "created" | "overwritten" | "skipped_exists" | "error:..."}
    """
    files = _EMBEDDED_SKILLS.get(name)
    if not files:
        return {name: "error:unknown skill '%s'" % name}

    skill_dir = os.path.join(out_dir, name)
    existed = os.path.exists(skill_dir)

    if existed and not force:
        return {name: "skipped_exists"}

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        pass

    # Remove existing symlink or directory when forcing
    if existed and force:
        if os.path.islink(skill_dir):
            os.unlink(skill_dir)
        # For directories, don't rmtree — os.replace overwrites each
        # file in place below. rmtree fails on NFS with stray lock files.

    try:
        os.makedirs(skill_dir, exist_ok=True)
        for rel_path, content in files.items():
            dest = os.path.join(skill_dir, rel_path)
            dest_dir = os.path.dirname(dest)
            if dest_dir != skill_dir:
                os.makedirs(dest_dir, exist_ok=True)
            tmp = dest + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, dest)
    except OSError as e:
        log.warning("install_skill %s: %s", name, e)
        return {name: "error:%s" % e}

    return {name: "overwritten" if existed else "created"}
