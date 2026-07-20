"""Tool-specific project skill installation paths."""

from pathlib import Path

import pytest

from camc_pkg import adapters, skills


@pytest.fixture
def one_manifest_skill(monkeypatch):
    monkeypatch.setattr(skills, "load_manifest", lambda: ["demo-skill"])
    monkeypatch.setattr(
        skills,
        "_EMBEDDED_SKILLS",
        {"demo-skill": {"SKILL.md": "---\nname: demo-skill\n---\n"}},
    )


@pytest.mark.parametrize(
    ("tool", "config_dir"),
    [
        ("claude", ".claude"),
        ("codex", ".codex"),
        ("cursor", ".cursor"),
    ],
)
def test_builtin_adapter_config_declares_project_config_dir(tool, config_dir):
    assert adapters._load_config(tool).config_dir == config_dir


@pytest.mark.parametrize(
    ("tool", "relative_skill_dir"),
    [
        ("claude", Path(".claude/skills")),
        ("codex", Path(".codex/skills")),
        ("cursor", Path(".cursor/skills")),
        ("future-tool", Path(".agents/skills")),
    ],
)
def test_manifest_skills_install_under_selected_tool_config_dir(
    tmp_path, one_manifest_skill, tool, relative_skill_dir
):
    config = (adapters._load_config(tool) if tool != "future-tool"
              else adapters.AdapterConfig({"adapter": {}}))
    result = skills.install_manifest_skills(
        str(tmp_path), config_dir=config.config_dir)

    expected = tmp_path / relative_skill_dir / "demo-skill" / "SKILL.md"
    assert result == {"demo-skill": "created"}
    assert expected.read_text(encoding="utf-8").startswith("---")


def test_manifest_skill_installer_default_remains_claude_compatible(
    tmp_path, one_manifest_skill
):
    skills.install_manifest_skills(str(tmp_path))

    assert (tmp_path / ".claude/skills/demo-skill/SKILL.md").is_file()
