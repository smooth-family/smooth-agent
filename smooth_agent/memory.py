"""Long-term memory: what Smooth Agent should remember across sessions.

Each memory is one short note with a kind:
  user        facts about the owner (name, role, where they live, accounts)
  preference  how the owner likes things done
  decision    something decided, with the reason
  finding     something learned while working (a working command, an API quirk)
  blocker     an open problem to come back to
  note        anything else

Before every message the agent gets the pinned memories plus the ones that
best match the message (SQLite FTS5 ranking), so it does not have to search for
the obvious. The `memory` tool lets the model add, replace, remove, search and
list them — compatible with the add/replace/remove + target=memory|user shape
many skills use.
"""
from __future__ import annotations

import time
from typing import Any

from .store import Store, fts_query
from .tools import Registry, ToolError

KINDS = ("user", "preference", "decision", "finding", "blocker", "note")
MAX_TEXT = 1200


class Memory:
    def __init__(self, store: Store):
        self.s = store

    def add(self, text: str, kind: str = "note", tags: str = "", *, pinned: bool = False, source: str = "") -> int:
        text = " ".join(str(text or "").split())
        if not text:
            raise ToolError("memory text is empty")
        if len(text) > MAX_TEXT:
            raise ToolError(f"keep a memory under {MAX_TEXT} characters; split it or summarise")
        kind = kind if kind in KINDS else "note"
        dup = self.s.one("SELECT id FROM memories WHERE lower(text)=lower(?)", (text,))
        if dup:
            return int(dup["id"])
        now = time.time()
        with self.s.tx() as c:
            return int(c.execute("INSERT INTO memories (kind, text, tags, pinned, created, updated, source) VALUES (?,?,?,?,?,?,?)",
                                 (kind, text, tags, int(pinned), now, now, source)).lastrowid)

    def find_by_text(self, fragment: str) -> list[dict]:
        frag = str(fragment or "").strip()
        if not frag:
            return []
        return [dict(r) for r in self.s.q("SELECT * FROM memories WHERE instr(lower(text), lower(?)) > 0 ORDER BY updated DESC", (frag,))]

    def update(self, mid: int, *, text: str | None = None, kind: str | None = None, pinned: bool | None = None) -> None:
        row = self.s.one("SELECT * FROM memories WHERE id=?", (mid,))
        if not row:
            raise ToolError(f"no memory #{mid}")
        new_text = " ".join(str(text).split()) if text is not None else row["text"]
        if len(new_text) > MAX_TEXT:
            raise ToolError(f"keep a memory under {MAX_TEXT} characters")
        with self.s.tx() as c:
            c.execute("UPDATE memories SET text=?, kind=?, pinned=?, updated=? WHERE id=?",
                      (new_text, kind if kind in KINDS else row["kind"], int(row["pinned"] if pinned is None else pinned), time.time(), mid))

    def delete(self, mid: int) -> bool:
        with self.s.tx() as c:
            return c.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount > 0

    def search(self, query: str, limit: int = 8, kinds: tuple[str, ...] = ()) -> list[dict]:
        fq = fts_query(query)
        if not fq:
            return []
        rows = self.s.q("SELECT m.* FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                        "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?", (fq, limit * 3))
        out = [dict(r) for r in rows if not kinds or r["kind"] in kinds]
        return out[:limit]

    def list(self, kind: str | None = None, limit: int = 100) -> list[dict]:
        if kind:
            return [dict(r) for r in self.s.q("SELECT * FROM memories WHERE kind=? ORDER BY pinned DESC, updated DESC LIMIT ?", (kind, limit))]
        return [dict(r) for r in self.s.q("SELECT * FROM memories ORDER BY pinned DESC, updated DESC LIMIT ?", (limit,))]

    def count(self) -> int:
        return int(self.s.one("SELECT count(*) n FROM memories")["n"])

    def recall(self, message: str, *, limit: int = 8, max_chars: int = 3000) -> str:
        """The block put in front of a turn: pinned + always-on kinds + best matches for the message."""
        seen: set[int] = set()
        picked: list[dict] = []

        def take(rows):
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    picked.append(r)
        take([dict(r) for r in self.s.q("SELECT * FROM memories WHERE pinned=1 ORDER BY updated DESC LIMIT 20")])
        take([dict(r) for r in self.s.q("SELECT * FROM memories WHERE kind IN ('user','preference') ORDER BY updated DESC LIMIT 12")])
        take([dict(r) for r in self.s.q("SELECT * FROM memories WHERE kind='blocker' ORDER BY updated DESC LIMIT 5")])
        if message:
            take(self.search(message, limit=limit))
        if not picked:
            return ""
        lines, size = [], 0
        for r in picked:
            line = f"- #{r['id']} [{r['kind']}] {r['text']}"
            if size + len(line) > max_chars:
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)


