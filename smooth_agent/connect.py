"""Tools that connect the agent to the owner's services: any HTTP API, email, git,
GitHub and SQLite files. Standard library only.

Anything that changes something outside this machine's workspace — a POST/PUT/
DELETE request, sending an email, a git commit/push, opening a GitHub issue —
goes through the owner's approval first (the same Allow / Deny as terminal
commands; `[tools] confirm = false` turns the asking off).

Secrets never pass through the model: API keys are written as `{{env:NAME}}`
in a request and filled in here, the email password and the GitHub token come
from the environment, and every result is redacted before the model sees it.
"""
from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import json
import mimetypes
import os
import re
import shutil
import smtplib
import sqlite3
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from .guard import redact, scrubbed_env
from .tools import Registry, ToolError, Workspace, _cap, html_to_text

UA = "smooth-agent (+https://github.com/smooth-family/smooth-agent)"
MAX_BODY = 1_000_000
SHOW_CHARS = 30_000
_ENV_REF = re.compile(r"\{\{\s*env:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _ask(reg: Registry, name: str, summary: dict[str, Any]) -> bool:
    """The owner's OK for one action that changes something (False when there is no one to ask)."""
    return bool(reg.approver and reg.approver(name, summary))


# ---------------------------------------------------------------- http_request

def fill_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Replace {{env:NAME}} with the variable's value (in strings, lists and dicts)."""
    env = env if env is not None else dict(os.environ)
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in env:
                raise ToolError(f"environment variable {name} is not set (put it in ~/.smooth-agent/.env)")
            return env[name]
        return _ENV_REF.sub(sub, value)
    if isinstance(value, list):
        return [fill_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: fill_env(v, env) for k, v in value.items()}
    return value


def http_request(method: str, url: str, *, headers: dict[str, str] | None = None, query: dict[str, Any] | None = None,
                 json_body: Any = None, body: str | None = None, timeout: float = 60.0,
                 env: dict[str, str] | None = None) -> str:
    method = (method or "GET").upper()
    if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        raise ToolError(f"unsupported method {method}")
    url = fill_env(str(url or ""), env)
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")
    if query:
        q = urllib.parse.urlencode({k: fill_env(str(v), env) for k, v in query.items()})
        url += ("&" if "?" in url else "?") + q
    head = {"user-agent": UA, **{str(k): str(v) for k, v in fill_env(headers or {}, env).items()}}
    data = None
    if json_body is not None:
        data = json.dumps(fill_env(json_body, env)).encode()
        head.setdefault("content-type", "application/json")
    elif body is not None:
        data = fill_env(str(body), env).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=head)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, min(float(timeout or 60), 300.0))) as r:  # noqa: S310
            status, rh, raw = r.status, r.headers, r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        status, rh, raw = e.code, e.headers, e.read(MAX_BODY + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"request failed: {e}") from None
    ms = int((time.time() - t0) * 1000)
    ctype = rh.get("content-type", "") if rh else ""
    keep = [f"{k}: {rh.get(k)}" for k in ("content-type", "location", "retry-after", "x-ratelimit-remaining") if rh and rh.get(k)]
    text = raw[:MAX_BODY].decode("utf-8", "replace")
    if "json" in ctype.lower():
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    elif "html" in ctype.lower():
        text = html_to_text(text)
    out = f"HTTP {status} ({ms} ms)\n" + "\n".join(keep) + f"\n\n{_cap(text, SHOW_CHARS) if method != 'HEAD' else ''}"
    return redact(out)


def add_http_request_tool(reg: Registry) -> None:
    @reg.tool(
        "http_request",
        "Call any HTTP API: method, url, headers, query, json (or raw body). Write secrets as {{env:NAME}} "
        "(e.g. headers {\"Authorization\": \"Bearer {{env:NOTION_TOKEN}}\"}) — the value is filled in here and never "
        "shown to you. GET/HEAD are free; POST/PUT/PATCH/DELETE ask the owner first. Returns status, key headers and "
        "the body (JSON pretty-printed, HTML as text).",
        {"type": "object", "properties": {
            "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]},
            "url": {"type": "string"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "query": {"type": "object"},
            "json": {"description": "JSON body (object or array)"},
            "body": {"type": "string", "description": "raw body (when not JSON)"},
            "timeout": {"type": "number"},
        }, "required": ["url"]},
    )
    def _http(args: dict[str, Any]) -> str:
        method = str(args.get("method") or "GET").upper()
        if method not in ("GET", "HEAD", "OPTIONS"):
            preview = {"method": method, "url": args.get("url"), "body": str(args.get("json") or args.get("body") or "")[:400]}
            if not _ask(reg, "http_request", preview):
                return "denied: the owner did not approve this request. Do not retry it."
        return http_request(method, args.get("url", ""), headers=args.get("headers"), query=args.get("query"),
                            json_body=args.get("json"), body=args.get("body"), timeout=args.get("timeout") or 60)


# ---------------------------------------------------------------- email (IMAP + SMTP)

# well-known providers: only user + password are needed for these
PRESETS = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 587),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 587),
    "outlook.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "hotmail.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "live.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "zoho.com": ("imap.zoho.com", 993, "smtp.zoho.com", 465),
}


