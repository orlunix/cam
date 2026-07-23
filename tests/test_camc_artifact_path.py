"""All shipped camc consumers use the single dist/camc artifact."""

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_desktop_and_cam_sync_use_dist_camc():
    desktop = json.loads((ROOT / "apps/cam-desktop/package.json").read_text())
    resources = desktop["build"]["extraResources"]
    assert {item["from"] for item in resources} >= {"../../dist/camc"}

    remote_source = (ROOT / "src/camc_pkg/remote.py").read_text()
    assert '"dist", "camc"' in remote_source

    mobile_build = (ROOT / "android/build.sh").read_text()
    assert 'CAMC_DIST="$PROJ_DIR/../dist/camc"' in mobile_build


def test_duplicate_src_camc_artifact_is_absent():
    assert not (ROOT / "src/camc").exists()