MEMORY_PROMPT = (
    "\n\n## Memory\nYou have long-term memory that survives across sessions. What you remember about the owner and "
    "the work is listed under 'Remembered' below. Save with memory(action='add') when the owner tells you something "
    "durable about themselves or how they like things (target='user' or kind='preference'), when a decision is made "
    "(kind='decision', with the reason), when you learn something that will save time later (kind='finding'), or when "
    "a problem stays open (kind='blocker'). One short fact per memory; do not save secrets, one-off chatter, or what is "
    "already remembered. Fix outdated memories with action='replace' and drop wrong ones with action='remove'."
)


def add_memory_tool(reg: Registry, mem: Memory, *, session_id: str = "") -> None:
    @reg.tool(
        "memory",
        "Long-term memory. add: save content (target='user' for facts about the owner, else kind = preference | "
        "decision | finding | blocker | note). replace: find the memory containing old_text (or id) and replace it "
        "with content. remove: delete the memory containing old_text (or id). search: find memories by words. "
        "list: show memories (optionally one kind). pin/unpin: always show a memory.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove", "search", "list", "pin", "unpin"]},
            "content": {"type": "string", "description": "the memory text (add / replace) or the search words (search)"},
            "target": {"type": "string", "enum": ["memory", "user"], "description": "user = a fact about the owner"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "old_text": {"type": "string", "description": "a fragment of the memory to replace / remove"},
            "id": {"type": "integer"},
            "tags": {"type": "string"},
        }, "required": ["action"]},
    )
    def _memory(args: dict[str, Any]) -> str:
        action = args.get("action")
        kind = "user" if args.get("target") == "user" else (args.get("kind") or "note")

        def resolve() -> int:
            if args.get("id"):
                return int(args["id"])
            hits = mem.find_by_text(args.get("old_text") or "")
            if not hits:
                raise ToolError("no memory contains that old_text; use search or list to find it")
            if len(hits) > 1:
                raise ToolError("old_text matches several memories: " + "; ".join(f"#{h['id']} {h['text'][:60]}" for h in hits[:5]) + " — pass id")
            return int(hits[0]["id"])

        if action == "add":
            mid = mem.add(args.get("content") or "", kind, args.get("tags") or "", source=session_id)
            return f"remembered #{mid} [{kind}]"
        if action == "replace":
            mid = resolve()
            mem.update(mid, text=args.get("content") or "", kind=args.get("kind") or ("user" if args.get("target") == "user" else None))
            return f"updated #{mid}"
        if action == "remove":
            mid = resolve()
            mem.delete(mid)
            return f"forgot #{mid}"
        if action in ("pin", "unpin"):
            mid = resolve()
            mem.update(mid, pinned=action == "pin")
            return f"{action}ned #{mid}"
        if action == "search":
            rows = mem.search(args.get("content") or args.get("old_text") or "", limit=15)
            return "\n".join(f"#{r['id']} [{r['kind']}] {r['text']}" for r in rows) or "(nothing found)"
        if action == "list":
            rows = mem.list(args.get("kind"))
            return "\n".join(f"#{r['id']} [{r['kind']}]{' (pinned)' if r['pinned'] else ''} {r['text']}" for r in rows) or "(no memories yet)"
        raise ToolError("action must be add, replace, remove, search, list, pin or unpin")


__all__ = ["Memory", "KINDS", "MEMORY_PROMPT", "add_memory_tool"]
