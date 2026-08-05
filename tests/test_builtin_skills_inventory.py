"""Regression coverage for the publishable built-in skill inventory."""

from pathlib import Path
import re


SKILLS_ROOT = Path(__file__).parents[1] / "src" / "camc_pkg" / "skills"
EXPECTED_SKILLS = {
    "camc-cron-loop",
    "camc-diagnose",
    "camc-goal-loop",
    "camc-messaging",
    "managing-camc",
}

# The API/profile/proxy feature is not publishable yet.  Keep this guard
# narrow so ordinary software-engineering advice about public APIs remains
# valid in the goal-loop skill.
FORBIDDEN_RELEASE_GUIDANCE = (
    "--api",
    "api-models",
    "api-token",
    "api default",
    "api check",
    "api proxy",
    "inference hub",
    "openai",
    "anthropic",
    "glm-",
    "proxy",
)

BARE_CAMC_COMMAND = re.compile(
    r"(?<![\w~/])camc\s+(?:run|list|status|capture|send|msg(?!#)|heal|cron|key|"
    r"attach|rm|stop|kill|upgrade|version|--help)\b"
)


def test_publishable_builtin_skill_inventory_and_content():
    names = {p.name for p in SKILLS_ROOT.iterdir() if p.is_dir()}
    assert names == EXPECTED_SKILLS

    for path in SKILLS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for marker in FORBIDDEN_RELEASE_GUIDANCE:
            assert marker not in text, "%s contains unpublished guidance %r" % (
                path,
                marker,
            )
        assert not BARE_CAMC_COMMAND.search(text), (
            "%s invokes bare camc; skills must use ~/.cam/camc" % path
        )


def test_goal_loop_skill_is_a_short_template_router_with_deterministic_checks():
    """The built-in goal-loop guide must stay operational, not become a DSL."""
    path = SKILLS_ROOT / "camc-goal-loop" / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    assert len(text.splitlines()) <= 100
    assert "reference/prompt.md" in text
    assert "reference/loop.md" in text
    assert "~/.cam/camc cron add --loop" in text
    assert "deterministic project scripts" in text
    assert "~/.cam/loops/<name>.json" not in text
    assert "history_updated" not in text

    prompt = (path.parent / "reference" / "prompt.md").read_text(encoding="utf-8")
    loop = (path.parent / "reference" / "loop.md").read_text(encoding="utf-8")
    assert "deterministic project script" in prompt
    assert "deterministic project script" in loop
    assert "~/.cam/loops/" not in loop
    assert "history_updated" not in prompt
    assert "history_updated" not in loop
