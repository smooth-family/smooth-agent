"""Tool registry for the Smooth Agent loop.

A tool is a name, a human description, a JSON-Schema for its arguments, and a
Python handler. The agent advertises the schemas to the model (OpenAI
`tools` format) and runs the handler when the model calls it.

Stage 1 ships a small, safe, single-tenant default set: read-only inspection, a
sandboxed workspace for writes, and a size-capped HTTP GET. Richer tools
(browser, vision, background processes, MCP) live in their own modules and are
switched on in the config — see ROADMAP.md.

Everything that touches the filesystem is confined to one workspace directory
(path containment), so a prompt-injected path cannot escape it.
"""
from __future__ import annotations

import html
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .guard import scrubbed_env


class ToolError(Exception):
    """Raised by a handler when it cannot do what was asked; the message goes
    back to the model so it can recover instead of the turn crashing."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)
    handler: Callable[[dict[str, Any]], str]
    # True for tools that act on the machine (terminal): the owner approves each call
    # unless they turned confirmation off in the config.
    needs_approval: bool = False

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


# approver(tool_name, arguments) -> True to run the call, False to refuse it
Approver = Callable[[str, dict[str, Any]], bool]


class Registry:
    def __init__(self, approver: Approver | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # No approver = calls that need approval are refused (safe default for library use).
        self.approver = approver

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def tool(self, name: str, description: str, parameters: dict[str, Any], *, needs_approval: bool = False):
        def deco(fn: Callable[[dict[str, Any]], str]) -> Callable[[dict[str, Any]], str]:
            self.add(Tool(name=name, description=description, parameters=parameters, handler=fn,
                          needs_approval=needs_approval))
            return fn
        return deco

    def copy(self, *, without: tuple[str, ...] = ()) -> "Registry":
        r = Registry(self.approver)
        for t in self._tools.values():
            if t.name not in without:
                r.add(t)
        return r

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"error: unknown tool '{name}'"
        if tool.needs_approval and not (self.approver and self.approver(name, arguments or {})):
            return "denied: the owner did not approve this call. Do not retry it; explain what you wanted to do instead."
        try:
            return tool.handler(arguments or {})
        except ToolError as e:
            return f"error: {e}"
        except Exception as e:  # a tool bug must not kill the turn
            return f"error: {type(e).__name__}: {e}"


# ---------- workspace (path containment) ----------

class Workspace:
    """A single directory the agent may read and write. All paths are resolved
    and checked to stay inside it."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str) -> Path:
        p = (self.root / str(rel)).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path '{rel}' is outside the workspace")
        return p


MAX_READ_BYTES = 200_000
MAX_HTTP_BYTES = 1_000_000
MAX_TERMINAL_CHARS = 50_000
MAX_EXTRACT_CHARS = 20_000
TERMINAL_MAX_TIMEOUT = 600


def _fetch(url: str, timeout: float = 30) -> tuple[bytes, str]:
    if not str(url).startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")
    req = urllib.request.Request(str(url), headers={"user-agent": "smooth-agent/0.1 (+https://github.com/smooth-family/smooth-agent)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read(MAX_HTTP_BYTES + 1), resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"fetch failed: {e}") from None


_DROP_BLOCKS = re.compile(r"(?is)<(script|style|noscript|svg|head|template)[^>]*>.*?</\1>")
_BLOCK_TAGS = re.compile(r"(?i)</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|pre|blockquote)[^>]*>")


def html_to_text(raw: str) -> str:
    """Readable text from HTML, stdlib only: drop scripts/styles, keep block breaks."""
    s = _DROP_BLOCKS.sub(" ", raw)
    s = _BLOCK_TAGS.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 3 // 4], text[-limit // 4:]
    return f"{head}\n...[{len(text) - limit} chars cut]...\n{tail}"


def run_terminal(command: str, *, cwd: Path, timeout: int, env: dict[str, str]) -> str:
    """Run one shell command in `cwd` with a scrubbed env; returns exit code + output."""
    shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    try:
        proc = subprocess.Popen([shell, "-c", command], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        raise ToolError(f"could not start the shell: {e}") from None
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the whole process group, not just the shell
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, _ = proc.communicate()
        code, timed_out = None, True
    text = (out or b"").decode("utf-8", "replace")
    head = f"[timed out after {timeout}s]" if timed_out else f"[exit {code}]"
    return f"{head}\n{_cap(text, MAX_TERMINAL_CHARS)}" if text.strip() else head


def _post_form(url: str, data: dict[str, str], timeout: float = 20) -> str:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) smooth-agent/0.1", "content-type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read(MAX_HTTP_BYTES).decode("utf-8", "replace")


def _json_request(url: str, *, headers: dict[str, str], body: dict[str, Any] | None = None, timeout: float = 20) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **headers}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read(MAX_HTTP_BYTES).decode("utf-8", "replace"))


