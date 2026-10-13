# Code layout

CAMC is a stdlib-only package that is concatenated into `dist/camc` by
`build_camc.py`. Provider profiles live in `api_store.py`, endpoint selection
in `api_routing.py`, the per-agent native proxy in `api_proxy.py`, and
tool-specific runtime policy in `src/cam/adapters/configs/*.toml`. Replaceable
launcher policy (raw templates, named presets, LSF/SSH discovery, and qsub
fallback) is isolated in `src/camc_pkg/tool_launcher.py`; `cli.py` and
`monitor.py` contain integration hooks only.

```text
cam/
├── src/camc_pkg/          # CAMC source modules
├── src/cam/adapters/      # tool adapter TOML and runtime config
├── tests/                 # unit and integration tests
├── docs/                  # design and operator documentation
└── dist/camc              # generated single-file deployment artifact
```

`camc run --api NAME` resolves one provider `base_url`, reads the provider
token, verifies the profile advertises the adapter's native endpoint, and
starts one loopback Camc Proxy owned by that agent. Messages, Responses, and
Chat Completions are forwarded only to the matching native provider endpoint;
there is no hidden Chat fallback.

Build and verify the bundle with:

```bash
PYTHONPATH=src python3 build_camc.py
./dist/camc version
./dist/camc api list --all
```
