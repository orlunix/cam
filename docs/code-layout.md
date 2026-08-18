# Code layout

CAMC is a stdlib-only package that is concatenated into `dist/camc` by
`build_camc.py`. The API path is direct-only: provider profiles live in
`api_store.py`, endpoint selection in `api_routing.py`, and tool-specific URL
shape in `src/cam/adapters/configs/*.toml`.

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
passes the resulting URL/model directly to Claude or Codex. There is no
production proxy package, proxy worker, or protocol-conversion test suite.

Build and verify the bundle with:

```bash
PYTHONPATH=src python3 build_camc.py
./dist/camc version
./dist/camc api list --all
```
