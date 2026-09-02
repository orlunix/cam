# CAM Desktop Extensions

Extensions are the unified tool surface of CAM Desktop: a folder with a
`manifest.yaml`, an optional `index.html` view, and an optional
`main.py` remote tool (python3.6 stdlib).

- **SPEC.md** — the architecture contract (format, bridge, install, MAS stance)
- **GUIDE.md** — how to design and build an extension
- **examples/agent-doctor** — demo extension (installable tar.gz lives in `dist/ext/`)
- **packages/** — built-in extensions shipped with the app (skills / todos / assistant; disable-only, never removable)
- **host/** — hub-side runtime (registry + tool proxy)

Renderer-side pieces live under `web/js/` (the hub serves pages from
the web root — see SPEC §0).
