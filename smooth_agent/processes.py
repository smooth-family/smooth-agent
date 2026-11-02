"""Background processes: long commands the agent starts and checks on later.

`terminal(command, background=true)` starts a command here and returns a
session id at once. The `process` tool then lists, polls, reads logs, waits,
writes to stdin, or kills it. Output goes to a log file under
$SMOOTH_AGENT_HOME/processes/, so nothing is lost if the agent reads it late.

With notify_on_complete=true the agent loop is told when the command exits
(see `ProcessManager.take_finished`), so it never has to remember to poll.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools import ToolError, Registry

MAX_SESSIONS = 32
LOG_CAP_BYTES = 20_000_000  # per process; older output is dropped from the head when read


@dataclass
class Session:
    id: str
    command: str
    cwd: str
    log_path: Path
    proc: subprocess.Popen
    started: float = field(default_factory=time.time)
    ended: float | None = None
    notify: bool = False
    notified: bool = False
    read_offset: int = 0  # bytes already returned by poll

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()

    @property
    def running(self) -> bool:
        return self.proc.poll() is None


class ProcessManager:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.sessions: dict[str, Session] = {}
        self._n = 0
        self._lock = threading.Lock()

    def start(self, command: str, *, cwd: Path, env: dict[str, str], notify: bool = False) -> Session:
        with self._lock:
            live = [s for s in self.sessions.values() if s.running]
            if len(live) >= MAX_SESSIONS:
                raise ToolError(f"too many background processes ({len(live)}); kill some first")
            self._n += 1
            sid = f"p{self._n}"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{int(time.time())}-{sid}.log"
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        fh = open(log_path, "wb")  # noqa: SIM115 - the child owns it
        try:
            proc = subprocess.Popen([shell, "-c", command], cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                    stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            fh.close()
            raise ToolError(f"could not start: {e}") from None
        fh.close()  # the child keeps its own handle
        s = Session(id=sid, command=command, cwd=str(cwd), log_path=log_path, proc=proc, notify=notify)
        self.sessions[sid] = s
        threading.Thread(target=self._reap, args=(s,), daemon=True).start()
        return s

    def _reap(self, s: Session) -> None:
        s.proc.wait()
        s.ended = time.time()

    def get(self, sid: str) -> Session:
        s = self.sessions.get(str(sid))
        if not s:
            raise ToolError(f"no process '{sid}' (process action=list shows them)")
        return s

    def _read(self, s: Session, start: int = 0, max_bytes: int = 50_000) -> tuple[str, int]:
        try:
            size = s.log_path.stat().st_size
            with open(s.log_path, "rb") as fh:
                fh.seek(max(start, size - LOG_CAP_BYTES))
                data = fh.read(max_bytes)
            return data.decode("utf-8", "replace"), start + len(data)
        except OSError:
            return "", start

    def status(self, s: Session) -> str:
        age = int((s.ended or time.time()) - s.started)
        state = "running" if s.running else f"exited {s.exit_code}"
        return f"{s.id}\t{state}\t{age}s\t{s.command[:120]}"

    def kill(self, s: Session) -> None:
        if s.running:
            try:
                os.killpg(s.proc.pid, signal.SIGTERM)
                try:
                    s.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(s.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                s.proc.kill()

    def take_finished(self) -> list[str]:
        """Exit notices for notify_on_complete processes that finished since the last call."""
        out = []
        for s in list(self.sessions.values()):
            if s.notify and not s.notified and not s.running:
                s.notified = True
                tail, _ = self._read(s, max(0, s.log_path.stat().st_size - 1500) if s.log_path.exists() else 0, 1500)
                out.append(f"Background process {s.id} finished with exit code {s.exit_code}: {s.command[:160]}\n"
                           f"Last output:\n{tail.strip()[-1500:]}")
        return out

    def shutdown(self) -> None:
        for s in self.sessions.values():
            self.kill(s)


def add_process_tools(reg: Registry, pm: ProcessManager) -> None:
    @reg.tool(
        "process",
        "Manage background processes started with terminal(background=true). Actions: list; poll (status + new output "
        "since the last poll); log (output with offset/limit lines); wait (block until exit or timeout seconds); "
        "write (send data to stdin); submit (send data + newline); close (close stdin); kill.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"]},
            "session_id": {"type": "string"},
            "data": {"type": "string", "description": "for write / submit"},
            "timeout": {"type": "integer", "minimum": 1, "description": "for wait, seconds (max 600)"},
            "offset": {"type": "integer", "description": "for log: first line (1-based; negative = from the end)"},
            "limit": {"type": "integer", "minimum": 1, "description": "for log: number of lines"},
        }, "required": ["action"]},
    )
    def _process(args: dict[str, Any]) -> str:
        action = args.get("action")
        if action == "list":
            return "\n".join(pm.status(s) for s in pm.sessions.values()) or "(no background processes)"
        s = pm.get(args.get("session_id") or "")
        if action == "poll":
            text, s.read_offset = pm._read(s, s.read_offset)
            return f"{pm.status(s)}\n{text}" if text else pm.status(s) + "\n(no new output)"
        if action == "log":
            text, _ = pm._read(s, 0, LOG_CAP_BYTES)
            lines = text.splitlines()
            off, lim = int(args.get("offset") or -200), int(args.get("limit") or 200)
            start = len(lines) + off if off < 0 else max(0, off - 1)
            start = max(0, start)
            return f"{pm.status(s)}\n" + "\n".join(lines[start:start + lim])
        if action == "wait":
            t = max(1, min(int(args.get("timeout") or 60), 600))
            try:
                s.proc.wait(timeout=t)
            except subprocess.TimeoutExpired:
                pass
            text, s.read_offset = pm._read(s, s.read_offset)
            return f"{pm.status(s)}\n{text[-8000:]}"
        if action == "kill":
            pm.kill(s)
            return pm.status(s)
        if action in ("write", "submit"):
            if not s.running or not s.proc.stdin:
                raise ToolError("the process is not running")
            data = str(args.get("data") or "") + ("\n" if action == "submit" else "")
            try:
                s.proc.stdin.write(data.encode())
                s.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise ToolError(f"stdin closed: {e}") from None
            return f"sent {len(data)} chars to {s.id}"
        if action == "close":
            if s.proc.stdin:
                s.proc.stdin.close()
            return f"closed stdin of {s.id}"
        raise ToolError("unknown action")


__all__ = ["ProcessManager", "Session", "add_process_tools"]