@dataclass
class EmailSettings:
    user: str = ""
    password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    from_addr: str = ""
    sent_folder: str = ""  # copy sent mail here over IMAP ("" = the server does it, like Gmail)

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.imap_host)

    @classmethod
    def from_config(cls, cfg: dict[str, Any], env: dict[str, str] | None = None) -> "EmailSettings":
        env = env if env is not None else dict(os.environ)
        e = dict(cfg.get("email") or {})
        user = str(e.get("user") or env.get("EMAIL_USER") or "")
        pw = str(e.get("password") or env.get(str(e.get("password_env") or "EMAIL_PASSWORD"), "") or "")
        domain = user.rsplit("@", 1)[-1].lower() if "@" in user else ""
        p = PRESETS.get(domain, ("", 993, "", 587))
        return cls(user=user, password=pw, imap_host=str(e.get("imap_host") or p[0]), imap_port=int(e.get("imap_port") or p[1]),
                   smtp_host=str(e.get("smtp_host") or p[2]), smtp_port=int(e.get("smtp_port") or p[3]),
                   from_addr=str(e.get("from") or user), sent_folder=str(e.get("sent_folder") or ""))


def _dec(value: Any) -> str:
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:
        return str(value or "")


def _since(text: str) -> str:
    """'2026-11-01' or '7d' -> IMAP date '01-Nov-2026'."""
    text = str(text or "").strip()
    m = re.fullmatch(r"(\d+)\s*d", text)
    t = time.time() - int(m.group(1)) * 86400 if m else time.mktime(time.strptime(text, "%Y-%m-%d"))
    return time.strftime("%d-%b-%Y", time.gmtime(t))


