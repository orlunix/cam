# Scripts

**Development and reference utilities.** Not embedded in `dist/camc` unless
explicitly copied by an operator.

| Path | Purpose |
|------|---------|
| `ihub_ping_curated.py` | Direct IHUB ping for enabled curated APIs |
| `release.sh`, `cam-serve-daemon.sh`, … | Release / ops helpers |

IHUB proxy dev scripts moved to **`dev/ihub_proxy/`**. Production proxy:
**`src/camc_pkg/proxy/`** → `python3 build_camc.py`. See `docs/code-layout.md`.
