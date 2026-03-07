"""Core skill management logic."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx

SKILLS_JSON = "skills.json"
SKILLS_DIR = ".skills"
REGISTRY_FILE = ".registry.json"

# GitHub raw content base
GH_API = "https://api.github.com"
GH_RAW = "https://raw.githubusercontent.com"


@dataclass
class SkillEntry:
    source: str  # "owner/repo" or "owner/repo/path"
    enabled: bool = True
    version: str = "latest"
    installed_at: Optional[str] = None

    def name(self) -> str:
        """Derive skill name from source — last path segment."""
        parts = self.source.rstrip("/").split("/")
        return parts[-1] if len(parts) > 2 else parts[1]


@dataclass
class SkillsConfig:
    skills: dict[str, SkillEntry] = field(default_factory=dict)
    registries: list[str] = field(default_factory=lambda: ["https://skills.sh/api"])

    @classmethod
    def load(cls, root: Path) -> "SkillsConfig":
        p = root / SKILLS_JSON
        if not p.exists():
            return cls()
        data = json.loads(p.read_text())
        skills = {}
        for name, v in data.get("skills", {}).items():
            if isinstance(v, str):
                skills[name] = SkillEntry(source=v)
            else:
                skills[name] = SkillEntry(**v)
        return cls(
            skills=skills,
            registries=data.get("registries", ["https://skills.sh/api"]),
        )

    def save(self, root: Path) -> None:
        data = {
            "skills": {},
            "registries": self.registries,
        }
        for name, entry in self.skills.items():
            d = asdict(entry)
            # Clean up None values
            d = {k: v for k, v in d.items() if v is not None}
            data["skills"][name] = d
        p = root / SKILLS_JSON
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def resolve_source(source: str) -> tuple[str, str, str]:
    """Parse source into (owner, repo, subpath).

    Formats:
      - owner/repo           → skill at repo root
      - owner/repo/sub/path  → skill in subdir
    """
    parts = source.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid source: {source!r} — expected owner/repo[/path]")
    owner, repo = parts[0], parts[1]
    subpath = "/".join(parts[2:]) if len(parts) > 2 else ""
    return owner, repo, subpath


def fetch_skill_files(source: str, branch: str = "main") -> dict[str, bytes]:
    """Fetch all files from a skill directory on GitHub.

    Uses tarball download (no API rate limit) with fallback branches.
    Returns {relative_path: content_bytes}.
    """
    import tarfile
    import io
    import os

    owner, repo, subpath = resolve_source(source)

    headers = {"User-Agent": "skillm/0.1"}
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if gh_token:
        headers["Authorization"] = f"token {gh_token}"

    client = httpx.Client(timeout=60, follow_redirects=True, headers=headers)

    # Download tarball — no API rate limit for this endpoint
    tarball_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.tar.gz"
    resp = client.get(tarball_url)

    if resp.status_code == 404:
        branch = "master"
        tarball_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.tar.gz"
        resp = client.get(tarball_url)

    client.close()

    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to download {owner}/{repo} (branch: {branch}): {resp.status_code}"
        )

    # Extract from tarball
    files: dict[str, bytes] = {}
    tar = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz")

    # Tarball root is typically "{repo}-{branch}/"
    prefix = None
    for member in tar.getmembers():
        if prefix is None:
            prefix = member.name.split("/")[0]

        if not member.isfile():
            continue

        # Strip the tarball root prefix
        parts = member.name.split("/", 1)
        if len(parts) < 2:
            continue
        rel = parts[1]

        # Filter to subpath if specified
        if subpath:
            if not rel.startswith(subpath + "/") and rel != subpath:
                continue
            rel = rel[len(subpath):].lstrip("/")

        if not rel:
            continue

        # Skip common non-skill files
        skip = (".git", "node_modules", "__pycache__", ".DS_Store", ".github")
        if any(s in rel.split("/") for s in skip):
            continue

        f = tar.extractfile(member)
        if f:
            files[rel] = f.read()

    tar.close()

    if not files:
        raise RuntimeError(f"No files found at {source} (branch: {branch})")

    # Verify SKILL.md exists
    skill_md_names = [f for f in files if f.upper().endswith("SKILL.MD")]
    if not skill_md_names:
        raise RuntimeError(f"No SKILL.md found in {source} — is this a valid skill?")

    return files


def install_skill(root: Path, source: str, name: str | None = None) -> str:
    """Fetch and install a skill into .skills/."""
    entry = SkillEntry(source=source)
    skill_name = name or entry.name()

    # Fetch files
    files = fetch_skill_files(source)

    # Install to .skills/<name>/
    skill_dir = root / SKILLS_DIR / skill_name
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True)

    for rel_path, content in files.items():
        out = skill_dir / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)

    # Update skills.json
    from datetime import datetime

    entry.installed_at = datetime.now().isoformat()
    config = SkillsConfig.load(root)
    config.skills[skill_name] = entry
    config.save(root)

    return skill_name


def remove_skill(root: Path, name: str) -> None:
    """Remove a skill completely."""
    skill_dir = root / SKILLS_DIR / name
    if skill_dir.exists():
        shutil.rmtree(skill_dir)

    config = SkillsConfig.load(root)
    config.skills.pop(name, None)
    config.save(root)


def toggle_skill(root: Path, name: str, enabled: bool) -> None:
    """Enable or disable a skill."""
    config = SkillsConfig.load(root)
    if name not in config.skills:
        raise KeyError(f"Skill {name!r} not found in skills.json")
    config.skills[name].enabled = enabled
    config.save(root)


def list_skills(root: Path) -> list[tuple[str, SkillEntry, bool]]:
    """List all skills with (name, entry, is_installed)."""
    config = SkillsConfig.load(root)
    results = []
    for name, entry in config.skills.items():
        installed = (root / SKILLS_DIR / name / "SKILL.md").exists() or any(
            (root / SKILLS_DIR / name).glob("**/SKILL.md")
        )
        results.append((name, entry, installed))
    return results


def sync_skills(root: Path) -> list[str]:
    """Install all skills from skills.json that aren't installed yet."""
    config = SkillsConfig.load(root)
    installed = []
    for name, entry in config.skills.items():
        skill_dir = root / SKILLS_DIR / name
        if not skill_dir.exists():
            install_skill(root, entry.source, name)
            installed.append(name)
    return installed