def _quote(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    def __init__(self, es: EmailSettings, connect: Callable[[EmailSettings], imaplib.IMAP4] | None = None):
        self.es = es
        self._connect = connect

    def imap(self) -> imaplib.IMAP4:
        if not self.es.configured:
            raise ToolError("email is not set up: [email] user + EMAIL_PASSWORD (an app password) in ~/.smooth-agent/.env")
        if self._connect:
            return self._connect(self.es)
        try:
            m = imaplib.IMAP4_SSL(self.es.imap_host, self.es.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
            m.login(self.es.user, self.es.password)
            return m
        except imaplib.IMAP4.error as e:
            raise ToolError(f"IMAP login failed ({e}); Gmail/Outlook/Yahoo need an app password") from None
        except OSError as e:
            raise ToolError(f"cannot reach {self.es.imap_host}: {e}") from None

    @staticmethod
    def _select(m: imaplib.IMAP4, folder: str) -> None:
        typ, data = m.select(_quote(folder or "INBOX"), readonly=False)
        if typ != "OK":
            raise ToolError(f"no folder '{folder}' ({data[0].decode(errors='replace') if data and data[0] else typ})")

    def folders(self) -> list[str]:
        m = self.imap()
        try:
            _t, data = m.list()
            out = []
            for raw in data or []:
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                mm = re.search(r'"([^"]*)"\s*$|(\S+)\s*$', line)
                if mm:
                    out.append(mm.group(1) if mm.group(1) is not None else mm.group(2))
            return out
        finally:
            m.logout()

    def search(self, *, folder: str = "INBOX", text: str = "", sender: str = "", subject: str = "", since: str = "",
               unseen: bool = False, limit: int = 10) -> list[dict[str, str]]:
        m = self.imap()
        try:
            self._select(m, folder)
            crit: list[str] = []
            if unseen:
                crit.append("UNSEEN")
            if since:
                crit += ["SINCE", _since(since)]
            for key, val in (("FROM", sender), ("SUBJECT", subject)):
                if val:
                    crit += [key, _quote(val.encode("ascii", "ignore").decode() or val)]
            literal = None
            if text:
                if text.isascii():
                    crit += ["TEXT", _quote(text)]
                else:  # non-ASCII words go as a literal with CHARSET UTF-8
                    literal = text.encode("utf-8")
                    crit.append("TEXT")
            if literal is not None:
                m.literal = literal
                typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", *crit)
            else:
                typ, data = m.uid("SEARCH", *(crit or ["ALL"]))
            if typ != "OK":
                raise ToolError(f"search failed: {data}")
            uids = (data[0] or b"").split()[-max(1, min(int(limit or 10), 50)):][::-1]
            out = []
            for uid in uids:
                typ, msg = m.uid("FETCH", uid, "(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if typ != "OK" or not msg or not isinstance(msg[0], tuple):
                    continue
                meta, hdr = msg[0]
                h = email.message_from_bytes(hdr, policy=email.policy.default)
                flags = meta.decode(errors="replace")
                out.append({"uid": uid.decode(), "date": _dec(h.get("Date")), "from": _dec(h.get("From")),
                            "subject": _dec(h.get("Subject")), "unread": "\\Seen" not in flags})
            return out
        finally:
            m.logout()

    def fetch(self, uid: str, folder: str = "INBOX", *, mark_read: bool = False) -> email.message.EmailMessage:
        if not re.fullmatch(r"\d+", str(uid)):
            raise ToolError("uid must be the number from email_search")
        m = self.imap()
        try:
            self._select(m, folder)
            typ, msg = m.uid("FETCH", str(uid), "(RFC822)" if mark_read else "(BODY.PEEK[])")
            if typ != "OK" or not msg or not isinstance(msg[0], tuple):
                raise ToolError(f"no message with uid {uid} in {folder}")
            return email.message_from_bytes(msg[0][1], policy=email.policy.default)  # type: ignore[return-value]
        finally:
            m.logout()

    def append(self, folder: str, raw: bytes) -> None:
        m = self.imap()
        try:
            m.append(_quote(folder), "\\Seen", imaplib.Time2Internaldate(time.time()), raw)
        finally:
            m.logout()


def message_text(msg: email.message.EmailMessage) -> str:
    """The readable text: the plain part, or the HTML one when the plain part is missing or empty."""
    for pref in ("plain", "html"):
        part = msg.get_body(preferencelist=(pref,))
        if part is None:
            continue
        try:
            body = str(part.get_content())
        except Exception:
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        text = html_to_text(body) if pref == "html" else body
        if text.strip():
            return text
    return ""


def attachments(msg: email.message.EmailMessage) -> list[tuple[str, bytes]]:
    out = []
    for part in msg.iter_attachments():
        name = _dec(part.get_filename() or "") or f"attachment{mimetypes.guess_extension(part.get_content_type()) or ''}"
        out.append((name, part.get_payload(decode=True) or b""))
    return out


def send_mail(es: EmailSettings, msg: EmailMessage, smtp: Callable[[EmailSettings], smtplib.SMTP] | None = None) -> None:
    if not (es.user and es.password and es.smtp_host):
        raise ToolError("sending is not set up: [email] smtp_host (or a known provider address) + EMAIL_PASSWORD")
    try:
        if smtp:
            s = smtp(es)
        elif es.smtp_port == 465:
            s = smtplib.SMTP_SSL(es.smtp_host, es.smtp_port, context=ssl.create_default_context(), timeout=60)
        else:
            s = smtplib.SMTP(es.smtp_host, es.smtp_port, timeout=60)
            s.starttls(context=ssl.create_default_context())
        with s:
            s.login(es.user, es.password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise ToolError(f"SMTP login refused ({e.smtp_code}); use an app password") from None
    except (smtplib.SMTPException, OSError) as e:
        raise ToolError(f"sending failed: {e}") from None


def add_email_tools(reg: Registry, ws: Workspace, es: EmailSettings, *, mailbox: Mailbox | None = None,
                    smtp: Callable[[EmailSettings], smtplib.SMTP] | None = None) -> None:
    box = mailbox or Mailbox(es)

    @reg.tool(
        "email_search",
        "Find emails in the owner's mailbox (IMAP). Filters: text (anywhere), sender, subject, since ('7d' or "
        "'2026-11-01'), unseen, folder (default INBOX; folders=true lists them). Newest first; gives uid, date, "
        "from, subject, unread. Read one with email_read.",
        {"type": "object", "properties": {
            "text": {"type": "string"}, "sender": {"type": "string"}, "subject": {"type": "string"},
            "since": {"type": "string"}, "unseen": {"type": "boolean"}, "folder": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "folders": {"type": "boolean"},
        }},
    )
    def _search(args: dict[str, Any]) -> str:
        if args.get("folders"):
            return "\n".join(box.folders()) or "(no folders)"
        rows = box.search(folder=args.get("folder") or "INBOX", text=args.get("text") or "", sender=args.get("sender") or "",
                          subject=args.get("subject") or "", since=args.get("since") or "", unseen=bool(args.get("unseen")),
                          limit=int(args.get("limit") or 10))
        if not rows:
            return "(no matching emails)"
        return redact("\n".join(f"{r['uid']}\t{r['date'][:25]}\t{r['from'][:60]}\t{'● ' if r['unread'] else ''}{r['subject'][:120]}"
                                for r in rows))

    @reg.tool(
        "email_read",
        "Read one email by uid (from email_search): headers, the text, and its attachments. save_attachments=true "
        "saves them to workspace/inbox/email/ so read_file can open them. Does not mark it read unless mark_read.",
        {"type": "object", "properties": {
            "uid": {"type": "string"}, "folder": {"type": "string"}, "save_attachments": {"type": "boolean"},
            "mark_read": {"type": "boolean"},
        }, "required": ["uid"]},
    )
    def _read(args: dict[str, Any]) -> str:
        folder = args.get("folder") or "INBOX"
        msg = box.fetch(str(args.get("uid")), folder, mark_read=bool(args.get("mark_read")))
        lines = [f"{k}: {_dec(msg.get(k))}" for k in ("From", "To", "Cc", "Date", "Subject", "Message-ID") if msg.get(k)]
        files = attachments(msg)
        if files:
            if args.get("save_attachments"):
                from .channels import safe_filename, unique_path
                folder_p = ws.root / "inbox" / "email" / time.strftime("%Y-%m-%d")
                folder_p.mkdir(parents=True, exist_ok=True)
                saved = []
                for name, data in files:
                    p = unique_path(folder_p, safe_filename(name))
                    p.write_bytes(data)
                    saved.append(f"{ws.show(p)} ({max(1, len(data) // 1024)} KB)")
                lines.append("Attachments saved: " + ", ".join(saved))
            else:
                lines.append("Attachments: " + ", ".join(f"{n} ({max(1, len(d) // 1024)} KB)" for n, d in files)
                             + " — save_attachments=true to open them")
        return redact("\n".join(lines) + "\n\n" + _cap(message_text(msg).strip(), SHOW_CHARS))

    @reg.tool(
        "email_send",
        "Send an email from the owner's address (asks the owner first, showing recipients, subject and text). "
        "to/cc/bcc: lists of addresses; body: plain text (Markdown is fine); attachments: file paths; "
        "reply_to_uid (+ folder): answer that email in its thread (to defaults to its sender, subject gets 'Re:').",
        {"type": "object", "properties": {
            "to": {"type": "array", "items": {"type": "string"}}, "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}}, "subject": {"type": "string"}, "body": {"type": "string"},
            "attachments": {"type": "array", "items": {"type": "string"}}, "reply_to_uid": {"type": "string"},
            "folder": {"type": "string"},
        }, "required": ["body"]},
    )
    def _send(args: dict[str, Any]) -> str:
        def addrs(v: Any) -> list[str]:
            items = [v] if isinstance(v, str) else list(v or [])
            out = [formataddr(a) for a in getaddresses([str(x) for x in items]) if "@" in a[1]]
            return out

        to, cc, bcc = addrs(args.get("to")), addrs(args.get("cc")), addrs(args.get("bcc"))
        subject = str(args.get("subject") or "")
        msg = EmailMessage()
        if args.get("reply_to_uid"):
            orig = box.fetch(str(args["reply_to_uid"]), args.get("folder") or "INBOX")
            if not to:
                to = addrs(orig.get("Reply-To") or orig.get("From"))
            osub = _dec(orig.get("Subject"))
            subject = subject or (osub if osub.lower().startswith("re:") else f"Re: {osub}")
            if orig.get("Message-ID"):
                msg["In-Reply-To"] = orig["Message-ID"]
                msg["References"] = f"{orig.get('References', '')} {orig['Message-ID']}".strip()
        if not to:
            raise ToolError("to is required (or reply_to_uid)")
        msg["From"] = es.from_addr or es.user
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject or "(no subject)"
        msg["Message-ID"] = make_msgid(domain=(parseaddr(es.user)[1].rsplit("@", 1)[-1] or None))
        msg.set_content(str(args.get("body") or ""))
        names = []
        for rel in args.get("attachments") or []:
            p = ws.resolve(str(rel))
            if not p.is_file():
                raise ToolError(f"no such file: {rel}")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            main, _, sub = ctype.partition("/")
            msg.add_attachment(p.read_bytes(), maintype=main, subtype=sub, filename=p.name)
            names.append(p.name)
        preview = {"to": to, "cc": cc, "bcc": bcc, "subject": msg["Subject"], "body": str(args.get("body") or "")[:600],
                   "attachments": names}
        if not _ask(reg, "email_send", preview):
            return "denied: the owner did not approve sending this email. Do not retry it."
        send_mail(es, msg, smtp)
        if es.sent_folder:
            try:
                box.append(es.sent_folder, bytes(msg))
            except Exception:
                pass
        return f"sent to {', '.join(to + cc)}" + (f" with {len(names)} attachment(s)" if names else "")


# ---------------------------------------------------------------- git

GIT_READ = {"status", "log", "diff", "show", "branch", "remote"}
GIT_WRITE = {"add", "commit", "pull", "push", "clone", "checkout", "init", "fetch"}


def _safe_ref(v: Any, what: str) -> str:
    v = str(v or "").strip()
    if v.startswith("-") or any(c in v for c in "\n\r\x00"):
        raise ToolError(f"{what} looks like an option, not a {what}")
    return v


def _git_env(token: str) -> dict[str, str]:
    env = scrubbed_env()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"})
    if token:  # GitHub over https without writing the token into .git/config or the command line
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}"})
    return env


def run_git(argv: list[str], cwd: Path, token: str = "", timeout: int = 300) -> str:
    if not shutil.which("git"):
        raise ToolError("git is not installed on this machine")
    try:
        r = subprocess.run(["git", *argv], cwd=str(cwd), env=_git_env(token), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"git {argv[0]} took longer than {timeout} s") from None
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode:
        raise ToolError(redact(f"git {argv[0]} failed ({r.returncode}): {_cap(out, 4000)}"))
    return redact(_cap(out, SHOW_CHARS)) or "(done, no output)"


def git_argv(action: str, a: dict[str, Any]) -> list[str]:
    paths = [_safe_ref(p, "path") for p in (a.get("paths") or [])]
    if action == "status":
        return ["status", "--short", "--branch"]
    if action == "log":
        n = max(1, min(int(a.get("limit") or 20), 200))
        return ["log", f"-n{n}", "--date=short", "--pretty=format:%h %ad %an  %s", *(["--", *paths] if paths else [])]
    if action == "diff":
        ref = _safe_ref(a.get("ref"), "ref")
        return ["diff", "--stat", "--patch", *(["--cached"] if a.get("staged") else []), *([ref] if ref else []),
                *(["--", *paths] if paths else [])]
    if action == "show":
        return ["show", "--stat", "--patch", _safe_ref(a.get("ref") or "HEAD", "ref")]
    if action == "branch":
        name = _safe_ref(a.get("name"), "branch name")
        return ["branch", name] if name else ["branch", "-a", "-vv"]
    if action == "remote":
        return ["remote", "-v"]
    if action == "add":
        return ["add", "--", *(paths or ["."])]
    if action == "commit":
        if not str(a.get("message") or "").strip():
            raise ToolError("message is required for commit")
        return ["commit", *(["-a"] if a.get("all") else []), "-m", str(a["message"])]
    if action == "pull":
        return ["pull", "--ff-only", *([_safe_ref(a["remote"], "remote")] if a.get("remote") else []),
                *([_safe_ref(a["ref"], "ref")] if a.get("ref") else [])]
    if action == "fetch":
        return ["fetch", "--prune", *([_safe_ref(a["remote"], "remote")] if a.get("remote") else [])]
    if action == "push":
        argv = ["push"]
        if a.get("set_upstream"):
            argv.append("-u")
        return argv + [_safe_ref(a.get("remote") or "origin", "remote"), *([_safe_ref(a["ref"], "ref")] if a.get("ref") else [])]
    if action == "checkout":
        ref = _safe_ref(a.get("ref"), "ref")
        if not ref:
            raise ToolError("ref (branch, tag or commit) is required")
        return ["checkout", *(["-b"] if a.get("create") else []), ref]
    if action == "init":
        return ["init"]
    raise ToolError(f"unknown git action '{action}'")


def add_git_tool(reg: Registry, ws: Workspace, *, token_env: str = "GITHUB_TOKEN") -> None:
    @reg.tool(
        "git",
        "Work with git repositories without the terminal. Read (free): status, log (limit, paths), diff (staged, ref, "
        "paths), show (ref), branch (list), remote. Change (asks the owner): add (paths), commit (message, all), "
        "pull, fetch, push (remote, ref, set_upstream), checkout (ref, create), branch (name = create), init, "
        "clone (url, dest). repo = the repository folder (default: the workspace). GitHub over https uses "
        "GITHUB_TOKEN from the environment; force-push and history rewrites are not offered.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": sorted(GIT_READ | GIT_WRITE | {"clone"})},
            "repo": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}},
            "ref": {"type": "string"}, "message": {"type": "string"}, "all": {"type": "boolean"},
            "staged": {"type": "boolean"}, "limit": {"type": "integer"}, "name": {"type": "string"},
            "remote": {"type": "string"}, "set_upstream": {"type": "boolean"}, "create": {"type": "boolean"},
            "url": {"type": "string"}, "dest": {"type": "string"},
        }, "required": ["action"]},
    )
    def _git(args: dict[str, Any]) -> str:
        action = str(args.get("action") or "")
        token = os.environ.get(token_env, "")
        if action == "clone":
            url = str(args.get("url") or "")
            if not re.match(r"^(https://|git@|ssh://)", url):
                raise ToolError("url must be https://…, ssh://… or git@host:owner/repo")
            dest = ws.resolve(str(args.get("dest") or Path(urllib.parse.urlparse(url).path or url).stem or "repo"), write=True)
            if dest.exists() and any(dest.iterdir()):
                raise ToolError(f"{ws.show(dest)} exists and is not empty")
            if not _ask(reg, "git", {"command": f"git clone {url} {ws.show(dest)}"}):
                return "denied: the owner did not approve this git command."
            dest.parent.mkdir(parents=True, exist_ok=True)
            return run_git(["clone", "--", url, str(dest)], dest.parent, token) + f"\ncloned into {ws.show(dest)}"
        repo = ws.resolve(str(args.get("repo") or "."), write=action in GIT_WRITE)
        if not repo.is_dir():
            raise ToolError(f"no such folder: {args.get('repo')}")
        argv = git_argv(action, args)
        write = action in GIT_WRITE or (action == "branch" and args.get("name"))
        if write and not _ask(reg, "git", {"command": "git " + " ".join(argv), "repo": ws.show(repo)}):
            return "denied: the owner did not approve this git command."
        return run_git(argv, repo, token if action in ("pull", "push", "fetch") else "")


