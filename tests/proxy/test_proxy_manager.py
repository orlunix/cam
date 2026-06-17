"""Tests for proxy lifecycle keying and reuse guards."""

from camc_pkg.proxy.manager import ROUTE_DEFAULTS, _run_key, ensure_proxy


def test_run_key_differs_by_upstream_url():
    route = "completions_to_messages"
    k1 = _run_key(route, "https://a.example/v1/chat/completions")
    k2 = _run_key(route, "https://b.example/v1/chat/completions")
    assert k1 != k2


def test_run_key_same_for_identical_route_and_upstream():
    route = "completions_to_messages"
    url = "https://inference-api.nvidia.com/v1/chat/completions"
    assert _run_key(route, url) == _run_key(route, url)


def test_ensure_proxy_reuses_matching_record(monkeypatch, tmp_path):
    """Healthy proxy for route+upstream is reused; different upstream is not."""
    from camc_pkg.proxy import manager as mgr

    runs_file = tmp_path / "proxy-runs.json"
    upstream_a = "https://inference-api.nvidia.com/v1/chat/completions"
    upstream_b = "https://other.example/v1/chat/completions"
    route = "completions_to_messages"
    port = ROUTE_DEFAULTS[route]["port"]

    rec_a = {
        "route": route,
        "port": port,
        "pid": 4242,
        "upstream_url": upstream_a,
        "model": "glm-5.1",
        "api": "glm-5.1",
    }
    runs = {_run_key(route, upstream_a): rec_a}
    runs_file.write_text(__import__("json").dumps(runs))

    monkeypatch.setattr(mgr, "PROXY_RUNS_FILE", str(runs_file))
    monkeypatch.setattr(mgr, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(mgr, "_health_ok", lambda port, timeout=1.0: port == port)
    monkeypatch.setattr(mgr, "_start_proxy", lambda **kw: (_ for _ in ()).throw(
        AssertionError("_start_proxy should not run when reuse is valid")))

    plan_a = {
        "mode": "proxy",
        "route": route,
        "proxy_port": port,
        "upstream_url": upstream_a,
        "name": "glm-5.1",
        "model": "nvidia/zai-org/eccn-glm-5.1",
    }
    used_port, rec = ensure_proxy(plan_a, token="tok")
    assert used_port == port
    assert rec == rec_a

    start_calls = []

    def _fake_start(**kwargs):
        start_calls.append(kwargs)
        return 9999, str(tmp_path / "proxy.log")

    monkeypatch.setattr(mgr, "_start_proxy", _fake_start)
    plan_b = dict(plan_a, upstream_url=upstream_b, name="other", model="other-model")
    used_port_b, rec_b = ensure_proxy(plan_b, token="tok")
    assert start_calls, "different upstream must start a new proxy"
    assert used_port_b == port
    assert rec_b["upstream_url"] == upstream_b
