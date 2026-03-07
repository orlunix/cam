# skillm — Per-Project AI Agent Skill Manager

Manage AI agent skills at the project level, not globally.

```bash
skillm init                        # Create skills.json + .skills/
skillm add joeseesun/defuddle      # Fetch & install a skill
skillm remove defuddle             # Remove a skill
skillm disable defuddle            # Keep files, hide from agent
skillm enable defuddle             # Re-enable
skillm list                        # Show all skills + status
skillm search "youtube"            # Search registries
skillm update [name]               # Update to latest
skillm sync                        # Install all from skills.json
skillm inject                      # Regenerate CLAUDE.md skills section
```

## Install

```bash
cd skillm && pip install -e .
```

## How It Works

```
your-project/
├── skills.json          # Declarative skill dependencies
├── .skills/             # Installed skills (gitignore this)
│   ├── defuddle/
│   │   └── SKILL.md
│   └── .registry.json   # Local state
└── CLAUDE.md            # Auto-injected skills section
```

Skills are fetched from GitHub repos. Compatible with `npx skills add` ecosystem (same repo format).