# ---------------------------------------------------------------- GitHub

class GitHub:
    def __init__(self, token: str = "", api: str = "https://api.github.com",
                 http: Callable[..., tuple[int, Any]] | None = None):
        self.token, self.api = token, api.rstrip("/")
        self._http = http

    def call(self, method: str, path: str, body: Any = None, *, accept: str = "application/vnd.github+json") -> Any:
        url = path if path.startswith("https://") else f"{self.api}{path}"
        head = {"accept": accept, "user-agent": UA, "x-github-api-version": "2022-11-28"}
        if self.token:
            head["authorization"] = f"Bearer {self.token}"
        if self._http:
            status, data = self._http(method, url, head, body)
        else:
            req = urllib.request.Request(url, method=method, headers={**head, **({"content-type": "application/json"} if body is not None else {})},
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                    raw = r.read(MAX_BODY)
                    status = r.status
            except urllib.error.HTTPError as e:
                raw, status = e.read(MAX_BODY), e.code
            except (urllib.error.URLError, OSError) as e:
                raise ToolError(f"GitHub unreachable: {e}") from None
            text = raw.decode("utf-8", "replace")
            try:
                data = json.loads(text) if "json" in accept else text
            except ValueError:
                data = text
        if status >= 400:
            msg = data.get("message") if isinstance(data, dict) else str(data)[:200]
            hint = " (set GITHUB_TOKEN in ~/.smooth-agent/.env)" if status in (401, 403, 404) and not self.token else ""
            raise ToolError(f"GitHub {status}: {msg}{hint}")
        return data


def _repo(v: Any) -> str:
    v = str(v or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", v):
        raise ToolError("repo must be owner/name")
    return v


def _issue_line(i: dict) -> str:
    kind = "PR" if "pull_request" in i or "head" in i else "issue"
    labels = ",".join(lbl.get("name", "") for lbl in i.get("labels") or [])
    return (f"#{i.get('number')} [{i.get('state')}] {kind} {i.get('title')} — @{(i.get('user') or {}).get('login', '?')}"
            f"{' {' + labels + '}' if labels else ''} ({str(i.get('updated_at', ''))[:10]})")


def github_action(gh: GitHub, a: dict[str, Any]) -> str:
    action = str(a.get("action") or "")
    n = max(1, min(int(a.get("limit") or 20), 100))
    if action == "repo":
        r = gh.call("GET", f"/repos/{_repo(a.get('repo'))}")
        return (f"{r['full_name']} — {r.get('description') or ''}\n★ {r.get('stargazers_count')}  forks {r.get('forks_count')}  "
                f"open issues {r.get('open_issues_count')}  default branch {r.get('default_branch')}  "
                f"{'private' if r.get('private') else 'public'}  pushed {str(r.get('pushed_at'))[:10]}\n{r.get('html_url')}")
    if action in ("issues", "prs"):
        state = a.get("state") or "open"
        q = urllib.parse.urlencode({"state": state, "per_page": n, **({"labels": a["labels"]} if a.get("labels") else {})})
        path = f"/repos/{_repo(a.get('repo'))}/{'pulls' if action == 'prs' else 'issues'}?{q}"
        rows = gh.call("GET", path)
        rows = [i for i in rows if action == "prs" or "pull_request" not in i]
        return "\n".join(_issue_line(i) for i in rows) or f"(no {state} {action})"
    if action in ("issue", "pr"):
        repo, num = _repo(a.get("repo")), int(a.get("number") or 0)
        i = gh.call("GET", f"/repos/{repo}/{'pulls' if action == 'pr' else 'issues'}/{num}")
        out = [_issue_line(i), str(i.get("html_url")), "", _cap(str(i.get("body") or "(no description)"), 8000)]
        if action == "pr":
            out.insert(1, f"{i.get('head', {}).get('ref')} → {i.get('base', {}).get('ref')}  +{i.get('additions')} -{i.get('deletions')} "
                          f"in {i.get('changed_files')} files  mergeable: {i.get('mergeable_state')}")
            files = gh.call("GET", f"/repos/{repo}/pulls/{num}/files?per_page=100")
            out += ["", "Files:"] + [f"  {f.get('status')} {f.get('filename')} +{f.get('additions')} -{f.get('deletions')}" for f in files]
            if a.get("diff"):
                out += ["", _cap(str(gh.call("GET", f"/repos/{repo}/pulls/{num}", accept="application/vnd.github.v3.diff")), 20000)]
        comments = gh.call("GET", f"/repos/{repo}/issues/{num}/comments?per_page=30")
        if comments:
            out += ["", f"Comments ({len(comments)}):"] + [
                f"— @{(c.get('user') or {}).get('login')} {str(c.get('created_at'))[:10]}: {_cap(str(c.get('body') or ''), 1500)}" for c in comments]
        return "\n".join(out)
    if action == "search":
        kind = a.get("type") or "issues"
        if kind not in ("issues", "repositories", "code"):
            raise ToolError("type: issues, repositories or code")
        r = gh.call("GET", f"/search/{kind}?" + urllib.parse.urlencode({"q": str(a.get("query") or ""), "per_page": n}))
        items = r.get("items") or []
        if kind == "repositories":
            return "\n".join(f"{x['full_name']} ★{x.get('stargazers_count')} — {x.get('description') or ''}" for x in items) or "(nothing)"
        if kind == "code":
            return "\n".join(f"{x['repository']['full_name']}: {x['path']}" for x in items) or "(nothing)"
        return "\n".join(f"{x.get('repository_url', '').split('/repos/')[-1]} {_issue_line(x)}" for x in items) or "(nothing)"
    if action == "notifications":
        rows = gh.call("GET", f"/notifications?per_page={n}")
        return "\n".join(f"{x['repository']['full_name']}: {x['subject']['type']} {x['subject']['title']} ({x['reason']})"
                         for x in rows) or "(no unread notifications)"
    raise ToolError(f"unknown action '{action}'")


GITHUB_WRITE = {"create_issue", "comment", "create_pr", "close", "reopen"}


def github_write(gh: GitHub, a: dict[str, Any]) -> str:
    action, repo = str(a.get("action")), _repo(a.get("repo"))
    if action == "create_issue":
        r = gh.call("POST", f"/repos/{repo}/issues", {"title": str(a.get("title") or ""), "body": str(a.get("body") or ""),
                                                      **({"labels": a["labels"].split(",") if isinstance(a["labels"], str) else a["labels"]}
                                                         if a.get("labels") else {})})
        return f"opened #{r['number']}: {r['html_url']}"
    num = int(a.get("number") or 0)
    if action == "comment":
        r = gh.call("POST", f"/repos/{repo}/issues/{num}/comments", {"body": str(a.get("body") or "")})
        return f"commented: {r.get('html_url')}"
    if action in ("close", "reopen"):
        r = gh.call("PATCH", f"/repos/{repo}/issues/{num}", {"state": "closed" if action == "close" else "open"})
        return f"#{num} is {r.get('state')}"
    if action == "create_pr":
        r = gh.call("POST", f"/repos/{repo}/pulls", {"title": str(a.get("title") or ""), "body": str(a.get("body") or ""),
                                                     "head": str(a.get("head") or ""), "base": str(a.get("base") or "main"),
                                                     "draft": bool(a.get("draft"))})
        return f"opened PR #{r['number']}: {r['html_url']}"
    raise ToolError(f"unknown action '{action}'")


def add_github_tool(reg: Registry, *, token_env: str = "GITHUB_TOKEN", api: str = "https://api.github.com",
                    gh: GitHub | None = None) -> None:
    @reg.tool(
        "github",
        "GitHub without the browser. Read: repo, issues / prs (state, labels, limit), issue / pr (number; pr diff=true "
        "for the patch), search (query, type issues|repositories|code), notifications. Write (asks the owner): "
        "create_issue (title, body, labels), comment (number, body), close / reopen (number), create_pr (head, base, "
        "title, body, draft). repo = owner/name. Uses GITHUB_TOKEN from the environment (public reads work without).",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["repo", "issues", "prs", "issue", "pr", "search", "notifications",
                                                  *sorted(GITHUB_WRITE)]},
            "repo": {"type": "string"}, "number": {"type": "integer"}, "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "labels": {"type": "string"}, "limit": {"type": "integer"}, "diff": {"type": "boolean"},
            "query": {"type": "string"}, "type": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
            "head": {"type": "string"}, "base": {"type": "string"}, "draft": {"type": "boolean"},
        }, "required": ["action"]},
    )
    def _gh(args: dict[str, Any]) -> str:
        client = gh or GitHub(os.environ.get(token_env, ""), api)
        if str(args.get("action")) in GITHUB_WRITE:
            preview = {k: args.get(k) for k in ("action", "repo", "number", "title", "head", "base") if args.get(k)}
            preview["body"] = str(args.get("body") or "")[:600]
            if not _ask(reg, "github", preview):
                return "denied: the owner did not approve this. Do not retry it."
            return redact(github_write(client, args))
        return redact(github_action(client, args))


