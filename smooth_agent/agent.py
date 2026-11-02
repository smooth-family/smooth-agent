"""The Smooth Agent loop: model → tool calls → results → model, until done.

Lean by design and multi-router: every model call goes through
`smooth_agent.routers.Router`, so the same loop runs on OpenAI, Anthropic,
DeepSeek, GLM, a local Smooth Router / 9router, or anything else the owner
configured. Tool calling uses the OpenAI `tools` wire, which every provider in
the preset list speaks.

Context compression and long-term memory are added on top of this in later
stages (see ROADMAP.md).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .routers import Router, RouterError
from .tools import Registry, ToolError, parse_arguments, todo_tool

if TYPE_CHECKING:
    from .skills import SkillLibrary

DEFAULT_SYSTEM = (
    "You are Smooth Agent, a capable, self-hosted assistant running on the owner's own machine. "
    "Use your tools to get real results instead of guessing: read and edit files in the workspace, search and read "
    "the web, and (when enabled) run commands and code. For multi-step work keep a todo list and work through it. "
    "Hand independent sub-tasks to delegate_task when that keeps your own context focused. "
    "When a tool returns an error, read it and adjust instead of repeating the same call. "
    "Give clear, direct answers and say plainly what you did, what you found, and what is left."
)

SUBAGENT_SYSTEM = (
    "You are a Smooth Agent sub-agent working on one delegated task. Use your tools to do it fully, then reply with a "
    "concise report: what you did, the result (with the concrete facts, numbers, paths or URLs), and anything left open. "
    "You cannot ask the user questions; make reasonable assumptions and state them."
)


@dataclass
class Turn:
    """The result of one user message."""

    text: str
    steps: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None  # set if the loop hit a limit instead of finishing


class Agent:
    def __init__(self, router: Router, registry: Registry | None = None, *, system: str = DEFAULT_SYSTEM,
                 max_steps: int = 12, max_tokens: int = 1536, provider: str | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 skills: SkillLibrary | None = None, delegate: bool = False, delegate_steps: int = 10,
                 processes: Any = None):
        self.router = router
        self.registry = registry
        self.system = system
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.provider = provider
        self.on_event = on_event or (lambda _e, _d: None)
        self.skills = skills
        self.processes = processes  # ProcessManager, for "your background command finished" notices
        if skills is not None and registry is not None and not registry.get("skill_view"):
            from .skills import add_skill_tools
            add_skill_tools(registry, skills)
        if delegate and registry is not None:
            add_delegate_tool(registry, self._subagent_factory(delegate_steps))
        self.history: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]

    def _system_prompt(self) -> str:
        if self.skills is None:
            return self.system
        from .skills import SKILLS_PROMPT
        return self.system + SKILLS_PROMPT + self.skills.index_text()

    def _subagent_factory(self, steps: int) -> Callable[[], "Agent"]:
        def make() -> Agent:
            sub_reg = self.registry.copy(without=("delegate_task", "todo")) if self.registry else None
            if sub_reg is not None and self.registry is not None and self.registry.get("todo"):
                sub_reg.add(todo_tool())  # each sub-agent gets its own todo list, not the parent's
            return Agent(self.router, sub_reg, system=SUBAGENT_SYSTEM, max_steps=steps, max_tokens=self.max_tokens,
                         provider=self.provider, on_event=lambda e, d: self.on_event(f"sub:{e}", d))
        return make

    def _deliver_process_notices(self) -> None:
        if self.processes is None:
            return
        for note in self.processes.take_finished():
            self.on_event("process_done", {"text": note})
            self.history.append({"role": "user", "content": f"[automatic notice, not from the user]\n{note}"})

    def reset(self) -> None:
        self.history = [{"role": "system", "content": self._system_prompt()}]

    def run(self, user_message: str) -> Turn:
        # refresh the system prompt: skills created or edited last turn show up in the index
        if self.history and self.history[0].get("role") == "system":
            self.history[0] = {"role": "system", "content": self._system_prompt()}
        self.history.append({"role": "user", "content": user_message})
        tools = self.registry.schemas() if self.registry else None
        extra = {"tools": tools, "tool_choice": "auto"} if tools else {}
        used: list[dict[str, Any]] = []
        usage_total: dict[str, int] = {}

        for step in range(1, self.max_steps + 1):
            self._deliver_process_notices()
            try:
                c = self.router.complete(self.history, provider=self.provider,
                                         max_tokens=self.max_tokens, extra=dict(extra))
            except RouterError as e:
                self.on_event("error", {"message": str(e)})
                return Turn(text=f"(model error: {e})", steps=step, tool_calls=used, usage=usage_total, stopped="error")

            for k, v in (c.usage or {}).items():
                if isinstance(v, int):
                    usage_total[k] = usage_total.get(k, 0) + v

            if not c.tool_calls:
                # a plain answer ends the turn
                self.history.append(c.message or {"role": "assistant", "content": c.text})
                if c.text:
                    self.on_event("text", {"text": c.text})
                return Turn(text=c.text, steps=step, tool_calls=used, usage=usage_total)

            # record the assistant's tool-call message, then run each call
            self.history.append(c.message or {"role": "assistant", "content": c.text or None,
                                              "tool_calls": c.tool_calls})
            for call in c.tool_calls:
                fn = (call.get("function") or {})
                name = fn.get("name", "")
                args = parse_arguments(fn.get("arguments"))
                self.on_event("tool", {"name": name, "arguments": args})
                result = self.registry.run(name, args) if self.registry else "error: no tools available"
                used.append({"name": name, "arguments": args})
                self.on_event("tool_result", {"name": name, "result": result})
                self.history.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{step}_{name}",
                    "content": result if isinstance(result, str) else json.dumps(result),
                })

        # out of steps: ask once more without tools for a closing answer
        self.history.append({"role": "system", "content": "Stop using tools and give your best final answer now."})
        try:
            c = self.router.complete(self.history, provider=self.provider, max_tokens=self.max_tokens)
            self.history.append(c.message or {"role": "assistant", "content": c.text})
            return Turn(text=c.text, steps=self.max_steps, tool_calls=used, usage=usage_total, stopped="max_steps")
        except RouterError as e:
            return Turn(text=f"(stopped after {self.max_steps} steps; {e})", steps=self.max_steps,
                        tool_calls=used, usage=usage_total, stopped="max_steps")


def add_delegate_tool(registry: Registry, make_subagent: Callable[[], Agent], *, max_parallel: int = 3,
                      max_tasks: int = 6) -> None:
    @registry.tool(
        "delegate_task",
        "Hand work to sub-agents that have your tools (except delegation) and their own fresh context. Give one goal, "
        f"or a list of up to {max_tasks} independent tasks that run in parallel. Put everything a sub-agent needs in "
        "context (it cannot see this conversation). Returns each sub-agent's report.",
        {"type": "object", "properties": {
            "goal": {"type": "string", "description": "the task, for a single sub-agent"},
            "context": {"type": "string", "description": "facts, files, constraints the sub-agent needs"},
            "tasks": {"type": "array", "maxItems": max_tasks, "items": {"type": "object", "properties": {
                "goal": {"type": "string"}, "context": {"type": "string"}}, "required": ["goal"]}},
        }, "required": []},
    )
    def _delegate(args: dict[str, Any]) -> str:
        tasks = [t for t in (args.get("tasks") or []) if isinstance(t, dict) and t.get("goal")]
        if not tasks and args.get("goal"):
            tasks = [{"goal": args["goal"], "context": args.get("context", "")}]
        if not tasks:
            raise ToolError("give a goal or a list of tasks")
        tasks = tasks[:max_tasks]
        shared = str(args.get("context") or "") if args.get("tasks") else ""

        def one(t: dict[str, Any]) -> str:
            prompt = t["goal"]
            ctx = "\n\n".join(c for c in (shared, str(t.get("context") or "")) if c)
            if ctx:
                prompt += f"\n\nContext:\n{ctx}"
            turn = make_subagent().run(prompt)
            tag = f" (stopped: {turn.stopped})" if turn.stopped else ""
            return f"{turn.text}{tag}"

        if len(tasks) == 1:
            return one(tasks[0])
        with ThreadPoolExecutor(max_workers=min(max_parallel, len(tasks))) as pool:
            results = list(pool.map(one, tasks))
        return "\n\n".join(f"### Task {i}: {t['goal'][:120]}\n{r}" for i, (t, r) in enumerate(zip(tasks, results), 1))


__all__ = ["Agent", "Turn", "DEFAULT_SYSTEM", "SUBAGENT_SYSTEM", "add_delegate_tool"]
