"""Scheduled agent runs that survive restarts.

- `crons` table: named schedules with a prompt (created by `smooth-agent cron add`
  or by the agent with the `cronjob` tool).
- `jobs` table: a durable queue. When a cron is due, a job is queued; a worker
  claims it with a lease. If the process dies mid-job, the lease runs out and
  the job is retried (with backoff) until max_attempts.
- `smooth-agent daemon` runs `Scheduler.loop()`: tick crons, run due jobs, write
  each result to $SMOOTH_AGENT_HOME/cron-output/<name>/ and hand it to the
  delivery hooks (chat channels plug in there).
"""
from __future__ import annotations

import json
import os
import socket
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from .schedule import ScheduleError, next_run, parse

try:
    import fcntl
except ImportError:  # Windows: no advisory locks; the daemon lock becomes best-effort
    fcntl = None  # type: ignore
from .store import Store
from .tools import Registry, ToolError

LEASE_SECONDS = 30 * 60
NAME_OK = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Crons:
    """CRUD over the crons table."""

    def __init__(self, store: Store, tz: str | None = None):
        self.s = store
        self.tz = tz

    def create(self, name: str, schedule: str, prompt: str, *, repeat: int = 0, deliver: str = "",
               skills: list[str] | None = None, model: str = "", script: str = "") -> dict:
        name = str(name or "").strip()
        if not NAME_OK.match(name):
            raise ToolError("name: letters, digits, '.', '_' or '-' (max 64), starting with a letter or digit")
        if not str(prompt or "").strip() and not str(script or "").strip():
            raise ToolError("give a prompt (what the agent should do) and/or a script (a command to run)")
        try:
            sched = parse(schedule)
        except ScheduleError as e:
            raise ToolError(str(e)) from None
        nxt = next_run(schedule, time.time(), self.tz)
        if nxt is None:
            raise ToolError("that schedule has no future run")
        if self.s.one("SELECT 1 FROM crons WHERE name=?", (name,)):
            raise ToolError(f"a cron job named '{name}' exists; update or remove it first")
        if sched.kind == "once":
            repeat = 1
        with self.s.tx() as c:
            c.execute("INSERT INTO crons (name, schedule, prompt, enabled, repeat, runs, deliver, skills, model, next_run, created, script) "
                      "VALUES (?,?,?,1,?,0,?,?,?,?,?,?)", (name, schedule, prompt or "", int(repeat or 0), deliver or "",
                                                           json.dumps(skills or []), model or "", nxt, time.time(), script or ""))
        return self.get(name)

    def get(self, name: str) -> dict:
        r = self.s.one("SELECT * FROM crons WHERE name=?", (name,))
        if not r:
            raise ToolError(f"no cron job named '{name}'")
        return dict(r)

    def list(self) -> list[dict]:
        return [dict(r) for r in self.s.q("SELECT * FROM crons ORDER BY name")]

    def update(self, name: str, **fields: Any) -> dict:
        cur = self.get(name)
        sets, args = [], []
        if fields.get("schedule"):
            try:
                parse(fields["schedule"])
            except ScheduleError as e:
                raise ToolError(str(e)) from None
            sets += ["schedule=?", "next_run=?"]
            args += [fields["schedule"], next_run(fields["schedule"], time.time(), self.tz)]
        for k in ("prompt", "deliver", "model", "script"):
            if fields.get(k) is not None:
                sets.append(f"{k}=?")
                args.append(str(fields[k]))
        if fields.get("repeat") is not None:
            sets.append("repeat=?")
            args.append(int(fields["repeat"]))
        if fields.get("skills") is not None:
            sets.append("skills=?")
            args.append(json.dumps(list(fields["skills"])))
        if "enabled" in fields:
            sets.append("enabled=?")
            args.append(int(bool(fields["enabled"])))
            if fields["enabled"] and not cur["enabled"]:
                sets.append("next_run=?")
                args.append(next_run(cur["schedule"], time.time(), self.tz))
        if sets:
            with self.s.tx() as c:
                c.execute(f"UPDATE crons SET {', '.join(sets)} WHERE name=?", (*args, name))
        return self.get(name)

    def remove(self, name: str) -> bool:
        with self.s.tx() as c:
            return c.execute("DELETE FROM crons WHERE name=?", (name,)).rowcount > 0