# ---------------------------------------------------------------- SQLite

def sql_query(path: Path, sql: str, limit: int = 200) -> str:
    if not path.is_file():
        raise ToolError(f"no such database file: {path}")
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as e:
        raise ToolError(f"cannot open {path.name}: {e}") from None
    try:
        con.execute("PRAGMA query_only = ON")
        if not str(sql or "").strip():
            rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                               "ORDER BY name").fetchall()
            out = []
            for name, ddl in rows:
                try:
                    n = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                except sqlite3.Error:
                    n = "?"
                out.append(f"{name} ({n} rows)\n  {ddl}")
            return "\n".join(out) or "(empty database)"
        cur = con.execute(sql)
        if cur.description is None:
            return "(no rows)"
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max(1, min(int(limit or 200), 2000)) + 1)
        more = len(rows) > int(limit or 200)
        rows = rows[: int(limit or 200)]
        cell = lambda v: "" if v is None else (f"<{len(v)} bytes>" if isinstance(v, (bytes, bytearray)) else str(v).replace("\t", " ").replace("\n", " ")[:300])
        lines = ["\t".join(cols)] + ["\t".join(cell(v) for v in r) for r in rows]
        return redact(_cap("\n".join(lines), SHOW_CHARS)) + (f"\n...(more rows; raise limit or add LIMIT/WHERE)" if more else f"\n({len(rows)} rows)")
    except sqlite3.Error as e:
        raise ToolError(f"SQL error: {e}") from None
    finally:
        con.close()


