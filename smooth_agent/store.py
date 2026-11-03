"""One SQLite database for everything Smooth Agent keeps: sessions and their
messages, long-term memory, the job queue and scheduled jobs.

File: $SMOOTH_AGENT_HOME/smooth.db (WAL mode, so the daemon and an interactive
session can use it at the same time). Full-text search uses SQLite FTS5, which
ships with Python's sqlite3. Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, title TEXT, channel TEXT DEFAULT 'cli', created REAL, updated REAL, meta TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  data TEXT NOT NULL, text TEXT DEFAULT '', created REAL,
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL DEFAULT 'note', text TEXT NOT NULL,
  tags TEXT DEFAULT '', pinned INTEGER DEFAULT 0, created REAL, updated REAL, source TEXT DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, tags, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
  INSERT INTO memories_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  priority INTEGER DEFAULT 0, run_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, locked_by TEXT, locked_until REAL,
  last_error TEXT, result TEXT, created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, run_at);

CREATE TABLE IF NOT EXISTS crons (
  name TEXT PRIMARY KEY, schedule TEXT NOT NULL, prompt TEXT NOT NULL, enabled INTEGER DEFAULT 1,
  repeat INTEGER DEFAULT 0, runs INTEGER DEFAULT 0, deliver TEXT DEFAULT '', skills TEXT DEFAULT '[]',
  model TEXT DEFAULT '', next_run REAL, last_run REAL, last_status TEXT DEFAULT '', created REAL,
  script TEXT DEFAULT ''
);
"""


MIGRATIONS = (
    "ALTER TABLE crons ADD COLUMN script TEXT DEFAULT ''",
    "ALTER TABLE crons ADD COLUMN watch TEXT DEFAULT ''",
)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write = threading.Lock()
        with self._write:  # executescript commits on its own, so it runs outside tx()
            self.conn().executescript(SCHEMA)
            for sql in MIGRATIONS:  # columns added after a database was created
                try:
                    self.conn().execute(sql)
                except sqlite3.OperationalError:
                    pass

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    class _Tx:
        def __init__(self, store: "Store"):
            self.s = store

        def __enter__(self) -> sqlite3.Connection:
            self.s._write.acquire()
            self.c = self.s.conn()
            self.c.execute("BEGIN IMMEDIATE")
            return self.c

        def __exit__(self, et, ev, tb):
            try:
                self.c.execute("ROLLBACK" if et else "COMMIT")
            finally:
                self.s._write.release()

    def tx(self) -> "Store._Tx":
        return Store._Tx(self)

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, args).fetchone()

    # ---------- sessions ----------

    def new_session(self, sid: str, *, title: str = "", channel: str = "cli", meta: dict | None = None) -> str:
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO sessions (id, title, channel, created, updated, meta) VALUES (?,?,?,?,?,?)",
                      (sid, title, channel, now, now, json.dumps(meta or {})))
        return sid

    def archive_session(self, sid: str) -> str | None:
        """Move a session's messages to a new id (`<sid>~<time>`) so `sid` starts empty. Returns the archive id."""
        row = self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if not row:
            return None
        new_id = f"{sid}~{time.strftime('%Y%m%d-%H%M%S')}"
        with self.tx() as c:
            c.execute("INSERT INTO sessions (id, title, channel, created, updated, meta) VALUES (?,?,?,?,?,?)",
                      (new_id, row["title"], row["channel"], row["created"], row["updated"], row["meta"]))
            c.execute("UPDATE messages SET session_id=? WHERE session_id=?", (new_id, sid))
            c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        return new_id

    def label_session(self, sid: str, channel: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET channel=? WHERE id=? AND channel IS NOT ?", (channel, sid, channel))

    def add_message(self, sid: str, msg: dict[str, Any]) -> int:
        text = message_text(msg)
        now = time.time()
        with self.tx() as c:
            cur = c.execute("INSERT INTO messages (session_id, role, data, text, created) VALUES (?,?,?,?,?)",
                            (sid, msg.get("role", ""), json.dumps(msg, ensure_ascii=False), text, now))
            c.execute("UPDATE sessions SET updated=? WHERE id=?", (now, sid))
            if msg.get("role") == "user" and text and not text.startswith("["):
                c.execute("UPDATE sessions SET title=? WHERE id=? AND (title IS NULL OR title='')", (text[:80], sid))
            return int(cur.lastrowid)

    def session_messages(self, sid: str) -> list[dict[str, Any]]:
        return [json.loads(r["data"]) for r in self.q("SELECT data FROM messages WHERE session_id=? ORDER BY id", (sid,))]

    def sessions(self, limit: int = 30, channel: str | None = None) -> list[sqlite3.Row]:
        if channel:
            return self.q("SELECT s.*, (SELECT count(*) FROM messages m WHERE m.session_id=s.id) n FROM sessions s "
                          "WHERE channel=? ORDER BY updated DESC LIMIT ?", (channel, limit))
        return self.q("SELECT s.*, (SELECT count(*) FROM messages m WHERE m.session_id=s.id) n FROM sessions s "
                      "ORDER BY updated DESC LIMIT ?", (limit,))

    def delete_session(self, sid: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount > 0

    def search_messages(self, query: str, limit: int = 10, exclude_session: str | None = None) -> list[sqlite3.Row]:
        fq = fts_query(query)
        if not fq:
            return []
        return self.q(
            "SELECT m.id, m.session_id, m.role, m.created, s.title, snippet(messages_fts, 0, '[', ']', ' … ', 24) snip "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid JOIN sessions s ON s.id = m.session_id "
            "WHERE messages_fts MATCH ? AND m.role IN ('user','assistant') AND (? IS NULL OR m.session_id != ?) "
            "ORDER BY bm25(messages_fts) LIMIT ?", (fq, exclude_session, exclude_session, limit))


def message_text(msg: dict[str, Any]) -> str:
    """Searchable text of a stored message (tool results and calls included, briefly)."""
    c = msg.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    text = str(c or "")
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        text += f" [{fn.get('name', '')} {str(fn.get('arguments', ''))[:200]}]"
    return text[:20_000]


def fts_query(text: str) -> str:
    """Free text -> a safe FTS5 query (each word quoted, OR'ed; prefix match for longer words)."""
    import re
    words = [w for w in re.findall(r"[\w@.-]+", str(text).lower()) if len(w) > 1][:12]
    if not words:
        return ""
    return " OR ".join(f'"{w}"*' if len(w) > 3 else f'"{w}"' for w in words)


def iter_rows(rows: list[sqlite3.Row]) -> Iterator[dict]:
    for r in rows:
        yield dict(r)


__all__ = ["Store", "message_text", "fts_query"]
