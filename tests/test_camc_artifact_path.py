"""All shipped camc consumers use the single dist/camc artifact."""

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_desktop_and_cam_sync_use_dist_camc():
    desktop = json.loads((ROOT / "apps/cam-desktop/package.json").read_text())
    resources = desktop["build"]["extraResources"]
    assert {item["from"] for item in resources} >= {"../../dist/camc"}

    manager_source = (ROOT / "src/cam/core/agent_manager.py").read_text()
    assert '"dist" / "camc"' in manager_source
    assert 'parent.parent / "camc.py"' not in manager_source

    mobile_build = (ROOT / "apps/cam-mobile/build.sh").read_text()
    assert 'CAMC_DIST="$PROJ_DIR/../../dist/camc"' in mobile_build


def test_duplicate_src_camc_artifact_is_absent():
    assert not (ROOT / "src/camc").exists()


def test_operator_docs_describe_the_embedded_per_agent_proxy():
    texts = [
        (ROOT / "docs/code-layout.md").read_text(),
        (ROOT / "docs/inference-hub.md").read_text(),
        (ROOT / "scripts/README.md").read_text(),
    ]
    assert all("There is no production proxy" not in text for text in texts)
    assert "one loopback Camc Proxy per agent" in texts[1]