# ---------------------------------------------------------------- Postgres / MySQL (read-only)

READONLY_SQL = re.compile(r"^\s*(select|with|show|explain|desc|describe|table)\b", re.I)


@dataclass
class SqlConn:
    name: str
    backend: str  # "postgres" | "mysql"
    host: str = "localhost"
    port: int = 0
    user: str = ""
    password: str = ""
    database: str = ""


def sql_connections(cfg: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, SqlConn]:
    """Named read-only database connections from [sql.<name>] (url + optional password_env)."""
    env = env if env is not None else dict(os.environ)
    out: dict[str, SqlConn] = {}
    for name, c in (cfg.get("sql") or {}).items():
        if not isinstance(c, dict) or not c.get("url"):
            continue
        u = urllib.parse.urlparse(str(c["url"]))
        backend = "postgres" if u.scheme.startswith(("postgres", "pg")) else ("mysql" if u.scheme.startswith("mysql") else "")
        if not backend:
            continue
        pw = str(c.get("password") or (env.get(str(c["password_env"]), "") if c.get("password_env") else "") or
                 (urllib.parse.unquote(u.password) if u.password else ""))
        out[str(name)] = SqlConn(name=str(name), backend=backend, host=u.hostname or "localhost",
                                 port=u.port or (5432 if backend == "postgres" else 3306),
                                 user=urllib.parse.unquote(u.username or ""), password=pw,
                                 database=(u.path or "/").lstrip("/"))
    return out


