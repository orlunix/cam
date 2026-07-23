"""Release-version policy for the single-file camc generator."""

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
