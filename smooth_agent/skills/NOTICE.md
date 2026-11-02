# Bundled skills

The bundled skills include MIT-licensed material from third parties. Their
copyright and license notices are in THIRD_PARTY_NOTICES.md, which ships with
Smooth Agent. Smooth's own changes are MIT © Smooth.

- `core/` skills are available by default.
- `optional/` skills load only when `optional_skills = true` (or listed by name
  in `enable_skills`) in smooth-agent.toml.
- Your own skills go in `~/.smooth-agent/skills/` or `<workspace>/skills/` and
  override a bundled skill with the same name.
- Paths written as `${SKILL_DIR}` mean the skill's own folder; `skill_view`
  fills in the real path.