def _guard_readonly(sql: str) -> str:
    s = str(sql or "").strip().rstrip(";").strip()
    if ";" in s:
        raise ToolError("run one statement at a time (no ';')")
    if not READONLY_SQL.match(s):
        raise ToolError("read-only: only SELECT / WITH / SHOW / EXPLAIN / DESCRIBE are allowed")
    return s


def _open_remote(conn: SqlConn):  # pragma: no cover - exercised with a fake in tests
    if conn.backend == "postgres":
        try:
            import pg8000.dbapi as driver
        except ImportError:
            raise ToolError("Postgres needs the pg8000 driver: pip install \"smooth-agent[postgres]\"") from None
        return driver.connect(user=conn.user, password=conn.password, host=conn.host, port=conn.port,
                              database=conn.database or None)
    try:
        import pymysql as driver  # type: ignore
    except ImportError:
        raise ToolError("MySQL needs the PyMySQL driver: pip install \"smooth-agent[mysql]\"") from None
    return driver.connect(host=conn.host, port=conn.port, user=conn.user, password=conn.password,
                          database=conn.database or None, read_default_file="")


def remote_query(conn: SqlConn, sql: str, limit: int = 200, *, opener=_open_remote) -> str:
    con = opener(conn)
    try:
        cur = con.cursor()
        if conn.backend == "postgres":
            cur.execute("SET TRANSACTION READ ONLY")
        else:
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if not str(sql or "").strip():
            if conn.backend == "postgres":
                cur.execute("SELECT table_schema||'.'||table_name FROM information_schema.tables "
                            "WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1")
            else:
                cur.execute("SHOW TABLES")
            names = [str(r[0]) for r in cur.fetchall()]
            return "\n".join(names) or "(no tables)"
        cur.execute(_guard_readonly(sql))
        if cur.description is None:
            return "(no rows)"
        cols = [d[0] for d in cur.description]
        n = max(1, min(int(limit or 200), 2000))
        rows = cur.fetchmany(n + 1)
        more = len(rows) > n
        rows = rows[:n]
        cell = lambda v: "" if v is None else (f"<{len(v)} bytes>" if isinstance(v, (bytes, bytearray)) else str(v).replace("\t", " ").replace("\n", " ")[:300])
        lines = ["\t".join(cols)] + ["\t".join(cell(v) for v in r) for r in rows]
        return redact(_cap("\n".join(lines), SHOW_CHARS)) + ("\n...(more rows; raise limit or add LIMIT/WHERE)" if more else f"\n({len(rows)} rows)")
    except ToolError:
        raise
    except Exception as e:  # driver-specific errors: surface the message, not a traceback
        raise ToolError(f"SQL error: {str(e)[:300]}") from None
    finally:
        try:
            con.close()
        except Exception:
            pass


