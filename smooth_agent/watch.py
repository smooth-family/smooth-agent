"""web_watch: check a page or JSON API on a schedule and speak up only when it changes.

A watch is a cron job with a `watch` spec (url + what to look at). Each run the
scheduler fetches the page, extracts the part that matters, and compares it with
the last snapshot. Nothing changed → nothing is sent and no model is called.
Changed → the difference is delivered (or handed to the job's prompt).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from typing import Any

from .tools import Registry, ToolError, _fetch, html_to_text

MAX_SNAPSHOT = 60_000
MIN_INTERVAL = 300  # seconds; be polite to the sites being watched


def json_pick(data: Any, path: str) -> Any:
    """'data.items.0.price' / 'items[0].name' -> the value at that path."""
    for key in [k for k in re.split(r"[.\[\]]+", str(path or "")) if k]:
        if isinstance(data, list):
            try:
                data = data[int(key)]
            except (ValueError, IndexError):
                raise ToolError(f"json_path: no index '{key}'") from None
        elif isinstance(data, dict):
            if key not in data:
                raise ToolError(f"json_path: no key '{key}' (have: {', '.join(list(data)[:15])})")
            data = data[key]
        else:
            raise ToolError(f"json_path: cannot go into '{key}'")
    return data


def snapshot(spec: dict[str, Any], fetch=None) -> str:
    """The text a watch compares: the page as text (or JSON), narrowed by json_path and/or select (regex)."""
    raw, ctype = (fetch or _fetch)(str(spec.get("url") or ""))
    text = raw.decode("utf-8", "replace")
    if spec.get("json_path") or "json" in ctype.lower():
        try:
            data = json.loads(text)
        except ValueError:
            raise ToolError("the URL did not return JSON") from None
        if spec.get("json_path"):
            data = json_pick(data, spec["json_path"])
        text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False)
    elif "html" in ctype.lower() or text.lstrip().startswith("<"):
        text = html_to_text(text)
    sel = str(spec.get("select") or "")
    if sel:
        try:
            rx = re.compile(sel, re.I | re.M)
        except re.error as e:
            raise ToolError(f"select is not a valid regex: {e}") from None
        hits = []
        for m in rx.finditer(text):
            hits.append(m.group(0) if not m.groups() else " ".join(g or "" for g in m.groups()))
        text = "\n".join(h.strip() for h in hits) or "(nothing matched)"
    return text[:MAX_SNAPSHOT]


def compare(old: str | None, new: str, limit: int = 4000) -> str | None:
    """None when equal; otherwise a short line diff (old → new)."""
    if old is not None and hashlib.sha256(old.encode()).digest() == hashlib.sha256(new.encode()).digest():
        return None
    if old is None:
        return None
    diff = [ln for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "now", n=1, lineterm="")
            if not ln.startswith(("---", "+++"))]
    out = "\n".join(diff)
    return out[:limit] + ("\n…" if len(out) > limit else "") if out else "(whitespace changed)"


def run_watch(store, cr: dict[str, Any], fetch=None) -> tuple[bool, str]:
    """One scheduled check. Returns (changed, report). The first run only records the baseline."""
    spec = json.loads(cr.get("watch") or "{}")
    new = snapshot(spec, fetch)
    old = spec.get("last")
    diff = compare(old, new)
    spec.update({"last": new, "checked": time.time(), **({"changed": time.time()} if diff else {})})
    with store.tx() as c:
        c.execute("UPDATE crons SET watch=? WHERE name=?", (json.dumps(spec), cr["name"]))
    if diff is None:
        return False, ""
    what = spec.get("label") or spec.get("url")
    return True, f"🔔 {what} changed:\n{diff}\n{spec.get('url')}"


def _schedule(every: str) -> str:
    every = str(every or "30m").strip().lower()
    m = re.fullmatch(r"(?:every\s+)?(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", every)
    if not m:
        raise ToolError("every: like '15m', '2h' or '1d'")
    n, unit = int(m.group(1)), m.group(2)[0]
    secs = n * {"m": 60, "h": 3600, "d": 86400}[unit]
    if secs < MIN_INTERVAL:
        raise ToolError(f"check at most every {MIN_INTERVAL // 60} minutes")
    return f"every {n}{unit}"


def add_watch_tool(reg: Registry, crons, jobs, *, fetch=None) -> None:
    @reg.tool(
        "web_watch",
        "Watch a web page or JSON API and tell the owner only when it changes (no model call while nothing "
        "changes). create: name, url, every ('15m', '2h', '1d'; min 5 min), select (regex for the part that "
        "matters, e.g. 'Harga[^\\n]*' or 'Stok: (\\d+)'), json_path ('data.price') for APIs, label, deliver "
        "('telegram'), prompt (optional: what to do with the change, e.g. 'tell me if the price went down'). "
        "Also: list, check (run now and show what it sees), remove.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "check", "remove"]},
            "name": {"type": "string"}, "url": {"type": "string"}, "every": {"type": "string"},
            "select": {"type": "string"}, "json_path": {"type": "string"}, "label": {"type": "string"},
            "deliver": {"type": "string"}, "prompt": {"type": "string"},
        }, "required": ["action"]},
    )
    def _watch(args: dict[str, Any]) -> str:
        action = args.get("action")
        name = str(args.get("name") or "")
        if action == "list":
            rows = [r for r in crons.list() if r.get("watch")]
            if not rows:
                return "(no watches)"
            out = []
            for r in rows:
                s = json.loads(r["watch"])
                seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["changed"])) if s.get("changed") else "never"
                out.append(f"{r['name']}\t{'on' if r['enabled'] else 'off'}\t{r['schedule']}\t{s.get('url')}"
                           f"{' select=' + s['select'] if s.get('select') else ''}\tlast change: {seen}")
            return "\n".join(out)
        if action == "remove":
            cr = crons.get(name)
            if not cr.get("watch"):
                raise ToolError(f"'{name}' is not a watch")
            crons.remove(name)
            return f"removed watch '{name}'"
        if action == "check":
            if name:
                spec = json.loads(crons.get(name).get("watch") or "{}")
            else:
                spec = {k: args.get(k) for k in ("url", "select", "json_path") if args.get(k)}
            text = snapshot(spec, fetch)
            return f"{len(text)} chars watched:\n{text[:3000]}" + ("\n…" if len(text) > 3000 else "")
        if action == "create":
            url = str(args.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise ToolError("url must start with http:// or https://")
            spec = {"url": url, **{k: args[k] for k in ("select", "json_path", "label") if args.get(k)}}
            first = snapshot(spec, fetch)  # fails now (bad URL, regex, path) instead of at 3 am
            if not first.strip():
                raise ToolError("the page has no text without JavaScript (an app shell): watch the JSON API it loads "
                                "instead (find it with http_get / the browser's network calls), with json_path")
            if first == "(nothing matched)":
                raise ToolError("select matched nothing on the page right now; check it with action=check first")
            spec["last"] = first
            spec["checked"] = time.time()
            r = crons.create(name, _schedule(args.get("every") or "30m"), str(args.get("prompt") or ""),
                             deliver=str(args.get("deliver") or ""), watch=spec)
            preview = first[:300].replace("\n", " | ")
            return (f"watching {url} {r['schedule']} as '{name}'. It sees now: {preview}"
                    + ("…" if len(first) > 300 else "") + "\nYou'll hear only when this changes.")
        raise ToolError("unknown action")


__all__ = ["add_watch_tool", "snapshot", "compare", "run_watch", "json_pick"]
