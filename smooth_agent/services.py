"""More connected services: calendar (Google Calendar / CalDAV), Notion and
Google Drive. Standard library only — every call is plain HTTP over urllib.

Same contract as connect.py: reads are free, anything that changes a service
(create an event, append to a Notion page) asks the owner first, and tokens come
from the environment and never pass through the model.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .connect import UA, _ask
from .guard import redact
from .tools import Registry, ToolError, _cap

SHOW_CHARS = 30_000
MAX_BODY = 1_000_000


# ---------------------------------------------------------------- HTTP helper

def request(method: str, url: str, *, headers: dict[str, str] | None = None, query: dict[str, Any] | None = None,
            json_body: Any = None, data: bytes | None = None, timeout: float = 60.0) -> tuple[int, Any, str]:
    """One HTTP call. Returns (status, parsed_json_or_None, raw_text)."""
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    head = {"user-agent": UA, "accept": "application/json", **{str(k): str(v) for k, v in (headers or {}).items()}}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        head.setdefault("content-type", "application/json")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=head)
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, min(float(timeout or 60), 120.0))) as r:  # noqa: S310
            status, raw = r.status, r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(MAX_BODY + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"request failed: {e}") from None
    text = raw[:MAX_BODY].decode("utf-8", "replace")
    obj = None
    try:
        obj = json.loads(text)
    except ValueError:
        pass
    return status, obj, text


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _check(status: int, obj: Any, text: str, what: str) -> Any:
    if status >= 400:
        msg = ""
        if isinstance(obj, dict):
            msg = str(obj.get("message") or obj.get("error") or obj.get("error_description") or "")
            if isinstance(obj.get("error"), dict):
                msg = str(obj["error"].get("message") or msg)
        raise ToolError(f"{what}: HTTP {status}{' — ' + msg if msg else ''}"[:500])
    return obj if obj is not None else text


# ---------------------------------------------------------------- time helpers

def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(days: float) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now, now + timedelta(days=max(0.01, float(days or 7)))


def _fmt_dt(value: str) -> str:
    """Trim an ISO/RFC3339 timestamp to 'YYYY-MM-DD HH:MM'; leave all-day dates as is."""
    v = str(value or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", v)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return v


# ================================================================ Calendar

@dataclass
class CalendarSettings:
    kind: str = ""            # "google" | "caldav"
    token: str = ""           # google: OAuth access token
    calendar_id: str = "primary"
    url: str = ""             # caldav: the calendar collection URL
    user: str = ""
    password: str = ""
    api: str = "https://www.googleapis.com/calendar/v3"

    @property
    def configured(self) -> bool:
        return (self.kind == "google" and bool(self.token)) or (self.kind == "caldav" and bool(self.url))

    @classmethod
    def from_config(cls, cfg: dict[str, Any], env: dict[str, str] | None = None) -> "CalendarSettings":
        import os
        env = env if env is not None else dict(os.environ)
        c = dict(cfg.get("calendar") or {})
        kind = str(c.get("type") or c.get("kind") or ("google" if c.get("token_env") else ("caldav" if c.get("url") else ""))).lower()
        token = str(env.get(str(c.get("token_env") or "GOOGLE_TOKEN"), "") or "")
        pw = str(env.get(str(c.get("password_env") or "CALDAV_PASSWORD"), "") or "")
        return cls(kind=kind, token=token, calendar_id=str(c.get("calendar_id") or "primary"),
                   url=str(c.get("url") or ""), user=str(c.get("user") or ""), password=pw,
                   api=str(c.get("api") or "https://www.googleapis.com/calendar/v3"))


# ---- Google Calendar ----

def _gcal_list(st: CalendarSettings, days: float, query: str = "") -> str:
    lo, hi = _window(days)
    base = f"{st.api.rstrip('/')}/calendars/{urllib.parse.quote(st.calendar_id)}/events"
    status, obj, text = request("GET", base, headers=_bearer(st.token), query={
        "timeMin": _rfc3339(lo), "timeMax": _rfc3339(hi), "singleEvents": "true",
        "orderBy": "startTime", "maxResults": 50, "q": query or None})
    obj = _check(status, obj, text, "calendar")
    items = obj.get("items", []) if isinstance(obj, dict) else []
    if not items:
        return "(no events in that window)"
    out = []
    for e in items:
        start = (e.get("start") or {})
        when = _fmt_dt(start.get("dateTime") or start.get("date") or "")
        out.append(f"{when}\t{e.get('summary') or '(no title)'}" + (f"\t@ {e['location']}" if e.get("location") else ""))
    return redact("\n".join(out))


def _gcal_create(st: CalendarSettings, a: dict) -> str:
    start, end = str(a.get("start") or ""), str(a.get("end") or "")
    if not start or not end:
        raise ToolError("create needs start and end (RFC3339, e.g. 2026-11-05T09:00:00Z)")
    body = {"summary": a.get("summary") or "(no title)", "start": {"dateTime": start}, "end": {"dateTime": end}}
    if a.get("description"):
        body["description"] = str(a["description"])
    if a.get("location"):
        body["location"] = str(a["location"])
    base = f"{st.api.rstrip('/')}/calendars/{urllib.parse.quote(st.calendar_id)}/events"
    status, obj, text = request("POST", base, headers=_bearer(st.token), json_body=body)
    obj = _check(status, obj, text, "calendar create")
    return f"created event '{body['summary']}'" + (f" ({obj.get('htmlLink')})" if isinstance(obj, dict) and obj.get("htmlLink") else "")


# ---- CalDAV ----

_CALDAV_REPORT = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    '<D:prop><D:getetag/><C:calendar-data/></D:prop>'
    '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
    '<C:time-range start="{lo}" end="{hi}"/>'
    '</C:comp-filter></C:comp-filter></C:filter></C:calendar-query>'
)


def _caldav_auth_header(st: CalendarSettings) -> dict[str, str]:
    import base64
    if st.user:
        raw = base64.b64encode(f"{st.user}:{st.password}".encode()).decode()
        return {"authorization": f"Basic {raw}"}
    return {}


def unfold_ics(text: str) -> str:
    """RFC5545 line unfolding: a CRLF followed by a space or tab continues the previous line."""
    return re.sub(r"\r?\n[ \t]", "", text)


def parse_vevents(ics: str) -> list[dict]:
    """Minimal VEVENT reader: SUMMARY, DTSTART, DTEND, LOCATION per event."""
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfold_ics(ics), re.S):
        ev: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            name = key.split(";", 1)[0].strip().upper()
            if name in ("SUMMARY", "DTSTART", "DTEND", "LOCATION"):
                ev[name] = val.strip()
        if ev:
            events.append(ev)
    return events


def _ics_dt(value: str) -> str:
    """ICS DTSTART like 20261105T090000Z or 20261105 -> 'YYYY-MM-DD HH:MM'/'YYYY-MM-DD'."""
    v = str(value or "").strip()
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?", v)
    if not m:
        return v
    y, mo, d, hh, mm = m.groups()
    return f"{y}-{mo}-{d}" + (f" {hh}:{mm}" if hh else "")


def _caldav_list(st: CalendarSettings, days: float, query: str = "") -> str:
    lo, hi = _window(days)
    body = _CALDAV_REPORT.format(lo=lo.strftime("%Y%m%dT%H%M%SZ"), hi=hi.strftime("%Y%m%dT%H%M%SZ")).encode()
    head = {"content-type": "application/xml; charset=utf-8", "depth": "1", **_caldav_auth_header(st)}
    status, _obj, text = request("REPORT", st.url, headers=head, data=body)
    if status >= 400:
        raise ToolError(f"caldav: HTTP {status}")
    import xml.etree.ElementTree as ET
    events: list[dict] = []
    try:
        root = ET.fromstring(text)
        for data in root.iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
            events += parse_vevents(data.text or "")
    except ET.ParseError:
        events = parse_vevents(text)  # some servers return raw ICS
    rows = []
    for e in sorted(events, key=lambda e: e.get("DTSTART", "")):
        if query and query.lower() not in (e.get("SUMMARY", "")).lower():
            continue
        rows.append(f"{_ics_dt(e.get('DTSTART', ''))}\t{e.get('SUMMARY') or '(no title)'}"
                    + (f"\t@ {e['LOCATION']}" if e.get("LOCATION") else ""))
    return redact("\n".join(rows)) if rows else "(no events in that window)"


def _caldav_create(st: CalendarSettings, a: dict) -> str:
    start, end = str(a.get("start") or ""), str(a.get("end") or "")
    if not start or not end:
        raise ToolError("create needs start and end (e.g. 2026-11-05T09:00:00Z)")
    def ics_stamp(v: str) -> str:
        return re.sub(r"[-:]", "", v).replace(" ", "T")
    uid = f"{uuid.uuid4().hex}@smooth-agent"
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//smooth-agent//EN\r\nBEGIN:VEVENT\r\n"
           f"UID:{uid}\r\nDTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}\r\n"
           f"DTSTART:{ics_stamp(start)}\r\nDTEND:{ics_stamp(end)}\r\n"
           f"SUMMARY:{a.get('summary') or '(no title)'}\r\n"
           + (f"DESCRIPTION:{a['description']}\r\n" if a.get("description") else "")
           + (f"LOCATION:{a['location']}\r\n" if a.get("location") else "")
           + "END:VEVENT\r\nEND:VCALENDAR\r\n")
    url = st.url.rstrip("/") + "/" + uid + ".ics"
    head = {"content-type": "text/calendar; charset=utf-8", "if-none-match": "*", **_caldav_auth_header(st)}
    status, _o, _t = request("PUT", url, headers=head, data=ics.encode())
    if status >= 400:
        raise ToolError(f"caldav create: HTTP {status}")
    return f"created event '{a.get('summary') or '(no title)'}'"


def add_calendar_tool(reg: Registry, st: CalendarSettings) -> None:
    @reg.tool(
        "calendar",
        "The owner's calendar (Google Calendar or CalDAV, set in config). agenda: upcoming events (days, default 7). "
        "search: events whose title matches a query (days window). create (asks the owner): summary, start, end "
        "(RFC3339 like 2026-11-05T09:00:00Z), optional description, location.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["agenda", "search", "create"]},
            "days": {"type": "number"}, "query": {"type": "string"},
            "summary": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
            "description": {"type": "string"}, "location": {"type": "string"},
        }, "required": ["action"]},
    )
    def _cal(a: dict[str, Any]) -> str:
        action = a.get("action") or "agenda"
        if action in ("agenda", "search"):
            days = a.get("days") or (30 if action == "search" else 7)
            q = str(a.get("query") or "") if action == "search" else ""
            return _gcal_list(st, days, q) if st.kind == "google" else _caldav_list(st, days, q)
        if action == "create":
            preview = {k: a.get(k) for k in ("summary", "start", "end", "location") if a.get(k)}
            if not _ask(reg, "calendar", preview):
                return "denied: the owner did not approve creating this event."
            return _gcal_create(st, a) if st.kind == "google" else _caldav_create(st, a)
        raise ToolError("actions: agenda, search, create")


# ================================================================ Notion

NOTION_VERSION = "2022-06-28"


def _notion_token(cfg: dict, env: dict) -> str:
    import os
    env = env if env is not None else dict(os.environ)
    n = dict(cfg.get("notion") or {})
    return str(env.get(str(n.get("token_env") or "NOTION_TOKEN"), "") or "")


def _rich_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(p.get("plain_text", "") for p in parts if isinstance(p, dict))


def _block_text(block: dict) -> str:
    t = block.get("type", "")
    body = block.get(t) or {}
    text = _rich_text(body.get("rich_text")) if isinstance(body, dict) else ""
    if t in ("heading_1", "heading_2", "heading_3"):
        return "#" * int(t[-1]) + " " + text
    if t == "to_do":
        return f"[{'x' if body.get('checked') else ' '}] {text}"
    if t in ("bulleted_list_item", "numbered_list_item"):
        return f"- {text}"
    if t == "code":
        return f"```\n{text}\n```"
    return text


class Notion:
    def __init__(self, token: str, api: str = "https://api.notion.com/v1"):
        self.token = token
        self.api = api.rstrip("/")

    def _head(self) -> dict[str, str]:
        if not self.token:
            raise ToolError("Notion needs NOTION_TOKEN in the environment (an internal integration token)")
        return {**_bearer(self.token), "notion-version": NOTION_VERSION}

    def call(self, method: str, path: str, *, json_body: Any = None, query: dict | None = None) -> Any:
        status, obj, text = request(method, self.api + path, headers=self._head(), json_body=json_body, query=query)
        return _check(status, obj, text, "notion")


def notion_action(client: Notion, a: dict) -> str:
    action = a.get("action")
    if action == "search":
        body: dict[str, Any] = {"page_size": int(a.get("limit") or 20)}
        if a.get("query"):
            body["query"] = str(a["query"])
        res = client.call("POST", "/search", json_body=body)
        rows = []
        for r in res.get("results", []) if isinstance(res, dict) else []:
            kind = r.get("object", "")
            title = ""
            props = r.get("properties") or {}
            for p in props.values():
                if isinstance(p, dict) and p.get("type") == "title":
                    title = _rich_text(p.get("title"))
                    break
            if not title and kind == "database":
                title = _rich_text(r.get("title"))
            rows.append(f"{kind}\t{r.get('id', '')}\t{title or '(untitled)'}")
        return redact("\n".join(rows)) if rows else "(nothing found)"
    if action == "page":
        pid = str(a.get("id") or "").strip()
        if not pid:
            raise ToolError("page needs an id")
        res = client.call("GET", f"/blocks/{urllib.parse.quote(pid)}/children", query={"page_size": 100})
        lines = [_block_text(b) for b in (res.get("results", []) if isinstance(res, dict) else [])]
        return redact(_cap("\n".join(l for l in lines if l), SHOW_CHARS)) or "(empty page)"
    if action == "database":
        did = str(a.get("id") or "").strip()
        if not did:
            raise ToolError("database needs an id")
        res = client.call("POST", f"/databases/{urllib.parse.quote(did)}/query", json_body={"page_size": int(a.get("limit") or 20)})
        rows = []
        for r in res.get("results", []) if isinstance(res, dict) else []:
            cells = []
            for name, p in (r.get("properties") or {}).items():
                if not isinstance(p, dict):
                    continue
                t = p.get("type")
                if t == "title":
                    cells.append(f"{name}={_rich_text(p.get('title'))}")
                elif t == "rich_text":
                    cells.append(f"{name}={_rich_text(p.get('rich_text'))}")
                elif t == "select" and p.get("select"):
                    cells.append(f"{name}={p['select'].get('name', '')}")
                elif t in ("number", "checkbox", "url", "email", "phone_number"):
                    cells.append(f"{name}={p.get(t)}")
            rows.append("\t".join(cells))
        return redact(_cap("\n".join(rows), SHOW_CHARS)) if rows else "(no rows)"
    raise ToolError("actions: search, page, database, append")


def add_notion_tool(reg: Registry, cfg: dict, env: dict | None = None) -> None:
    import os
    token = _notion_token(cfg, env if env is not None else dict(os.environ))

    @reg.tool(
        "notion",
        "The owner's Notion (needs NOTION_TOKEN, an internal integration shared with the pages). search (query -> pages "
        "and databases with their ids), page (id -> its text), database (id -> rows). append (asks the owner): id (page) "
        "+ text, adds a paragraph. Share a page/database with the integration first or it is invisible.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["search", "page", "database", "append"]},
            "query": {"type": "string"}, "id": {"type": "string"}, "text": {"type": "string"},
            "limit": {"type": "integer"},
        }, "required": ["action"]},
    )
    def _notion(a: dict[str, Any]) -> str:
        client = Notion(token)
        if a.get("action") == "append":
            pid = str(a.get("id") or "").strip()
            text = str(a.get("text") or "")
            if not pid or not text:
                raise ToolError("append needs id (the page) and text")
            if not _ask(reg, "notion", {"append_to": pid, "text": text[:600]}):
                return "denied: the owner did not approve this."
            client.call("PATCH", f"/blocks/{urllib.parse.quote(pid)}/children", json_body={"children": [
                {"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}]})
            return "appended a paragraph"
        return notion_action(client, a)


# ================================================================ Google Drive

class Drive:
    def __init__(self, token: str, api: str = "https://www.googleapis.com/drive/v3"):
        self.token = token
        self.api = api.rstrip("/")

    def _head(self) -> dict[str, str]:
        if not self.token:
            raise ToolError("Google Drive needs a token (GOOGLE_TOKEN, an OAuth access token with the Drive scope)")
        return _bearer(self.token)

    def call(self, method: str, path_or_url: str, *, query: dict | None = None) -> tuple[int, Any, str]:
        url = path_or_url if path_or_url.startswith("http") else self.api + path_or_url
        return request(method, url, headers=self._head(), query=query)


# a Google-native file exported to a text format we can read
_EXPORT = {
    "application/vnd.google-apps.document": ("text/plain", "txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "csv"),
    "application/vnd.google-apps.presentation": ("text/plain", "txt"),
}


def drive_action(client: Drive, a: dict) -> str:
    action = a.get("action")
    if action in ("list", "search"):
        q = str(a.get("query") or "").strip()
        params = {"pageSize": int(a.get("limit") or 25), "fields": "files(id,name,mimeType,modifiedTime,size)",
                  "orderBy": "modifiedTime desc", "q": (f"name contains '{q}' and trashed=false" if q else "trashed=false")}
        status, obj, text = client.call("GET", "/files", query=params)
        obj = _check(status, obj, text, "drive")
        rows = [f"{f.get('id')}\t{f.get('mimeType', '').split('.')[-1]:<12}\t{f.get('name', '')}"
                for f in (obj.get("files", []) if isinstance(obj, dict) else [])]
        return redact("\n".join(rows)) if rows else "(no files)"
    if action == "read":
        fid = str(a.get("id") or "").strip()
        if not fid:
            raise ToolError("read needs a file id")
        status, meta, text = client.call("GET", f"/files/{urllib.parse.quote(fid)}", query={"fields": "id,name,mimeType"})
        meta = _check(status, meta, text, "drive")
        mime = meta.get("mimeType", "") if isinstance(meta, dict) else ""
        if mime in _EXPORT:
            fmt, _ = _EXPORT[mime]
            status, _o, body = client.call("GET", f"/files/{urllib.parse.quote(fid)}/export", query={"mimeType": fmt})
        elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
            status, _o, body = client.call("GET", f"/files/{urllib.parse.quote(fid)}", query={"alt": "media"})
        else:
            raise ToolError(f"cannot read {mime} as text (download it with http_request + a token if you must)")
        if status >= 400:
            raise ToolError(f"drive read: HTTP {status}")
        name = meta.get("name", fid) if isinstance(meta, dict) else fid
        return redact(f"# {name}\n\n" + _cap(body, SHOW_CHARS))
    raise ToolError("actions: list, search, read")


def add_drive_tool(reg: Registry, cfg: dict, env: dict | None = None) -> None:
    import os
    env = env if env is not None else dict(os.environ)
    g = dict(cfg.get("gdrive") or cfg.get("google") or {})
    token = str(env.get(str(g.get("token_env") or "GOOGLE_TOKEN"), "") or "")

    @reg.tool(
        "gdrive",
        "The owner's Google Drive (needs GOOGLE_TOKEN, an OAuth access token with a Drive scope). list / search (query -> "
        "files with their ids), read (id -> the file's text; Google Docs/Sheets/Slides are exported to text/CSV, and "
        "text files are read as is). Binary files can't be read as text.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "search", "read"]},
            "query": {"type": "string"}, "id": {"type": "string"}, "limit": {"type": "integer"},
        }, "required": ["action"]},
    )
    def _drive(a: dict[str, Any]) -> str:
        return drive_action(Drive(token), a)


# ---------------------------------------------------------------- wiring

def add_service_tools(reg: Registry, cfg: dict[str, Any], env: dict | None = None) -> list[str]:
    import os
    env = env if env is not None else dict(os.environ)
    notes = []
    cal = CalendarSettings.from_config(cfg, env)
    if cal.configured:
        add_calendar_tool(reg, cal)
        notes.append(f"calendar: {cal.kind}")
    if cfg.get("notion") is not None or env.get("NOTION_TOKEN"):
        add_notion_tool(reg, cfg, env)
        notes.append("notion: " + ("token set" if _notion_token(cfg, env) else "no NOTION_TOKEN"))
    if cfg.get("gdrive") is not None or cfg.get("google") is not None or env.get("GOOGLE_TOKEN"):
        add_drive_tool(reg, cfg, env)
        notes.append("gdrive: " + ("token set" if env.get("GOOGLE_TOKEN") else "no GOOGLE_TOKEN"))
    return notes


__all__ = ["CalendarSettings", "Notion", "Drive", "add_calendar_tool", "add_notion_tool", "add_drive_tool",
           "add_service_tools", "parse_vevents", "unfold_ics", "notion_action", "drive_action", "request"]
