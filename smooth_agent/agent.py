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
import time
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


NOTICE = "[automatic notice, not from the user]\n"
COMPRESS_PROMPT = (
    "Summarise the conversation below so the assistant can continue the work without it. Keep: the owner's goals and "
    "instructions, decisions and their reasons, facts, numbers, names, file paths, URLs, commands that worked, what was "
    "done, and what is still open. Drop small talk and raw tool output. Write compact bullet points in the language of "
    "the conversation. Return only the summary."
)


class Agent:
    def __init__(self, router: Router, registry: Registry | None = None, *, system: str = DEFAULT_SYSTEM,
                 max_steps: int = 12, max_tokens: int = 1536, provider: str | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 skills: SkillLibrary | None = None, delegate: bool = False, delegate_steps: int = 10,
                 processes: Any = None, store: Any = None, memory: Any = None, session_id: str | None = None,
                 context_tokens: int = 64_000, keep_recent: int = 10, model: str | None = None):
        self.router = router
        self.registry = registry
        self.system = system
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.provider = provider
        self.model = model or None  # override the provider's default model (e.g. for one cron job)
        self.on_event = on_event or (lambda _e, _d: None)
        self.skills = skills
        self.processes = processes  # ProcessManager, for "your background command finished" notices
        self.store = store          # Store: every message of the session is saved there
        self.memory = memory        # Memory: recalled into the system prompt each turn
        self.session_id = session_id
        self.context_tokens = context_tokens
        self.keep_recent = keep_recent
        self._recall = ""
        if skills is not None and registry is not None and not registry.get("skill_view"):
            from .skills import add_skill_tools
            add_skill_tools(registry, skills)
        if delegate and registry is not None:
            add_delegate_tool(registry, self._subagent_factory(delegate_steps))
        self.history: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
        if store is not None and session_id:
            store.new_session(session_id)

    # ---------- prompt ----------

    def _system_prompt(self) -> str:
        parts = [self.system]
        if self.memory is not None:
            from .memory import MEMORY_PROMPT
            parts.append(MEMORY_PROMPT)
            if self._recall:
                parts.append("\n\nRemembered:\n" + self._recall)
        if self.skills is not None:
            from .skills import SKILLS_PROMPT
            parts.append(SKILLS_PROMPT + self.skills.index_text())
        return "".join(parts)

    def _subagent_factory(self, steps: int) -> Callable[[], "Agent"]:
        def make() -> Agent:
            sub_reg = self.registry.copy(without=("delegate_task", "todo", "memory", "clarify")) if self.registry else None
            if sub_reg is not None and self.registry is not None and self.registry.get("todo"):
                sub_reg.add(todo_tool())  # each sub-agent gets its own todo list, not the parent's
            return Agent(self.router, sub_reg, system=SUBAGENT_SYSTEM, max_steps=steps, max_tokens=self.max_tokens,
                         provider=self.provider, on_event=lambda e, d: self.on_event(f"sub:{e}", d),
                         context_tokens=self.context_tokens)
        return make

    # ---------- history ----------

    def _push(self, msg: dict[str, Any]) -> None:
        self.history.append(msg)
        if self.store is not None and self.session_id:
            try:
                self.store.add_message(self.session_id, msg)
            except Exception as e:  # saving must never break the turn
                self.on_event("error", {"message": f"could not save the message: {e}"})

    def resume(self, session_id: str) -> int:
        """Continue a saved session: load its messages (the system prompt is rebuilt)."""
        if self.store is None:
            raise ValueError("no store configured")
        msgs = [m for m in self.store.session_messages(session_id) if m.get("role") != "system"]
        self.session_id = session_id
        self.history = [{"role": "system", "content": self._system_prompt()}] + msgs
        return len(msgs)

    def _deliver_process_notices(self) -> None:
        if self.processes is None:
            return
        for note in self.processes.take_finished():
            self.on_event("process_done", {"text": note})
            self._push({"role": "user", "content": NOTICE + note})

    def reset(self) -> None:
        self.history = [{"role": "system", "content": self._system_prompt()}]
        if self.store is not None and self.session_id:
            import uuid
            self.session_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            self.store.new_session(self.session_id)

    # ---------- context compression ----------

    @staticmethod
    def _size(msgs: list[dict[str, Any]]) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs) // 4  # ~4 characters per token

    def _maybe_compress(self) -> None:
        if self.context_tokens <= 0 or self._size(self.history) < self.context_tokens * 0.8:
            return
        body = self.history[1:]
        if len(body) <= self.keep_recent + 2:
            return
        cut = len(body) - self.keep_recent
        # never split an assistant tool call from its results: start the kept part at a user message
        while cut > 0 and body[cut].get("role") != "user":
            cut -= 1
        if cut <= 1:
            return
        old, recent = body[:cut], body[cut:]
        transcript = "\n".join(f"{m.get('role')}: {str(m.get('content') or '')[:3000]}"
                               + "".join(f" [called {c.get('function', {}).get('name')}]" for c in m.get("tool_calls") or [])
                               for m in old)
        try:
            c = self.router.complete([{"role": "system", "content": COMPRESS_PROMPT}, {"role": "user", "content": transcript[-120_000:]}],
                                     provider=self.provider, max_tokens=1500)
            summary = c.text.strip()
        except RouterError as e:
            self.on_event("error", {"message": f"context compression failed: {e}"})
            return
        if not summary:
            return
        self.history = [self.history[0], {"role": "user", "content": NOTICE + "Summary of the earlier part of this conversation:\n" + summary}] + recent
        self.on_event("compressed", {"messages": len(old), "tokens": self._size(self.history)})

    # ---------- the loop ----------

    def run(self, user_message: str) -> Turn:
        if self.memory is not None:
            try:
                self._recall = self.memory.recall(user_message)
            except Exception:
                self._recall = ""
        # refresh the system prompt: memories and skills changed last turn show up now
        if self.history and self.history[0].get("role") == "system":
            self.history[0] = {"role": "system", "content": self._system_prompt()}
        self._push({"role": "user", "content": user_message})
        tools = self.registry.schemas() if self.registry else None
        extra = {"tools": tools, "tool_choice": "auto"} if tools else {}
        used: list[dict[str, Any]] = []
        usage_total: dict[str, int] = {}

        for step in range(1, self.max_steps + 1):
            self._deliver_process_notices()
            self._maybe_compress()
            try:
                c = self.router.complete(self.history, provider=self.provider, model=self.model,
                                         max_tokens=self.max_tokens, extra=dict(extra))
            except RouterError as e:
                self.on_event("error", {"message": str(e)})
                return Turn(text=f"(model error: {e})", steps=step, tool_calls=used, usage=usage_total, stopped="error")

            for k, v in (c.usage or {}).items():
                if isinstance(v, int):
                    usage_total[k] = usage_total.get(k, 0) + v

            if not c.tool_calls:
                # a plain answer ends the turn
                self._push(c.message or {"role": "assistant", "content": c.text})
                if c.text:
                    self.on_event("text", {"text": c.text})
                return Turn(text=c.text, steps=step, tool_calls=used, usage=usage_total)

            # record the assistant's tool-call message, then run each call
            self._push(c.message or {"role": "assistant", "content": c.text or None, "tool_calls": c.tool_calls})
            for call in c.tool_calls:
                fn = (call.get("function") or {})
                name = fn.get("name", "")
                args = parse_arguments(fn.get("arguments"))
                self.on_event("tool", {"name": name, "arguments": args})
                result = self.registry.run(name, args) if self.registry else "error: no tools available"
                used.append({"name": name, "arguments": args})
                self.on_event("tool_result", {"name": name, "result": result})
                self._push({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{step}_{name}",
                    "content": result if isinstance(result, str) else json.dumps(result),
                })

        # out of steps: ask once more without tools for a closing answer
        self._push({"role": "user", "content": NOTICE + "You are out of tool steps for this message. Stop using tools and "
                    "give your best final answer now: what is done, what you found, and what is left."})
        try:
            c = self.router.complete(self.history, provider=self.provider, model=self.model, max_tokens=self.max_tokens)
            self._push(c.message or {"role": "assistant", "content": c.text})
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