class Jobs:
    def __init__(self, store: Store):
        self.s = store

    def enqueue(self, kind: str, payload: dict, *, run_at: float | None = None, priority: int = 0, max_attempts: int = 3) -> int:
        now = time.time()
        with self.s.tx() as c:
            return int(c.execute("INSERT INTO jobs (kind, payload, priority, run_at, state, attempts, max_attempts, created, updated) "
                                 "VALUES (?,?,?,?,'queued',0,?,?,?)",
                                 (kind, json.dumps(payload), priority, run_at or now, max_attempts, now, now)).lastrowid)

    def claim(self, worker: str) -> dict | None:
        now = time.time()
        with self.s.tx() as c:
            # leases that ran out: the worker died mid-job
            for r in c.execute("SELECT id, attempts, max_attempts FROM jobs WHERE state='running' AND locked_until < ?", (now,)).fetchall():
                if r["attempts"] >= r["max_attempts"]:
                    c.execute("UPDATE jobs SET state='failed', last_error=coalesce(last_error,'')||' [lease expired]', updated=? WHERE id=?", (now, r["id"]))
                else:
                    c.execute("UPDATE jobs SET state='queued', locked_by=NULL, run_at=?, updated=? WHERE id=?", (now, now, r["id"]))
            row = c.execute("SELECT * FROM jobs WHERE state='queued' AND run_at <= ? ORDER BY priority DESC, run_at, id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET state='running', locked_by=?, locked_until=?, attempts=attempts+1, updated=? WHERE id=?",
                      (worker, now + LEASE_SECONDS, now, row["id"]))
        job = dict(row)
        job["attempts"] += 1
        job["payload"] = json.loads(job["payload"] or "{}")
        return job

    def finish(self, job_id: int, result: str) -> None:
        with self.s.tx() as c:
            c.execute("UPDATE jobs SET state='done', result=?, locked_by=NULL, updated=? WHERE id=?", (result[:50_000], time.time(), job_id))

    def fail(self, job: dict, error: str) -> str:
        now = time.time()
        state = "failed" if job["attempts"] >= job["max_attempts"] else "queued"
        backoff = 60 * (2 ** max(0, job["attempts"] - 1))
        with self.s.tx() as c:
            c.execute("UPDATE jobs SET state=?, last_error=?, locked_by=NULL, run_at=?, updated=? WHERE id=?",
                      (state, error[:5000], now + backoff, now, job["id"]))
        return state

    def list(self, state: str | None = None, limit: int = 30) -> list[dict]:
        if state:
            return [dict(r) for r in self.s.q("SELECT * FROM jobs WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit))]
        return [dict(r) for r in self.s.q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]


# run_job(prompt, cron_row) -> result text
RunFn = Callable[[str, dict], str]
DeliverFn = Callable[[dict, str, bool], None]  # (cron row, text, ok)


SILENT = "[SILENT]"
SCRIPT_TIMEOUT = 300


def run_script(command: str, cwd: Path, timeout: int = SCRIPT_TIMEOUT) -> tuple[int | None, str]:
    """A cron's script: shell command, no secrets in its environment, stdout+stderr, timeout."""
    from .guard import scrubbed_env
    from .tools import run_terminal
    out = run_terminal(command, cwd=cwd, timeout=timeout, env=scrubbed_env())
    head, _, body = out.partition("\n")
    code = None if "timed out" in head else int(head.strip("[]").split()[-1])
    return code, body


class Scheduler:
    def __init__(self, store: Store, home: Path, run_fn: RunFn, *, tz: str | None = None,
                 deliver: list[DeliverFn] | None = None, log: Callable[[str], None] = print):
        self.store = store
        self.home = home
        self.crons = Crons(store, tz)
        self.jobs = Jobs(store)
        self.run_fn = run_fn
        self.tz = tz
        self.deliver = deliver or []
        self.log = log
        self.worker = f"{socket.gethostname()}:{os.getpid()}"
        self._stop = False

    def tick_crons(self, now: float | None = None) -> int:
        now = now or time.time()
        queued = 0
        for cr in self.store.q("SELECT * FROM crons WHERE enabled=1 AND next_run IS NOT NULL AND next_run <= ?", (now,)):
            cr = dict(cr)
            nxt = next_run(cr["schedule"], now, self.tz)
            runs = cr["runs"] + 1
            done = bool(cr["repeat"]) and runs >= cr["repeat"]
            with self.store.tx() as c:
                c.execute("UPDATE crons SET next_run=?, runs=?, last_run=?, enabled=? WHERE name=?",
                          (None if done or nxt is None else nxt, runs, now, 0 if done or nxt is None else 1, cr["name"]))
            self.jobs.enqueue("cron", {"name": cr["name"]})
            queued += 1
        return queued

    def run_one(self) -> bool:
        job = self.jobs.claim(self.worker)
        if not job:
            return False
        name = job["payload"].get("name", "")
        try:
            cr = self.crons.get(name)
        except ToolError:
            self.jobs.finish(job["id"], "cron job was removed")
            return True
        prompt = cr["prompt"]
        skills = json.loads(cr.get("skills") or "[]")
        if skills:
            prompt = f"Before you start, load and follow these skills with skill_view: {', '.join(skills)}.\n\n{prompt}"
        self.log(f"cron {name}: running (job {job['id']}, attempt {job['attempts']})")
        try:
            if cr.get("script"):
                code, output = run_script(cr["script"], self.home)
                if code not in (0, None) and not output.strip():
                    raise RuntimeError(f"script exited {code}")
                if not output.strip():
                    result = SILENT  # watcher pattern: nothing new, nothing to say
                elif cr["prompt"].strip():
                    result = self.run_fn(f"{prompt}\n\nOutput of the scheduled script `{cr['script']}`:\n{output[-20000:]}", cr)
                else:
                    result = output if code in (0, None) else f"[script exited {code}]\n{output}"
            else:
                result = self.run_fn(prompt, cr)
            ok = True
        except Exception as e:  # the run itself failed: retry later
            result = f"{type(e).__name__}: {e}"
            ok = False
            state = self.jobs.fail(job, result + "\n" + traceback.format_exc()[-2000:])
            self.log(f"cron {name}: failed ({state}): {result[:200]}")
        if ok:
            self.jobs.finish(job["id"], result)
            self.log(f"cron {name}: done")
        with self.store.tx() as c:
            c.execute("UPDATE crons SET last_status=? WHERE name=?", ("ok" if ok else "error", name))
        out = self.home / "cron-output" / name
        out.mkdir(parents=True, exist_ok=True)
        (out / time.strftime("%Y%m%d-%H%M%S.md")).write_text(result, encoding="utf-8")
        silent = ok and result.strip() == SILENT
        if silent:
            self.log(f"cron {name}: nothing to report")
        if (ok and not silent) or (not ok and job["attempts"] >= job["max_attempts"]):
            for d in self.deliver:
                try:
                    d(cr, result, ok)
                except Exception as e:
                    self.log(f"cron {name}: delivery failed: {e}")
        return True

    def loop(self, *, interval: float = 15.0, once: bool = False) -> None:
        while not self._stop:
            try:
                self.tick_crons()
                while self.run_one() and not self._stop:
                    pass
            except Exception as e:
                self.log(f"scheduler error: {e}")
            if once:
                return
            for _ in range(int(interval * 10)):
                if self._stop:
                    return
                time.sleep(0.1)

    def stop(self) -> None:
        self._stop = True


class DaemonLock:
    """One daemon per home: an exclusive lock on $SMOOTH_AGENT_HOME/daemon.pid."""

    def __init__(self, home: Path):
        self.path = home / "daemon.pid"
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")  # noqa: SIM115
        if fcntl is None:
            return True
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    @staticmethod
    def running(home: Path) -> bool:
        p = home / "daemon.pid"
        if not p.exists() or fcntl is None:
            return p.exists() and fcntl is None
        with open(p, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
                return False
            except OSError:
                return True


def fmt_time(ts: float | None, tz: str | None = None) -> str:
    if not ts:
        return "-"
    from datetime import datetime
    from .schedule import get_tz
    return datetime.fromtimestamp(ts, get_tz(tz)).strftime("%Y-%m-%d %H:%M")


def add_cron_tool(reg: Registry, crons: Crons, jobs: Jobs, home: Path, *, scripts_allowed: bool = False) -> None:
    @reg.tool(
        "cronjob",
        "Schedule agent runs. create: name, schedule ('daily 09:00', 'weekdays 08:30', 'every 2h', '0 9 * * 1-5', "
        "'at 2026-11-03 09:00'), prompt (a complete, self-contained instruction — the run will not see this chat), "
        "optional repeat (stop after N runs), skills (list of skill names to load), deliver (where to send the "
        "result, e.g. 'telegram'), script (a shell command run first; empty output = nothing to report, otherwise its "
        "output is reported or handed to the prompt — good for cheap watchers). Reply exactly [SILENT] from a run to "
        "send nothing. Also: list, update, pause, resume, remove, run (queue it now). Runs happen while "
        "`smooth-agent daemon` is running.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "update", "pause", "resume", "remove", "run"]},
            "name": {"type": "string"}, "job_id": {"type": "string", "description": "same as name"},
            "schedule": {"type": "string"}, "prompt": {"type": "string"},
            "repeat": {"type": "integer", "minimum": 0}, "deliver": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}}, "model": {"type": "string"},
            "script": {"type": "string", "description": "shell command for a watcher-style job (needs terminal enabled and the owner's OK)"},
        }, "required": ["action"]},
    )
    def _cron(args: dict[str, Any]) -> str:
        action = args.get("action")
        name = str(args.get("name") or args.get("job_id") or "")
        daemon = "" if DaemonLock.running(home) else "\nnote: the scheduler is not running; start it with `smooth-agent daemon`."
        if action == "list":
            rows = crons.list()
            if not rows:
                return "(no cron jobs)"
            return "\n".join(f"{r['name']}\t{'on' if r['enabled'] else 'off'}\t{r['schedule']}\tnext {fmt_time(r['next_run'], crons.tz)}\t"
                             f"runs {r['runs']}{'/' + str(r['repeat']) if r['repeat'] else ''}\t{r['prompt'][:60]}" for r in rows) + daemon
        if args.get("script") and action in ("create", "update"):
            if not scripts_allowed:
                raise ToolError("scheduled scripts need [tools] terminal = true")
            if not (reg.approver and reg.approver("cronjob", {"command": f"(scheduled '{args.get('schedule') or name}') {args['script']}"})):
                return "denied: the owner did not approve this scheduled command."
        if action == "create":
            r = crons.create(name, args.get("schedule") or "", args.get("prompt") or "", repeat=int(args.get("repeat") or 0),
                             deliver=args.get("deliver") or "", skills=args.get("skills"), model=args.get("model") or "",
                             script=args.get("script") or "")
            return f"created '{r['name']}' ({parse(r['schedule']).describe()}); next run {fmt_time(r['next_run'], crons.tz)}" + daemon
        if action == "update":
            r = crons.update(name, schedule=args.get("schedule"), prompt=args.get("prompt"), repeat=args.get("repeat"),
                             deliver=args.get("deliver"), skills=args.get("skills"), model=args.get("model"),
                             script=args.get("script"))
            return f"updated '{name}'; next run {fmt_time(r['next_run'], crons.tz)}"
        if action in ("pause", "resume"):
            r = crons.update(name, enabled=action == "resume")
            return f"{name}: {'on, next run ' + fmt_time(r['next_run'], crons.tz) if r['enabled'] else 'paused'}"
        if action == "remove":
            return f"removed '{name}'" if crons.remove(name) else f"error: no cron job named '{name}'"
        if action == "run":
            crons.get(name)
            jid = jobs.enqueue("cron", {"name": name}, priority=5)
            return f"queued '{name}' to run now (job {jid})" + daemon
        raise ToolError("unknown action")


__all__ = ["Crons", "Jobs", "Scheduler", "DaemonLock", "add_cron_tool", "fmt_time"]