def web_search(query: str, limit: int = 5, *, backend: str = "auto", env: dict[str, str] | None = None) -> list[dict[str, str]]:
    """[{title, url, snippet}] from Tavily / Brave / SearXNG (when configured) or DuckDuckGo (no key)."""
    env = env if env is not None else dict(os.environ)
    limit = max(1, min(int(limit or 5), 10))
    if backend == "auto":
        backend = ("tavily" if env.get("TAVILY_API_KEY") else "brave" if env.get("BRAVE_API_KEY")
                   else "searxng" if env.get("SEARXNG_URL") else "duckduckgo")
    try:
        if backend == "tavily":
            j = _json_request("https://api.tavily.com/search", headers={"authorization": f"Bearer {env.get('TAVILY_API_KEY', '')}"},
                              body={"query": query, "max_results": limit})
            return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")} for r in j.get("results", [])][:limit]
        if backend == "brave":
            url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": limit})
            j = _json_request(url, headers={"x-subscription-token": env.get("BRAVE_API_KEY", ""), "accept": "application/json"})
            return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": html_to_text(r.get("description", ""))}
                    for r in (j.get("web") or {}).get("results", [])][:limit]
        if backend == "searxng":
            url = env.get("SEARXNG_URL", "").rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
            j = _json_request(url, headers={})
            return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")} for r in j.get("results", [])][:limit]
        page = _post_form("https://html.duckduckgo.com/html/", {"q": query})
        out = []
        for m in re.finditer(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:class="result__snippet"[^>]*>(.*?)</a>|</div>)', page):
            href = html.unescape(m.group(1))
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            real = q.get("uddg", [href])[0]
            if real.startswith("//"):
                real = "https:" + real
            if "duckduckgo.com/y.js" in real:  # ads
                continue
            out.append({"title": html_to_text(m.group(2)), "url": real, "snippet": html_to_text(m.group(3) or "")})
            if len(out) >= limit:
                break
        return out
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ToolError(f"search failed ({backend}): {e}") from None


def todo_tool() -> Tool:
    """A todo tool with its own list (one per agent / sub-agent)."""
    todos: list[dict[str, Any]] = []
    made: list[Tool] = []

    def _as_tool(name: str, description: str, parameters: dict[str, Any]):
        def deco(fn):
            made.append(Tool(name=name, description=description, parameters=parameters, handler=fn))
            return fn
        return deco


    @_as_tool(
        "todo",
        "Keep a task list for multi-step work. Pass todos (id, content, status) to set the list, or merge=true to update "
        "items by id. Call with no arguments to read it. Statuses: pending, in_progress, completed, cancelled.",
        {"type": "object", "properties": {
            "todos": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "content": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
            }, "required": ["id", "content", "status"]}},
            "merge": {"type": "boolean"},
        }, "required": []},
    )
    def _todo(args: dict[str, Any]) -> str:
        items = args.get("todos")
        if items is not None:
            if not args.get("merge"):
                todos.clear()
            for it in items:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                cur = next((t for t in todos if t["id"] == str(it["id"])), None)
                row = {"id": str(it["id"]), "content": str(it.get("content") or (cur or {}).get("content", "")),
                       "status": it.get("status") if it.get("status") in ("pending", "in_progress", "completed", "cancelled") else "pending"}
                if cur:
                    cur.update(row)
                else:
                    todos.append(row)
        if not todos:
            return "(no todos)"
        mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}
        done = sum(t["status"] == "completed" for t in todos)
        return f"{done}/{len(todos)} done\n" + "\n".join(f"{mark[t['status']]} {t['id']}. {t['content']}" for t in todos)

    return made[0]


