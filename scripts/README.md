# Scripts

**Development and reference utilities.** Not embedded in `dist/camc` unless
explicitly copied by an operator.

| Path | Purpose |
|------|---------|
| `ihub_ping_curated.py` | Direct IHUB ping for enabled curated APIs |
| `camc_live_api_regression.py` | Explicit real-agent lifecycle regression; defaults to the internal `kimi-k3` API profile |
| `release.sh`, `cam-serve-daemon.sh`, … | Release / ops helpers |

API routing is implemented by **`src/camc_pkg/api_*.py`** and embedded with
`python3 build_camc.py`. There is no production proxy. See
`docs/code-layout.md`.

Run the real API release regression explicitly (it is not part of normal
`pytest`):

```bash
python3 scripts/camc_live_api_regression.py --camc /path/to/candidate/camc
```

The default API profile is `kimi-k3`. Override it only when needed with
`--api PROFILE` or `CAMC_LIVE_API=PROFILE`.
