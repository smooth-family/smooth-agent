"""Persona files: plain Markdown the owner (or the agent) keeps next to the
workspace, loaded into the system prompt on every message.

    SOUL.md      who the agent is: character, tone, values
    IDENTITY.md  facts about itself: name, owner, where it runs
    USER.md      about the owner: name, timezone, work, preferences
    AGENTS.md    how to work: rules and habits for tasks
    TOOLS.md     notes about tools, accounts and quirks on this machine
    MEMORY.md    hand-kept notes (long-term memory proper is the memory tool)

Looked up in the workspace first (the agent can edit those with its file
tools), then in $SMOOTH_AGENT_HOME. Each file is capped, and so is the total,
so a runaway file cannot crowd out the conversation.
"""
from __future__ import annotations

from pathlib import Path

PERSONA_FILES = ("SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md")
PER_FILE = 6_000
TOTAL = 20_000

PERSONA_PROMPT = (
    "\n\nPersona files (Markdown kept by the owner and you; they are part of your instructions, follow them). "
    "To change one, edit the file in the workspace with your file tools; the change applies from the next message."
)


TEMPLATES = {
    "SOUL.md": "who you are: character, tone, values, how you talk",
    "IDENTITY.md": "facts about you: name, what you are, who runs you, where you run",
    "USER.md": "about the owner: name, timezone, language, work, preferences",
    "AGENTS.md": "how you work: rules and habits for tasks",
    "TOOLS.md": "notes about tools, accounts and quirks on this machine",
    "MEMORY.md": "hand-kept notes (long-term memory proper is the memory tool)",
}


def template(name: str) -> str:
    return f"# {name[:-3]}\n\n<!-- {TEMPLATES[name]}. Write below; while this file has no text of its own it is not used. -->\n"


def ensure_persona(directory: Path) -> list[str]:
    """Create the empty persona templates that are missing. Returns the names created."""
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for name in PERSONA_FILES:
        p = directory / name
        if not p.exists():
            p.write_text(template(name), encoding="utf-8")
            made.append(name)
    return made


def meaningful(body: str) -> str:
    """The text of a persona file without hint comments; empty when only the heading is left."""
    import re
    text = re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1 and (not lines or lines[0].lstrip().startswith("#")):
        return ""
    return text


def find_persona(dirs: list[Path]) -> list[tuple[str, Path]]:
    """[(name, path)] for each persona file, first directory that has it wins."""
    found = []
    for name in PERSONA_FILES:
        for d in dirs:
            p = Path(d) / name
            if p.is_file():
                found.append((name, p))
                break
    return found


def _first_meaningful(name: str, dirs: list[Path]) -> str:
    """The first copy of a persona file that has text of its own (an empty template does not hide a later one)."""
    for d in dirs:
        p = Path(d) / name
        if p.is_file():
            try:
                body = meaningful(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if body:
                return body
    return ""


def persona_text(dirs: list[Path]) -> str:
    parts, used = [], 0
    for name in PERSONA_FILES:
        body = _first_meaningful(name, dirs)
        if not body:
            continue
        if len(body) > PER_FILE:
            body = body[:PER_FILE] + f"\n[… {name} cut at {PER_FILE} characters]"
        if used + len(body) > TOTAL:
            parts.append(f"## {name}\n[skipped: persona files are over {TOTAL} characters in total]")
            break
        used += len(body)
        parts.append(f"## {name}\n{body}")
    return (PERSONA_PROMPT + "\n\n" + "\n\n".join(parts)) if parts else ""


__all__ = ["PERSONA_FILES", "TEMPLATES", "template", "ensure_persona", "meaningful", "find_persona", "persona_text"]
