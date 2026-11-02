# Optional Skills

Optional skills that are **not activated by default**.

These skills ship with the smooth-agent repository but are not copied to
`~/.smooth-agent/skills/` during setup. They are discoverable via the Skills Hub:

```bash
smooth-agent skills list --all            # every skill, optional ones included
smooth-agent skills search <words>       # also finds optional skills
smooth-agent skills install <name>       # copies an optional skill to ~/.smooth-agent/skills/ and activates it
```

## Why optional?

Some skills are useful but not broadly needed by every user:

- **Niche integrations** — specific paid services, specialized tools
- **Experimental features** — promising but not yet proven
- **Heavyweight dependencies** — require significant setup (API keys, installs)

By keeping them optional, we keep the default skill set lean while still
providing curated, tested, official skills for users who want them.
