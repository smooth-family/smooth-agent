"""Tools that talk to the owner or look back: clarify and session_search."""
from __future__ import annotations

import time
from typing import Any, Callable

from .store import Store
from .tools import Registry, ToolError

# ask(question, choices) -> the owner's answer, or None when nobody can answer right now
AskFn = Callable[[str, list[str]], "str | None"]


def add_clarify_tool(reg: Registry, ask: AskFn | None) -> None:
    @reg.tool(
        "clarify",
        "Ask the owner a question when the request is ambiguous and a wrong guess would waste work or do harm. Offer "
        "choices when there are a few clear options. Do not use it for things you can find out yourself.",
        {"type": "object", "properties": {
            "question": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        }, "required": ["question"]},
    )
    def _clarify(args: dict[str, Any]) -> str:
        q = str(args.get("question") or "").strip()
        if not q:
            raise ToolError("question is empty")
        choices = [str(c) for c in (args.get("choices") or [])][:6]
        answer = ask(q, choices) if ask else None
        if answer is None or not str(answer).strip():
            return ("No one is available to answer right now. Continue with the most reasonable assumption, "
                    "say clearly which assumption you made, and offer to change it.")
        return f"The owner answered: {answer}"


def add_session_tools(reg: Registry, store: Store, current: Callable[[], str | None]) -> None:
    @reg.tool(
        "session_search",
        "Search past conversations with the owner (all sessions, including scheduled runs) by words. Use it when the "
        "owner refers to something discussed before that is not in memory.",
        {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, "required": ["query"]},
    )
    def _search(args: dict[str, Any]) -> str:
        rows = store.search_messages(str(args.get("query") or ""), int(args.get("limit") or 10), exclude_session=current())
        if not rows:
            return "(nothing found in past sessions)"
        return "\n".join(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['created']))} [{r['session_id']}] "
                         f"{r['role']}: {r['snip']}" for r in rows)


__all__ = ["add_clarify_tool", "add_session_tools", "AskFn"]
