"""Skills: task playbooks the agent loads on demand.

A skill is a folder with a SKILL.md (YAML-ish front matter with at least `name` and
`description`, then Markdown instructions) and optional reference files or scripts.
The system prompt carries only a compact index (name + one-line description); the
model calls `skill_view(name)` to read the full playbook when a task matches. This
keeps the prompt small while hundreds of skills stay available.

Sources, highest precedence first (a skill with the same name in an earlier source
hides the later ones):
  1. <workspace>/skills/              — per-project skills
  2. ~/.config/smooth-agent/skills/   — the owner's own skills (skill_manage writes here)
  3. extra dirs from config
  4. bundled core skills              — always on
  5. bundled optional skills          — only when enabled in config

The bundled skills include MIT-licensed third-party material; see
THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .home import agent_home
from .tools import Registry, ToolError

BUNDLED = Path(__file__).parent / "skills"
def user_dir() -> Path:
    return Path(os.environ.get("SMOOTH_AGENT_SKILLS") or agent_home() / "skills").expanduser()


USER_DIR = None  # resolved at SkillLibrary() time, so SMOOTH_AGENT_HOME can change

_OS_ALIASES = {"linux": "linux", "darwin": "macos", "macos": "macos", "mac": "macos", "osx": "macos",
               "win32": "windows", "windows": "windows", "win": "windows"}
CURRENT_OS = _OS_ALIASES.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Front matter between leading '---' lines -> (fields, body).

    Only what Smooth Agent needs is parsed, with no YAML dependency: top-level
    `key: value` scalars, inline lists `[a, b]`, plus `os` / `bins` lists found
    anywhere in the block (some skills keep them in a JSON-style metadata map)."""
    if not text.startswith("---"):
        return {}, text
    end = re.search(r"(?m)^---\s*$", text[3:])
    if not end:
        return {}, text
    block = text[3:3 + end.start()]
    body = text[3 + end.end():].lstrip("\n")
    meta: dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in ("|", "|-", "|+", ">", ">-", ">+") or (not val and key == "description"):
            # block scalar (or value on the following indented lines)
            chunk = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
                chunk.append(lines[i].strip())
                i += 1
            joined = ("\n" if val.startswith("|") else " ").join(c for c in chunk if c)
            if joined:
                meta[key] = joined.strip()
            continue
        if not val:
            continue
        if val.startswith("[") and val.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[key] = val.strip("'\"")
    bins = re.search(r'"bins"\s*:\s*\[([^\]]*)\]', block)
    if bins:
        meta["requires_bins"] = [b.strip().strip("'\"") for b in bins.group(1).split(",") if b.strip()]
    oses = re.search(r'"os"\s*:\s*\[([^\]]*)\]', block)
    if oses:
        meta["os"] = [o.strip().strip("'\"") for o in oses.group(1).split(",") if o.strip()]
    return meta, body


def _fill(text: str, root: Path) -> str:
    """${SKILL_DIR} (and {baseDir}) in a skill mean its own folder: put the real path in."""
    return text.replace("${SKILL_DIR}", str(root)).replace("$SKILL_DIR", str(root)).replace("{baseDir}", str(root))


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # the skill folder
    source: str  # workspace | user | extra | core | optional
    category: str = ""
    platforms: list[str] = field(default_factory=list)
    requires_bins: list[str] = field(default_factory=list)

    @property
    def runs_here(self) -> bool:
        if not self.platforms:
            return True
        return CURRENT_OS in {_OS_ALIASES.get(p.lower(), p.lower()) for p in self.platforms}

    @property
    def missing_bins(self) -> list[str]:
        return [b for b in self.requires_bins if not shutil.which(b)]

    @property
    def editable(self) -> bool:
        return self.source in ("user", "workspace", "extra")