def inject_claude_md(root: Path) -> str:
    """Generate skills section for CLAUDE.md."""
    config = SkillsConfig.load(root)
    enabled = {n: e for n, e in config.skills.items() if e.enabled}

    if not enabled:
        return ""

    lines = [
        "",
        "## Project Skills (auto-generated by skillm)",
        "",
        "Available skills for this project:",
        "",
    ]

    for name, entry in enabled.items():
        # Try to read the first line of SKILL.md for description
        skill_dir = root / SKILLS_DIR / name
        desc = ""
        for md in skill_dir.rglob("SKILL.md"):
            text = md.read_text(errors="ignore")
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("---"):
                    desc = line[:120]
                    break
            break

        lines.append(f"- **{name}** (`{entry.source}`): {desc}")
        lines.append(f"  → Read `.skills/{name}/SKILL.md` when relevant")

    lines.append("")
    lines.append("Only read a skill's SKILL.md when the current task matches it.")
    lines.append("<!-- end:skillm -->")
    lines.append("")

    return "\n".join(lines)


def update_claude_md(root: Path) -> bool:
    """Inject or update skills section in CLAUDE.md."""
    claude_md = root / "CLAUDE.md"
    section = inject_claude_md(root)

    START_MARKER = "## Project Skills (auto-generated by skillm)"
    END_MARKER = "<!-- end:skillm -->"

    if claude_md.exists():
        content = claude_md.read_text()
        if START_MARKER in content:
            # Replace existing section
            start = content.index(START_MARKER)
            # Find a blank line before the marker
            pre_start = content.rfind("\n\n", 0, start)
            if pre_start == -1:
                pre_start = start

            end = content.index(END_MARKER) + len(END_MARKER)
            # Include trailing newline
            if end < len(content) and content[end] == "\n":
                end += 1

            content = content[:pre_start] + section + content[end:]
        else:
            content = content.rstrip() + "\n" + section
    else:
        content = f"# Project Configuration\n{section}"

    claude_md.write_text(content)
    return True