def add_sql_tool(reg: Registry, ws: Workspace, cfg: dict[str, Any] | None = None) -> None:
    conns = sql_connections(cfg or {})
    names = ", ".join(sorted(conns)) or "none configured"

    @reg.tool(
        "sql_query",
        "Query a database read-only. A SQLite file: give path (.db/.sqlite); empty sql lists its tables with columns "
        f"and row counts. A configured Postgres/MySQL connection: give connection ({names}); empty sql lists its "
        "tables. Only SELECT / WITH / SHOW / EXPLAIN / DESCRIBE run, one statement at a time. Tab-separated (limit, "
        "default 200). Nothing can be changed.",
        {"type": "object", "properties": {"path": {"type": "string"}, "connection": {"type": "string"},
                                          "sql": {"type": "string"}, "limit": {"type": "integer"}}},
    )
    def _sql(args: dict[str, Any]) -> str:
        name = str(args.get("connection") or "").strip()
        sql, limit = str(args.get("sql") or ""), int(args.get("limit") or 200)
        if name:
            if name not in conns:
                raise ToolError(f"no connection '{name}' (configured: {names})")
            return remote_query(conns[name], sql, limit)
        if not str(args.get("path") or "").strip():
            raise ToolError("give a SQLite path, or a configured connection for Postgres/MySQL")
        return sql_query(ws.resolve(str(args["path"])), sql, limit)


# ---------------------------------------------------------------- wiring

def add_connect_tools(reg: Registry, ws: Workspace, cfg: dict[str, Any], *, http: bool = True) -> list[str]:
    """Register what this config allows; returns notes for `doctor`."""
    notes = []
    gh_cfg = dict(cfg.get("github") or {})
    token_env = str(gh_cfg.get("token_env") or "GITHUB_TOKEN")
    if http:
        add_http_request_tool(reg)
        add_github_tool(reg, token_env=token_env, api=str(gh_cfg.get("api") or "https://api.github.com"))
        notes.append(f"github: {'token set' if os.environ.get(token_env) else 'no token (public reads only)'}")
    add_git_tool(reg, ws, token_env=token_env)
    add_sql_tool(reg, ws, cfg)
    sc = sql_connections(cfg)
    if sc:
        notes.append("sql: " + ", ".join(sorted(sc)))
    es = EmailSettings.from_config(cfg)
    if es.configured:
        add_email_tools(reg, ws, es)
        notes.append(f"email: {es.user} via {es.imap_host}")
    return notes


__all__ = ["add_connect_tools", "add_http_request_tool", "add_email_tools", "add_git_tool", "add_github_tool",
           "add_sql_tool", "http_request", "fill_env", "EmailSettings", "Mailbox", "GitHub", "github_action",
           "github_write", "git_argv", "run_git", "sql_query", "send_mail", "message_text",
           "sql_connections", "remote_query", "SqlConn", "_guard_readonly"]
