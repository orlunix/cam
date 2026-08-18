# Scripts

**Development and reference utilities.** Not embedded in `dist/camc` unless
explicitly copied by an operator.

| Path | Purpose |
|------|---------|
| `ihub_ping_curated.py` | Direct IHUB ping for enabled curated APIs |
| `release.sh`, `cam-serve-daemon.sh`, … | Release / ops helpers |

API routing is implemented by **`src/camc_pkg/api_*.py`** and embedded with
`python3 build_camc.py`. There is no production proxy. See
`docs/code-layout.md`.
