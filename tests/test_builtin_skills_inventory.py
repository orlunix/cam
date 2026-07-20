"""Regression coverage for the publishable built-in skill inventory."""

from pathlib import Path


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
