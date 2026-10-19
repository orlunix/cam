"""Ordinary commands must not initialize the HTTP proxy dependency tree."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import select

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("lazy_builder", ROOT / "build_camc.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    path = tmp_path_factory.mktemp("lazy-import") / "camc"
    path.write_text(builder.build())
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("entry", ["source", "standalone"])
def test_help_avoids_http_ssl_dependencies(candidate, entry):
    program = """
import runpy, sys
sys.argv = [sys.argv[1], 'run', '--help']
try:
    ENTRY
except SystemExit as exc:
    assert exc.code == 0
for name in ['http.server', 'http.client', 'ssl', 'urllib.request', 'camc_pkg.api_proxy']:
    assert name not in sys.modules, name
""".replace("ENTRY", "runpy.run_module('camc_pkg', run_name='__main__')" if entry == "source"
            else "runpy.run_path(sys.argv[0], run_name='__main__')")
    result = subprocess.run([sys.executable, "-c", program, str(candidate)],
                            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0, result.stderr


def test_bundle_proxy_initializes_once_on_first_use(candidate):
    program = """
import runpy, sys
ns = runpy.run_path(sys.argv[1], run_name='bundle_test')
g = ns['_camc_load_api_proxy'].__globals__
assert not g['_camc_api_proxy_loaded']
ns['_api_proxy_stop']({'id': 'test-owner'}, cleanup_home=False)
assert g['_camc_api_proxy_loaded']
handler = g['_Handler']
ns['_camc_load_api_proxy']()
assert g['_Handler'] is handler
try:
    ns['_api_proxy_run'](['--help'])
except SystemExit as exc:
    assert exc.code == 0
assert 'http.server' in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", program, str(candidate)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0, result.stderr


def test_api_catalog_still_fetches_when_requested(monkeypatch):
    import urllib.request
    from camc_pkg import api_store

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            pass
        def read(self):
            return json.dumps({"data": [{"id": "test-model"}]}).encode()

    seen = []
    def fetch(request, timeout):
        seen.append((request.full_url, timeout))
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    assert api_store.fetch_model_catalog({"base_url": "https://example.invalid/v1"}, "test") == {"test-model"}
    assert seen


def test_bundle_proxy_starts_and_answers_health(candidate, tmp_path):
    import urllib.request
    env = dict(os.environ, HOME=str(tmp_path))
    process = subprocess.Popen(
        [str(candidate), "_api_proxy", "--upstream-url", "http://127.0.0.1:1",
         "--upstream-model", "test-model", "--alias", "test-alias"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True)
    try:
        readable, _, _ = select.select([process.stdout], [], [], 8)
        assert readable, "proxy startup timed out"
        line = process.stdout.readline()
        assert line.strip().isdigit(), line
        port = int(line.strip())
        with urllib.request.urlopen("http://127.0.0.1:%s/health" % port, timeout=3) as response:
            body = json.load(response)
        assert body["ok"] is True and body["route"] == "camc_proxy"
    finally:
        process.terminate()
        process.communicate(timeout=5)