def default_registry(workspace: Workspace, *, allow_http: bool = True, terminal: bool = False,
                     terminal_timeout: int = 180, secret_env_names: tuple[str, ...] = (),
                     approver: Approver | None = None, code: bool | None = None,
                     search_backend: str = "auto", processes: Any = None) -> Registry:
    reg = Registry(approver)
    code = terminal if code is None else code

    @reg.tool("now", "Current UTC date and time (ISO 8601).", {"type": "object", "properties": {}})
    def _now(_args: dict[str, Any]) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @reg.tool(
        "list_dir", "List files and folders in a workspace directory.",
        {"type": "object", "properties": {"path": {"type": "string", "description": "relative path, '' for root"}},
         "required": []},
    )
    def _list_dir(args: dict[str, Any]) -> str:
        d = workspace.resolve(args.get("path", "") or ".")
        if not d.exists():
            raise ToolError(f"no such directory: {args.get('path', '')}")
        if not d.is_dir():
            raise ToolError(f"not a directory: {args.get('path', '')}")
        out = []
        for child in sorted(d.iterdir()):
            kind = "dir" if child.is_dir() else "file"
            size = child.stat().st_size if child.is_file() else ""
            out.append(f"{kind}\t{size}\t{child.relative_to(workspace.root)}")
        return "\n".join(out) or "(empty)"

    @reg.tool(
        "read_file",
        "Read a UTF-8 text file from the workspace. For big files pass offset (1-based first line) and limit (lines); "
        "lines come back numbered so patch/search results line up.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1, "description": "first line to return (1-based)"},
            "limit": {"type": "integer", "minimum": 1, "description": "max lines to return"},
        }, "required": ["path"]},
    )
    def _read_file(args: dict[str, Any]) -> str:
        p = workspace.resolve(args["path"])
        if not p.is_file():
            raise ToolError(f"no such file: {args['path']}")
        if not args.get("offset") and not args.get("limit"):
            data = p.read_bytes()[:MAX_READ_BYTES]
            text = data.decode("utf-8", "replace")
            if p.stat().st_size > MAX_READ_BYTES:
                text += f"\n...[truncated at {MAX_READ_BYTES} bytes; use offset/limit to read further]"
            return text
        start = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or 400), 5000))
        out, total = [], 0
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                total = i
                if start <= i < start + limit:
                    out.append(f"{i:6}\t{line.rstrip(chr(10))}")
        if not out:
            return f"(no lines: file has {total} lines)"
        more = f"\n...[{total - (start + limit - 1)} more lines]" if total >= start + limit else ""
        return "\n".join(out) + more

    @reg.tool(
        "write_file", "Create or overwrite a UTF-8 text file in the workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]},
    )
    def _write_file(args: dict[str, Any]) -> str:
        p = workspace.resolve(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args.get("content", "")), encoding="utf-8")
        return f"wrote {len(str(args.get('content', '')))} chars to {p.relative_to(workspace.root)}"

    @reg.tool(
        "patch",
        "Edit a workspace file. mode=replace: replace old_string with new_string (old_string must match exactly and "
        "be unique unless replace_all=true). mode=patch: apply a unified diff (paths relative to the workspace).",
        {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["replace", "patch"]},
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "patch": {"type": "string", "description": "unified diff, for mode=patch"},
        }, "required": ["mode"]},
    )
    def _patch(args: dict[str, Any]) -> str:
        mode = args.get("mode") or ("patch" if args.get("patch") else "replace")
        if mode == "replace":
            p = workspace.resolve(args.get("path") or "")
            if not p.is_file():
                raise ToolError(f"no such file: {args.get('path')}")
            old, new = args.get("old_string"), args.get("new_string", "")
            if not old:
                raise ToolError("old_string is required for mode=replace")
            text = p.read_text(encoding="utf-8")
            n = text.count(old)
            if n == 0:
                raise ToolError("old_string not found; read the file again and copy the text exactly")
            if n > 1 and not args.get("replace_all"):
                raise ToolError(f"old_string matches {n} places; add surrounding lines to make it unique or set replace_all")
            p.write_text(text.replace(old, str(new)) if args.get("replace_all") else text.replace(old, str(new), 1), encoding="utf-8")
            return f"replaced {n if args.get('replace_all') else 1} occurrence(s) in {p.relative_to(workspace.root)}"
        diff = str(args.get("patch") or "")
        if not diff.strip():
            raise ToolError("patch text is required for mode=patch")
        for target in re.findall(r"^(?:\+\+\+|---) (?:[ab]/)?(\S+)", diff, re.M):
            if target != "/dev/null":
                workspace.resolve(target)  # raises if a path escapes the workspace
        for cmd in (["git", "apply", "--unsafe-paths", "--whitespace=nowarn", "-"], ["patch", "-p1", "--forward"], ["patch", "-p0", "--forward"]):
            try:
                r = subprocess.run(cmd, input=diff.encode(), cwd=str(workspace.root), capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                return f"patch applied ({cmd[0]})"
        raise ToolError("the diff did not apply; check context lines, or use mode=replace")

    @reg.tool(
        "search_files",
        "Search the workspace. target=content: regex over file contents (returns path:line:text). "
        "target=files: find files whose path matches the pattern (glob or regex).",
        {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "target": {"type": "string", "enum": ["content", "files"]},
            "path": {"type": "string", "description": "subdirectory to search (relative)"},
            "file_glob": {"type": "string", "description": "only files matching this glob, e.g. *.py"},
            "limit": {"type": "integer", "minimum": 1},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"]},
        }, "required": ["pattern"]},
    )
    def _search_files(args: dict[str, Any]) -> str:
        import fnmatch
        base = workspace.resolve(args.get("path") or ".")
        limit = max(1, min(int(args.get("limit") or 100), 1000))
        pattern = str(args["pattern"])
        glob = args.get("file_glob")
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}

        def files():
            for root, dirs, names in os.walk(base):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for n in names:
                    fp = Path(root) / n
                    rel = str(fp.relative_to(workspace.root))
                    if glob and not fnmatch.fnmatch(n, glob) and not fnmatch.fnmatch(rel, glob):
                        continue
                    yield fp, rel

        if (args.get("target") or "content") == "files":
            try:
                rx = re.compile(fnmatch.translate(pattern)) if any(c in pattern for c in "*?[") else re.compile(pattern)
            except re.error as e:
                raise ToolError(f"bad pattern: {e}") from None
            hits = [rel for _fp, rel in files() if rx.search(rel) or rx.search(Path(rel).name)]
            return "\n".join(hits[:limit]) + (f"\n...[{len(hits) - limit} more]" if len(hits) > limit else "") if hits else "(no files)"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"bad regex: {e}") from None
        mode = args.get("output_mode") or "content"
        out, counts = [], {}
        for fp, rel in files():
            try:
                if fp.stat().st_size > 5_000_000:
                    continue
                with open(fp, encoding="utf-8", errors="strict") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            counts[rel] = counts.get(rel, 0) + 1
                            if mode == "content" and len(out) < limit:
                                out.append(f"{rel}:{i}:{line.rstrip()[:300]}")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable
        if mode == "count":
            return "\n".join(f"{c}\t{r}" for r, c in sorted(counts.items(), key=lambda x: -x[1])[:limit]) or "(no matches)"
        if mode == "files_only":
            return "\n".join(list(counts)[:limit]) or "(no matches)"
        total = sum(counts.values())
        return ("\n".join(out) + (f"\n...[{total - len(out)} more matches]" if total > len(out) else "")) if out else "(no matches)"

    reg.add(todo_tool())

    if allow_http:
        @reg.tool(
            "http_get", "Fetch a URL and return the raw body (size-capped). Best for JSON APIs; use web_extract for pages.",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        )
        def _http_get(args: dict[str, Any]) -> str:
            raw, _ctype = _fetch(args["url"])
            text = raw[:MAX_HTTP_BYTES].decode("utf-8", "replace")
            if len(raw) > MAX_HTTP_BYTES:
                text += f"\n...[truncated at {MAX_HTTP_BYTES} bytes]"
            return text

        @reg.tool(
            "web_extract", "Fetch up to 5 web pages and return their readable text (scripts, styles and markup removed).",
            {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
             "required": ["urls"]},
        )
        def _web_extract(args: dict[str, Any]) -> str:
            urls = args.get("urls") or ([args["url"]] if args.get("url") else [])
            if isinstance(urls, str):
                urls = [urls]
            if not urls:
                raise ToolError("give at least one url")
            parts = []
            for url in list(urls)[:5]:
                try:
                    raw, ctype = _fetch(url)
                    body = raw[:MAX_HTTP_BYTES].decode("utf-8", "replace")
                    text = html_to_text(body) if ("html" in ctype or "<html" in body[:500].lower()) else body
                    parts.append(f"## {url}\n{_cap(text, MAX_EXTRACT_CHARS)}")
                except ToolError as e:
                    parts.append(f"## {url}\nerror: {e}")
            return "\n\n".join(parts)

    if allow_http:
        @reg.tool(
            "web_search", "Search the web. Returns titles, URLs and snippets; follow up with web_extract to read a result.",
            {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
             "required": ["query"]},
        )
        def _web_search(args: dict[str, Any]) -> str:
            res = web_search(str(args["query"]), int(args.get("limit") or 5), backend=search_backend)
            if not res:
                return "(no results)"
            return "\n\n".join(f"{i}. {r['title']}\n{r['url']}\n{r['snippet'][:300]}" for i, r in enumerate(res, 1))

    if code:
        code_limit = max(1, min(int(terminal_timeout), TERMINAL_MAX_TIMEOUT))

        @reg.tool(
            "execute_code",
            "Run a Python 3 program on the owner's machine, in the workspace directory, and return its exit code and output "
            "(print what you need). Same rules as terminal: owner's user, no API keys in the environment, "
            f"timeout {code_limit}s.",
            {"type": "object", "properties": {"code": {"type": "string", "description": "Python source"}}, "required": ["code"]},
            needs_approval=True,
        )
        def _execute_code(args: dict[str, Any]) -> str:
            import sys
            import tempfile
            src = str(args.get("code") or "")
            if not src.strip():
                raise ToolError("code is empty")
            scratch = workspace.resolve(".smooth")
            scratch.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile("w", suffix=".py", dir=scratch, delete=False, encoding="utf-8") as fh:
                fh.write(src)
                path = fh.name
            try:
                import shlex
                return run_terminal(f"{shlex.quote(sys.executable)} {shlex.quote(path)}", cwd=workspace.root,
                                    timeout=code_limit, env=scrubbed_env(secret_env_names))
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    if terminal:
        limit = max(1, min(int(terminal_timeout), TERMINAL_MAX_TIMEOUT))

        @reg.tool(
            "terminal",
            "Run a shell command on the owner's machine (bash), starting in the workspace directory. "
            "Returns the exit code and combined output. Commands run as the owner's user, without the owner's API keys "
            f"in the environment. Non-interactive only (stdin is closed). Default timeout {limit}s, max {TERMINAL_MAX_TIMEOUT}s.",
            {"type": "object", "properties": {
                "command": {"type": "string", "description": "The command to execute"},
                "timeout": {"type": "integer", "minimum": 1, "description": f"Max seconds to wait (default {limit}, max {TERMINAL_MAX_TIMEOUT})"},
                "workdir": {"type": "string", "description": "Directory inside the workspace to start in (relative path)"},
                "background": {"type": "boolean", "description": "Start it in the background and return a session id at once "
                               "(servers, watchers, long builds/tests); check on it with the process tool."},
                "notify_on_complete": {"type": "boolean", "description": "With background=true: you get told automatically "
                                       "when it exits, with its last output. Use it for every long task that has an end."},
            }, "required": ["command"]},
            needs_approval=True,
        )
        def _terminal(args: dict[str, Any]) -> str:
            command = str(args.get("command") or "").strip()
            if not command:
                raise ToolError("command is empty")
            t = max(1, min(int(args.get("timeout") or limit), TERMINAL_MAX_TIMEOUT))
            cwd = workspace.resolve(args.get("workdir") or ".")
            if not cwd.is_dir():
                raise ToolError(f"workdir does not exist: {args.get('workdir')}")
            if args.get("background"):
                if processes is None:
                    raise ToolError("background processes are not available here; run it in the foreground with a timeout")
                s = processes.start(command, cwd=cwd, env=scrubbed_env(secret_env_names),
                                    notify=bool(args.get("notify_on_complete")))
                note = " You will be told when it exits." if s.notify else " Check it with process(action='poll')."
                return f"started in the background as {s.id} (log: {s.log_path}).{note}"
            return run_terminal(command, cwd=cwd, timeout=t, env=scrubbed_env(secret_env_names))

    return reg


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool-call arguments arrive as a JSON string (OpenAI) or already a dict."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except (json.JSONDecodeError, TypeError):
        return {}


__all__ = ["Tool", "Registry", "Workspace", "ToolError", "default_registry", "parse_arguments"]
