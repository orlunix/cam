"""Release-version policy for the single-file camc generator."""

import ast
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("build_camc", ROOT / "build_camc.py")
build_camc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_camc)


def test_normal_build_bumps_only_patch_version():
    updated, version = build_camc._versioned_init_source(
        '__version__ = "1.2.999"\n', requested=None)

    assert version == "1.2.1000"
    assert updated == '__version__ = "1.2.1000"\n'


def test_explicit_version_is_the_only_way_to_change_major_or_minor():
    updated, version = build_camc._versioned_init_source(
        '__version__ = "1.2.1"\n', requested="2.0.0")

    assert version == "2.0.0"
    assert updated == '__version__ = "2.0.0"\n'


@pytest.mark.parametrize("requested", ("1.2", "v1.2.3", "1.2.3.4", "one.two.three"))
def test_explicit_version_requires_three_numeric_parts(requested):
    with pytest.raises(ValueError):
        build_camc._versioned_init_source('__version__ = "1.2.1"\n', requested)


def test_built_skill_documents_preserve_every_source_file():
    """Import consolidation must not remove lines inside Markdown examples."""
    tree = ast.parse(build_camc.build())
    embedded = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "_EMBEDDED_SKILLS"
                        for target in node.targets))
    root = ROOT / "src" / "camc_pkg" / "skills"
    expected = {skill.name: {
        path.relative_to(skill).as_posix(): path.read_text(encoding="utf-8")
        for path in skill.rglob("*") if path.is_file()
    } for skill in root.iterdir() if skill.is_dir()}
    assert embedded == expected
