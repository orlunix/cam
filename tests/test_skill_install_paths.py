"""Tool-specific project skill installation paths."""

from pathlib import Path

import pytest

from camc_pkg import adapters, skills


@pytest.fixture
def one_embedded_skill(monkeypatch):
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
def test_embedded_skills_install_under_selected_tool_config_dir(
    tmp_path, one_embedded_skill, tool, relative_skill_dir
):
    config = (adapters._load_config(tool) if tool != "future-tool"
              else adapters.AdapterConfig({"adapter": {}}))
    result = skills.install_manifest_skills(
        str(tmp_path), config_dir=config.config_dir)

    expected = tmp_path / relative_skill_dir / "demo-skill" / "SKILL.md"
    assert result == {"demo-skill": "created"}
    assert expected.read_text(encoding="utf-8").startswith("---")


def test_embedded_skill_installer_default_remains_claude_compatible(
    tmp_path, one_embedded_skill
):
    skills.install_manifest_skills(str(tmp_path))

    assert (tmp_path / ".claude/skills/demo-skill/SKILL.md").is_file()


def test_embedded_skills_install_without_a_user_manifest(tmp_path, one_embedded_skill):
    manifest = tmp_path / "skills.json"

    result = skills.install_manifest_skills(str(tmp_path), config_dir=".codex")

    assert result == {"demo-skill": "created"}
    assert (tmp_path / ".codex/skills/demo-skill/SKILL.md").is_file()
    assert not manifest.exists()


def test_embedded_skill_install_writes_non_ascii_content(tmp_path, monkeypatch):
    """Skill files contain non-ASCII text; the installer must write
    UTF-8 explicitly so installs never fail on C/POSIX-locale hosts."""
    monkeypatch.setattr(
        skills,
        "_EMBEDDED_SKILLS",
        {"demo": {"SKILL.md": "---\nname: demo\n---\n中文说明 — emoji ✓\n"}},
    )
    result = skills.install_manifest_skills(str(tmp_path), config_dir=".codex")
    assert result == {"demo": "created"}
    assert "中文说明" in (tmp_path / ".codex/skills/demo/SKILL.md").read_text(encoding="utf-8")


def test_codex_skill_install_does_not_modify_agents_md(
    tmp_path, one_embedded_skill
):
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# User instructions\n\nKeep this text.\n", encoding="utf-8")
    original = agents_md.read_text(encoding="utf-8")

    skills.install_manifest_skills(str(tmp_path), config_dir=".codex")
    skills.install_manifest_skills(str(tmp_path), config_dir=".codex")

    assert agents_md.read_text(encoding="utf-8") == original


def test_codex_skill_install_respects_equivalent_user_guidance(
    tmp_path, one_embedded_skill
):
    agents_md = tmp_path / "AGENTS.md"
    original = (
        "# Existing policy\n\n"
        "When a sandbox command fails with Operation not permitted, retry it "
        "once using sandbox_permissions: require_escalated.\n"
    )
    agents_md.write_text(original, encoding="utf-8")

    skills.install_manifest_skills(str(tmp_path), config_dir=".codex")

    assert agents_md.read_text(encoding="utf-8") == original
    assert "camc:codex-host-escalation" not in original


@pytest.mark.parametrize("config_dir", [".claude", ".cursor", ".agents"])
def test_non_codex_skill_install_does_not_add_host_escalation_guidance(
    tmp_path, one_embedded_skill, config_dir
):
    skills.install_manifest_skills(str(tmp_path), config_dir=config_dir)

    assert not (tmp_path / "AGENTS.md").exists()


def test_heal_refreshes_embedded_skills_for_each_local_agent(monkeypatch, tmp_path):
    from camc_pkg import cli

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    agents = [
        {"id": "one", "hostname": "local", "context_path": str(first), "tool": "codex"},
        {"id": "two", "hostname": "local", "context_path": str(second), "tool": "claude"},
    ]
    calls = []
    monkeypatch.setattr(cli, "_is_same_host", lambda *_args: True)
    monkeypatch.setattr(cli, "install_manifest_skills", lambda *args, **kwargs: calls.append((args, kwargs)) or {"demo": "overwritten"})

    refreshed = cli._refresh_embedded_skills_for_local_agents(agents)

    assert refreshed == 2
    assert calls == [
        ((str(first),), {"force": True, "config_dir": ".codex"}),
        ((str(second),), {"force": True, "config_dir": ".claude"}),
    ]


def test_skills_add_and_rm_are_compatibility_noops(capsys):
    from types import SimpleNamespace
    from camc_pkg import cli

    cli.cmd_skills_add(SimpleNamespace(name="camc-messaging"))
    cli.cmd_skills_rm(SimpleNamespace(name="camc-messaging"))

    assert capsys.readouterr().out.count("bundled with camc") == 2
