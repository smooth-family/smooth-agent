"""Named workflows: saved, reusable, parameterised playbooks.

A workflow is a named set of steps the owner writes once and runs on demand or
on a schedule. Steps are plain instructions (a numbered list reads best) and may
contain `{{placeholders}}` filled in from parameters at run time, so one
workflow — "research", "weekly-report", "triage-inbox" — serves many inputs.

Storage: the `workflows` table in the one SQLite database (see store.py). Running
one is deliberately the same path as a scheduled run: `workflow` (the tool) and
`smooth-agent workflow run` render the playbook into a single self-contained
instruction and hand it to a fresh agent — through the job queue when a daemon is
up, or inline otherwise. A cron job can target a workflow by name (see
scheduler.py), which is how "run this workflow every morning" works.

Stdlib only.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .store import Store
from .tools import Registry, ToolError

NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
MAX_STEPS_CHARS = 20_000


def _clean_params(params: Any) -> list[dict]:
    """Normalise a parameter spec into a list of {name, description, default, required}."""
    if not params:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for p in params:
        if isinstance(p, str):
            p = {"name": p}
        if not isinstance(p, dict):
            raise ToolError("each parameter is a name or an object {name, description, default, required}")
        name = str(p.get("name") or "").strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            raise ToolError(f"parameter name '{name}': letters, digits and '_' only, not starting with a digit")
        if name in seen:
            raise ToolError(f"duplicate parameter '{name}'")
        seen.add(name)
        has_default = "default" in p and p["default"] is not None
        out.append({
            "name": name,
            "description": str(p.get("description") or ""),
            "default": str(p["default"]) if has_default else None,
            # a parameter with a default is optional unless explicitly marked required
            "required": bool(p.get("required", not has_default)),
        })
    return out


class Workflows:
    """CRUD over the workflows table, plus render()."""

    def __init__(self, store: Store):
        self.s = store

    def create(self, name: str, steps: str, *, description: str = "", params: Any = None,
               skills: list[str] | None = None, model: str = "") -> dict:
        name = str(name or "").strip()
        if not NAME_OK.match(name):
            raise ToolError("name: letters, digits, '.', '_' or '-' (max 64), starting with a letter or digit")
        steps = str(steps or "").strip()
        if not steps:
            raise ToolError("give the steps: what the agent should do (a numbered list reads best)")
        if len(steps) > MAX_STEPS_CHARS:
            raise ToolError(f"steps are too long ({len(steps)} chars, max {MAX_STEPS_CHARS})")
        spec = _clean_params(params)
        self._check_placeholders(steps, spec)
        if self.s.one("SELECT 1 FROM workflows WHERE name=?", (name,)):
            raise ToolError(f"a workflow named '{name}' exists; update or remove it first")
        now = time.time()
        with self.s.tx() as c:
            c.execute("INSERT INTO workflows (name, description, steps, params, skills, model, runs, created, updated) "
                      "VALUES (?,?,?,?,?,?,0,?,?)",
                      (name, description or "", steps, json.dumps(spec), json.dumps(skills or []), model or "", now, now))
        return self.get(name)

    def get(self, name: str) -> dict:
        r = self.s.one("SELECT * FROM workflows WHERE name=?", (name,))
        if not r:
            raise ToolError(f"no workflow named '{name}'")
        return dict(r)

    def list(self) -> list[dict]:
        return [dict(r) for r in self.s.q("SELECT * FROM workflows ORDER BY name")]

    def update(self, name: str, **fields: Any) -> dict:
        cur = self.get(name)
        steps = fields["steps"] if fields.get("steps") is not None else cur["steps"]
        if fields.get("steps") is not None:
            steps = str(fields["steps"]).strip()
            if not steps:
                raise ToolError("steps cannot be empty")
            if len(steps) > MAX_STEPS_CHARS:
                raise ToolError(f"steps are too long ({len(steps)} chars, max {MAX_STEPS_CHARS})")
        spec = _clean_params(fields["params"]) if fields.get("params") is not None else json.loads(cur["params"] or "[]")
        self._check_placeholders(steps, spec)
        sets, args = [], []
        if fields.get("steps") is not None:
            sets.append("steps=?"); args.append(steps)
        if fields.get("params") is not None:
            sets.append("params=?"); args.append(json.dumps(spec))
        if fields.get("description") is not None:
            sets.append("description=?"); args.append(str(fields["description"]))
        if fields.get("model") is not None:
            sets.append("model=?"); args.append(str(fields["model"]))
        if fields.get("skills") is not None:
            sets.append("skills=?"); args.append(json.dumps(list(fields["skills"])))
        if sets:
            sets.append("updated=?"); args.append(time.time())
            with self.s.tx() as c:
                c.execute(f"UPDATE workflows SET {', '.join(sets)} WHERE name=?", (*args, name))
        return self.get(name)

    def remove(self, name: str) -> bool:
        with self.s.tx() as c:
            return c.execute("DELETE FROM workflows WHERE name=?", (name,)).rowcount > 0

    def mark_run(self, name: str) -> None:
        with self.s.tx() as c:
            c.execute("UPDATE workflows SET runs=runs+1 WHERE name=?", (name,))

    @staticmethod
    def _check_placeholders(steps: str, spec: list[dict]) -> None:
        """Every {{placeholder}} in the steps must be a declared parameter."""
        declared = {p["name"] for p in spec}
        used = set(PLACEHOLDER.findall(steps))
        unknown = sorted(used - declared)
        if unknown:
            raise ToolError("the steps use undeclared parameter(s): " + ", ".join("{{" + u + "}}" for u in unknown)
                            + " — add them to params or remove the placeholder")

    def resolve_params(self, wf: dict, given: dict | None) -> dict[str, str]:
        """Merge supplied values with declared defaults; error on anything required but missing."""
        spec = json.loads(wf["params"] or "[]")
        given = {str(k): v for k, v in (given or {}).items()}
        values: dict[str, str] = {}
        missing: list[str] = []
        for p in spec:
            name = p["name"]
            if name in given and given[name] is not None and str(given[name]) != "":
                values[name] = str(given[name])
            elif p["default"] is not None:
                values[name] = p["default"]
            elif p["required"]:
                missing.append(name)
        if missing:
            raise ToolError(f"workflow '{wf['name']}' needs: " + ", ".join(missing))
        extra = sorted(set(given) - {p["name"] for p in spec})
        if extra:
            raise ToolError("unknown parameter(s): " + ", ".join(extra))
        return values

    def render(self, name: str, given: dict | None = None) -> str:
        """Turn a workflow into one self-contained instruction for a fresh agent run."""
        wf = self.get(name)
        values = self.resolve_params(wf, given)
        steps = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), wf["steps"])
        skills = json.loads(wf["skills"] or "[]")
        parts = [f'Run the saved workflow "{name}".']
        if wf["description"]:
            parts.append(wf["description"])
        if values:
            parts.append("Parameters:\n" + "\n".join(f"- {k}: {v}" for k, v in values.items()))
        if skills:
            parts.append(f"Before you start, load and follow these skills with skill_view: {', '.join(skills)}.")
        parts.append("Steps:\n" + steps)
        parts.append("Carry out every step in order, then reply with the result the owner should read.")
        return "\n\n".join(parts)


def _fmt(wf: dict) -> str:
    spec = json.loads(wf["params"] or "[]")
    ps = ", ".join(p["name"] + ("" if p["required"] else "?") for p in spec) or "none"
    return f"{wf['name']}\tparams: {ps}\truns {wf['runs']}\t{(wf['description'] or wf['steps'])[:60]}"


def add_workflow_tool(reg: Registry, workflows: Workflows, enqueue: Any = None) -> None:
    """The `workflow` tool. `enqueue(name, params) -> job_id` runs one through the
    daemon; without it, `run` returns the rendered instruction for the agent to
    carry out itself."""

    @reg.tool(
        "workflow",
        "Saved, reusable playbooks the owner can run by name or on a schedule. A workflow has steps (plain "
        "instructions; a numbered list reads best) that may contain {{placeholders}} filled from parameters at run "
        "time. Actions: create (name, steps, optional description, params, skills, model), list, show (name), update "
        "(name + any field), remove (name), run (name + params). Declare every {{placeholder}} in params. "
        "params is a list of names or objects {name, description, default, required}; a param with a default is "
        "optional. To schedule a workflow, create a cronjob with its `workflow` field set to the name.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "show", "update", "remove", "run"]},
            "name": {"type": "string"},
            "steps": {"type": "string", "description": "what to do, in order"},
            "description": {"type": "string"},
            "params": {"type": "array", "items": {"type": ["string", "object"]},
                       "description": "names, or objects {name, description, default, required}"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "model": {"type": "string"},
            "args": {"type": "object", "description": "parameter values for run, e.g. {\"topic\": \"...\"}"},
        }, "required": ["action"]},
    )
    def _workflow(a: dict[str, Any]) -> str:
        action = a.get("action")
        name = str(a.get("name") or "")
        if action == "list":
            rows = workflows.list()
            return "\n".join(_fmt(w) for w in rows) if rows else "(no workflows)"
        if action == "create":
            w = workflows.create(name, a.get("steps") or "", description=a.get("description") or "",
                                  params=a.get("params"), skills=a.get("skills"), model=a.get("model") or "")
            return f"created workflow '{w['name']}'"
        if action == "show":
            w = workflows.get(name)
            spec = json.loads(w["params"] or "[]")
            lines = [f"workflow: {w['name']}"]
            if w["description"]:
                lines.append(w["description"])
            if spec:
                lines.append("params:")
                for p in spec:
                    d = f" (default: {p['default']})" if p["default"] is not None else (" (required)" if p["required"] else " (optional)")
                    lines.append(f"  - {p['name']}{d}" + (f" — {p['description']}" if p["description"] else ""))
            if json.loads(w["skills"] or "[]"):
                lines.append("skills: " + ", ".join(json.loads(w["skills"])))
            if w["model"]:
                lines.append(f"model: {w['model']}")
            lines.append(f"runs: {w['runs']}")
            lines.append("steps:\n" + w["steps"])
            return "\n".join(lines)
        if action == "update":
            workflows.update(name, steps=a.get("steps"), description=a.get("description"), params=a.get("params"),
                             skills=a.get("skills"), model=a.get("model"))
            return f"updated workflow '{name}'"
        if action == "remove":
            return f"removed workflow '{name}'" if workflows.remove(name) else f"error: no workflow named '{name}'"
        if action == "run":
            prompt = workflows.render(name, a.get("args"))  # validates params; raises ToolError if any are missing
            if enqueue is not None:
                jid = enqueue(name, a.get("args") or {})
                return (f"queued workflow '{name}' to run now (job {jid}). It runs in its own agent session; "
                        "check `smooth-agent jobs` for the result, or it is delivered if the workflow sets a channel.")
            # No queue wired (library use): hand the steps back so the caller carries them out.
            return "Run these steps now and report the result:\n\n" + prompt
        raise ToolError("unknown action")


__all__ = ["Workflows", "add_workflow_tool", "NAME_OK"]