def _load(skill_md: Path, source: str, root: Path) -> Skill | None:
    try:
        meta, _ = parse_front_matter(skill_md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    name = str(meta.get("name") or skill_md.parent.name).strip()
    desc = str(meta.get("description") or "").strip()
    if not name:
        return None
    rel = skill_md.parent.relative_to(root)
    category = rel.parts[0] if len(rel.parts) > 1 else ""
    platforms = meta.get("platforms") or meta.get("os") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    return Skill(name=name, description=desc, path=skill_md.parent, source=source, category=category,
                 platforms=list(platforms), requires_bins=list(meta.get("requires_bins") or []))


class SkillLibrary:
    def __init__(self, *, workspace: Path | None = None, user_dir: Path | None | bool = True,
                 extra_dirs: list[Path] | tuple[Path, ...] = (), bundled: Path | None = BUNDLED,
                 optional: bool = False, enabled: set[str] | frozenset[str] = frozenset()):
        if user_dir is True:
            user_dir = globals()["user_dir"]()
        self.user_dir = Path(user_dir) if user_dir else None
        self.sources: list[tuple[str, Path]] = []
        if workspace:
            self.sources.append(("workspace", Path(workspace) / "skills"))
        if self.user_dir:
            self.sources.append(("user", self.user_dir))
        self.sources += [("extra", Path(d).expanduser()) for d in extra_dirs]
        if bundled:
            self.sources.append(("core", Path(bundled) / "core"))
            self.sources.append(("optional", Path(bundled) / "optional"))
        self.optional = optional
        self.enabled = set(enabled)
        self.reload()

    def reload(self) -> None:
        self.skills: dict[str, Skill] = {}
        self.hidden_optional: dict[str, Skill] = {}
        for source, root in self.sources:
            if not root.is_dir():
                continue
            for md in sorted(root.rglob("SKILL.md")):
                if any(part in ("node_modules", ".git", "__pycache__") for part in md.parts):
                    continue
                s = _load(md, source, root)
                if not s or s.name in self.skills:
                    continue
                if source == "optional" and not (self.optional or s.name in self.enabled):
                    self.hidden_optional.setdefault(s.name, s)
                    continue
                self.skills[s.name] = s

    # ---------- what the model sees ----------

    def available(self) -> list[Skill]:
        return [s for s in self.skills.values() if s.runs_here]

    def index_text(self, max_chars: int = 12_000, desc_chars: int = 110) -> str:
        lines = []
        for s in sorted(self.available(), key=lambda x: (x.category, x.name)):
            d = s.description.replace("\n", " ")
            d = d if len(d) <= desc_chars else d[: desc_chars - 1] + "…"
            need = f" (needs: {', '.join(s.missing_bins)})" if s.missing_bins else ""
            lines.append(f"- {s.name}: {d}{need}")
        out, size = [], 0
        for ln in lines:
            if size + len(ln) + 1 > max_chars:
                out.append(f"- …and {len(lines) - len(out)} more (call skills_list)")
                break
            out.append(ln)
            size += len(ln) + 1
        return "\n".join(out)

    def list(self, category: str | None = None) -> list[Skill]:
        items = self.available()
        if category:
            items = [s for s in items if s.category.lower() == category.lower()]
        return sorted(items, key=lambda x: (x.category, x.name))

    def get(self, name: str) -> Skill:
        s = self.skills.get(name) or self.skills.get(str(name).strip().lower())
        if not s:
            if name in self.hidden_optional:
                raise ToolError(f"skill '{name}' is an optional skill that is not enabled (owner: add it to enable_skills)")
            raise ToolError(f"no skill named '{name}' (call skills_list)")
        return s

    def view(self, name: str, file_path: str | None = None) -> str:
        s = self.get(name)
        root = s.path.resolve()
        if file_path:
            p = (root / file_path).resolve()
            if p != root and root not in p.parents:
                raise ToolError("file_path is outside the skill folder")
            if not p.is_file():
                raise ToolError(f"skill '{name}' has no file '{file_path}'")
            return _fill(p.read_text(encoding="utf-8", errors="replace")[:200_000], root)
        text = _fill((root / "SKILL.md").read_text(encoding="utf-8", errors="replace"), root)
        others = sorted(str(f.relative_to(root)) for f in root.rglob("*") if f.is_file() and f.name != "SKILL.md")
        head = f"[skill {s.name} — {s.source}; folder: {root}]\n"
        if s.missing_bins:
            head += f"[note: not installed on this machine: {', '.join(s.missing_bins)}]\n"
        tail = ("\n\n[files in this skill — read with skill_view(name, file_path)]\n" + "\n".join(others[:200])) if others else ""
        return head + text + tail

    # ---------- the agent maintaining its own skills ----------

    def _user_skill_dir(self, name: str) -> Path:
        if not self.user_dir:
            raise ToolError("no user skills directory configured")
        if not _NAME_OK.match(name):
            raise ToolError("skill name must be lowercase letters, digits, '.', '_' or '-' (max 64)")
        return self.user_dir / name

    def manage(self, action: str, name: str, *, content: str | None = None, old_string: str | None = None,
               new_string: str | None = None, file_path: str | None = None, file_content: str | None = None) -> str:
        name = str(name or "").strip()
        d = self._user_skill_dir(name)
        md = d / "SKILL.md"
        existing = self.skills.get(name)
        if action == "create":
            if not content:
                raise ToolError("content (the full SKILL.md) is required")
            if md.exists():
                raise ToolError(f"user skill '{name}' exists; use action=edit or patch")
            meta, _ = parse_front_matter(content)
            if not meta.get("description"):
                raise ToolError("SKILL.md needs front matter with name and description, e.g.\n---\nname: my-skill\ndescription: what it does and when to use it\n---")
            d.mkdir(parents=True, exist_ok=True)
            md.write_text(content if meta.get("name") == name else re.sub(r"(?m)^name:.*$", f"name: {name}", content, count=1), encoding="utf-8")
            note = f" (overrides the {existing.source} skill with the same name)" if existing and not existing.editable else ""
            self.reload()
            return f"created skill '{name}'{note}"
        if action in ("edit", "patch") and not md.exists():
            if existing and not existing.editable:
                # first edit of a bundled skill: copy it to the user dir, the copy then overrides it
                shutil.copytree(existing.path, d, dirs_exist_ok=True)
            else:
                raise ToolError(f"no user skill '{name}'")
        if action == "edit":
            if not content:
                raise ToolError("content (the full new SKILL.md) is required")
            md.write_text(content, encoding="utf-8")
            self.reload()
            return f"rewrote skill '{name}'"
        if action == "patch":
            text = md.read_text(encoding="utf-8")
            if not old_string or text.count(old_string) != 1:
                raise ToolError("old_string must match exactly once in SKILL.md")
            md.write_text(text.replace(old_string, new_string or "", 1), encoding="utf-8")
            self.reload()
            return f"patched skill '{name}'"
        if action == "delete":
            if not d.exists():
                raise ToolError(f"no user skill '{name}' (bundled skills cannot be deleted)")
            shutil.rmtree(d)
            self.reload()
            return f"deleted user skill '{name}'"
        if action in ("write_file", "remove_file"):
            if not md.exists():
                raise ToolError(f"no user skill '{name}'")
            if not file_path:
                raise ToolError("file_path is required")
            p = (d / file_path).resolve()
            if d.resolve() not in p.parents:
                raise ToolError("file_path must stay inside the skill folder")
            if action == "write_file":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(file_content or "", encoding="utf-8")
                return f"wrote {file_path} in skill '{name}'"
            if p.exists():
                p.unlink()
            return f"removed {file_path} from skill '{name}'"
        raise ToolError("action must be create, edit, patch, delete, write_file or remove_file")


SKILLS_PROMPT = (
    "\n\n## Skills\nSkills are tested playbooks for specific tasks. When a request matches one, call "
    "skill_view(name) first and follow it (it may point to reference files: skill_view(name, file_path)). "
    "When you work out a reliable way to do something new or the user corrects a skill, save it with skill_manage "
    "so you get better over time. Available skills:\n"
)


def add_skill_tools(reg: Registry, lib: SkillLibrary) -> None:
    @reg.tool("skills_list", "List available skills (name, category, description), optionally for one category.",
              {"type": "object", "properties": {"category": {"type": "string"}}, "required": []})
    def _skills_list(args: dict[str, Any]) -> str:
        items = lib.list(args.get("category"))
        if not items:
            return "(no skills)"
        return "\n".join(f"{s.category or '-'}\t{s.name}\t{s.description[:120]}" + (f" (needs: {', '.join(s.missing_bins)})" if s.missing_bins else "")
                         for s in items)

    @reg.tool("skill_view", "Read a skill's SKILL.md (or one of its files with file_path) before doing the task it covers.",
              {"type": "object", "properties": {"name": {"type": "string"}, "file_path": {"type": "string"}}, "required": ["name"]})
    def _skill_view(args: dict[str, Any]) -> str:
        return lib.view(str(args["name"]), args.get("file_path"))

    @reg.tool(
        "skill_manage",
        "Create or improve the owner's skills. create/edit take content = a full SKILL.md (front matter with name and "
        "description, then instructions); patch replaces old_string with new_string; write_file/remove_file manage extra "
        "files; delete removes a user skill. Editing a bundled skill makes a user copy that overrides it.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"]},
            "name": {"type": "string"}, "content": {"type": "string"},
            "old_string": {"type": "string"}, "new_string": {"type": "string"},
            "file_path": {"type": "string"}, "file_content": {"type": "string"},
        }, "required": ["action", "name"]},
    )
    def _skill_manage(args: dict[str, Any]) -> str:
        return lib.manage(str(args.get("action")), str(args.get("name")), content=args.get("content"),
                          old_string=args.get("old_string"), new_string=args.get("new_string"),
                          file_path=args.get("file_path"), file_content=args.get("file_content"))


__all__ = ["Skill", "SkillLibrary", "parse_front_matter", "add_skill_tools", "SKILLS_PROMPT", "BUNDLED", "USER_DIR"]
