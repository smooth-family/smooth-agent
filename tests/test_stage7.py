"""Stage 7: http_request, email, git, GitHub, sql_query and web_watch."""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from smooth_agent import connect, watch
from smooth_agent.connect import (EmailSettings, GitHub, Mailbox, add_email_tools, add_git_tool, add_github_tool,
                                  add_http_request_tool, add_sql_tool, fill_env, git_argv, message_text)
from smooth_agent.tools import Registry, ToolError, Workspace


def registry(answer=True):
    asked = []
    reg = Registry(lambda name, args: asked.append((name, args)) or answer)
    return reg, asked


# ---------------------------------------------------------------- http_request

class _Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {"path": self.path, "auth": self.headers.get("authorization")})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        self._reply(201, {"got": json.loads(self.rfile.read(n) or b"null"), "auth": self.headers.get("authorization")})


class HttpRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_fill_env(self):
        self.assertEqual(fill_env({"h": "Bearer {{env:T}}", "x": ["{{ env:T }}"]}, {"T": "abc"}), {"h": "Bearer abc", "x": ["abc"]})
        with self.assertRaisesRegex(ToolError, "NOPE is not set"):
            fill_env("{{env:NOPE}}", {})

    def test_get_free_post_asks_and_secret_never_shown(self):
        reg, asked = registry(True)
        add_http_request_tool(reg)
        with mock.patch.dict(os.environ, {"MY_TOKEN": "sk-proj-ABCDEFGHIJKLMNOPQRST123"}):
            out = reg.run("http_request", {"url": self.url + "/items", "query": {"q": "x y"},
                                           "headers": {"Authorization": "Bearer {{env:MY_TOKEN}}"}})
            self.assertIn("HTTP 200", out)
            self.assertIn('"/items?q=x+y"', out)
            self.assertNotIn("ABCDEFGHIJKLMNOPQRST123", out, "the echoed token is redacted")
            self.assertEqual(asked, [], "GET does not ask")
            out = reg.run("http_request", {"method": "POST", "url": self.url + "/items", "json": {"name": "kopi"}})
            self.assertIn("HTTP 201", out)
            self.assertIn('"name": "kopi"', out)
            self.assertEqual(asked[0][0], "http_request")
            self.assertEqual(asked[0][1]["method"], "POST")
        reg2, _ = registry(False)
        add_http_request_tool(reg2)
        self.assertIn("denied", reg2.run("http_request", {"method": "DELETE", "url": self.url + "/items/1"}))


# ---------------------------------------------------------------- email

def mail(uid, subject, frm="Budi <budi@example.com>", body="halo", html=None, attach=None, seen=False):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = frm, "me@example.com", subject, "Mon, 02 Nov 2026 10:00:00 +0700"
    m["Message-ID"] = f"<m{uid}@example.com>"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attach:
        m.add_attachment(attach[1], maintype="application", subtype="pdf", filename=attach[0])
    return m


class FakeIMAP:
    def __init__(self, msgs):
        self.msgs = msgs  # uid -> (EmailMessage, seen)
        self.searched = []
        self.literal = None
        self.appended = []

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"]) if mailbox.strip('"') in ("INBOX", "Sent") else ("NO", [b"no such folder"])

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "[Gmail]/Sent Mail"']

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searched.append((args, self.literal))
            self.literal = None
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.msgs))]
        uid = int(args[0])
        msg, seen = self.msgs[uid]
        if "HEADER.FIELDS" in args[1]:
            hdr = "".join(f"{k}: {msg[k]}\r\n" for k in ("From", "Subject", "Date")).encode() + b"\r\n"
            flags = "\\Seen" if seen else ""
            return "OK", [(f"{uid} (UID {uid} FLAGS ({flags}) RFC822.SIZE 100".encode(), hdr), b")"]
        return "OK", [(b"x", bytes(msg)), b")"]

    def append(self, *a):
        self.appended.append(a)

    def logout(self):
        pass


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.tmp)
        self.imap = FakeIMAP({
            5: (mail(5, "Invoice November", attach=("inv.pdf", b"%PDF-1.4")), True),
            7: (mail(7, "Rapat besok", body="", html="<p>Jam <b>9</b> pagi</p>"), False),
        })
        self.es = EmailSettings(user="me@example.com", password="pw", imap_host="imap.example.com", smtp_host="smtp.example.com")
        self.sent = []

        class FakeSMTP:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def login(s, u, p):
                pass

            def send_message(s, msg):
                self.sent.append(msg)

        self.reg, self.asked = registry(True)
        add_email_tools(self.reg, self.ws, self.es, mailbox=Mailbox(self.es, connect=lambda es: self.imap),
                        smtp=lambda es: FakeSMTP())

    def test_presets(self):
        es = EmailSettings.from_config({"email": {"user": "a@gmail.com"}}, env={"EMAIL_PASSWORD": "x"})
        self.assertEqual((es.imap_host, es.smtp_host, es.smtp_port, es.configured), ("imap.gmail.com", "smtp.gmail.com", 587, True))
        self.assertFalse(EmailSettings.from_config({}, env={}).configured)

    def test_search_newest_first_and_unicode_literal(self):
        out = self.reg.run("email_search", {"since": "7d", "unseen": True})
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("7\t") and "● Rapat besok" in lines[0])
        self.assertTrue(lines[1].startswith("5\t") and "●" not in lines[1])
        self.assertEqual(self.imap.searched[0][0][0], "UNSEEN")
        self.reg.run("email_search", {"text": "tagihan Rp5.000 — lunas"})
        args, literal = self.imap.searched[-1]
        self.assertEqual(args[:2], ("CHARSET", "UTF-8"))
        self.assertEqual(literal, "tagihan Rp5.000 — lunas".encode())
        self.assertIn("[Gmail]/Sent Mail", self.reg.run("email_search", {"folders": True}))

    def test_read_html_and_save_attachment(self):
        out = self.reg.run("email_read", {"uid": "7"})
        self.assertIn("Subject: Rapat besok", out)
        self.assertIn("Jam 9 pagi", out)
        out = self.reg.run("email_read", {"uid": "5", "save_attachments": True})
        self.assertIn("Attachments saved:", out)
        saved = list((self.tmp / "inbox" / "email").rglob("inv.pdf"))
        self.assertEqual(saved[0].read_bytes(), b"%PDF-1.4")
        self.assertIn("uid must be", self.reg.run("email_read", {"uid": "1 OR 1"}))

    def test_send_reply_in_thread_with_approval(self):
        (self.tmp / "laporan.xlsx").write_bytes(b"PK")
        out = self.reg.run("email_send", {"reply_to_uid": "5", "body": "Sudah dibayar, terima kasih.", "attachments": ["laporan.xlsx"]})
        self.assertIn("sent to Budi <budi@example.com>", out)
        msg = self.sent[0]
        self.assertEqual(msg["Subject"], "Re: Invoice November")
        self.assertEqual(msg["In-Reply-To"], "<m5@example.com>")
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["laporan.xlsx"])
        name, preview = self.asked[-1]
        self.assertEqual((name, preview["to"], preview["attachments"]), ("email_send", ["Budi <budi@example.com>"], ["laporan.xlsx"]))
        reg, _ = registry(False)
        add_email_tools(reg, self.ws, self.es, mailbox=Mailbox(self.es, connect=lambda es: self.imap), smtp=lambda es: None)
        self.assertIn("denied", reg.run("email_send", {"to": ["x@example.com"], "subject": "s", "body": "b"}))
        self.assertEqual(len(self.sent), 1)

    def test_message_text_prefers_plain(self):
        self.assertEqual(message_text(mail(1, "s", body="plain wins", html="<p>html</p>")).strip(), "plain wins")


# ---------------------------------------------------------------- git

@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.tmp)
        self.env = mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
                                                "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_read_free_write_asks(self):
        reg, asked = registry(True)
        add_git_tool(reg, self.ws)
        (self.tmp / "proj").mkdir()
        self.assertNotIn("Error", reg.run("git", {"action": "init", "repo": "proj"}))
        (self.tmp / "proj" / "a.txt").write_text("hello\n")
        status = reg.run("git", {"action": "status", "repo": "proj"})
        self.assertIn("?? a.txt", status)
        n_asked = len(asked)
        reg.run("git", {"action": "add", "repo": "proj", "paths": ["a.txt"]})
        reg.run("git", {"action": "commit", "repo": "proj", "message": "first"})
        self.assertEqual(len(asked), n_asked + 2)
        self.assertIn("git commit -m first", asked[-1][1]["command"])
        self.assertIn("first", reg.run("git", {"action": "log", "repo": "proj"}))
        self.assertEqual(len(asked), n_asked + 2, "log does not ask")

    def test_denied_and_option_injection(self):
        reg, _ = registry(False)
        add_git_tool(reg, self.ws)
        self.assertIn("denied", reg.run("git", {"action": "init"}))
        with self.assertRaisesRegex(ToolError, "option"):
            git_argv("checkout", {"ref": "--orphan"})
        with self.assertRaisesRegex(ToolError, "option"):
            git_argv("diff", {"paths": ["--output=/etc/x"]})
        self.assertIn("url must be", reg.run("git", {"action": "clone", "url": "file:///etc"}))

    def test_token_goes_in_env_not_argv(self):
        env = connect._git_env("ghp_TESTTOKEN")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic "))
        self.assertNotIn("ghp_TESTTOKEN", " ".join(git_argv("push", {"remote": "origin", "ref": "main"})))


# ---------------------------------------------------------------- GitHub

class GitHubTests(unittest.TestCase):
    def fake(self):
        calls = []

        def http(method, url, head, body):
            calls.append((method, url, body))
            if url.endswith("/issues?state=open&per_page=20"):
                return 200, [{"number": 1, "title": "Bug", "state": "open", "user": {"login": "a"}, "labels": [{"name": "bug"}]},
                             {"number": 2, "title": "PR", "state": "open", "user": {"login": "b"}, "pull_request": {}}]
            if method == "POST":
                return 201, {"number": 9, "html_url": "https://github.com/o/r/issues/9"}
            return 404, {"message": "Not Found"}
        return GitHub("", http=http), calls

    def test_issues_skip_prs_and_writes_ask(self):
        gh, calls = self.fake()
        reg, asked = registry(True)
        add_github_tool(reg, gh=gh)
        out = reg.run("github", {"action": "issues", "repo": "https://github.com/o/r.git"})
        self.assertIn("#1 [open] issue Bug", out)
        self.assertNotIn("#2", out)
        self.assertIn("opened #9", reg.run("github", {"action": "create_issue", "repo": "o/r", "title": "T", "body": "B", "labels": "bug,ui"}))
        self.assertEqual(calls[-1][2]["labels"], ["bug", "ui"])
        self.assertEqual(asked[-1][1]["title"], "T")
        self.assertIn("GITHUB_TOKEN", reg.run("github", {"action": "repo", "repo": "o/private"}))
        reg2, _ = registry(False)
        add_github_tool(reg2, gh=gh)
        self.assertIn("denied", reg2.run("github", {"action": "comment", "repo": "o/r", "number": 1, "body": "x"}))
        self.assertIn("owner/name", reg.run("github", {"action": "repo", "repo": "bad"}))


# ---------------------------------------------------------------- SQLite

class SqlTests(unittest.TestCase):
    def test_read_only(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "shop.db"
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE items(id INTEGER, name TEXT, price REAL); "
                          "INSERT INTO items VALUES (1,'kopi',15000),(2,'teh',8000);")
        con.commit()
        con.close()
        reg, _ = registry()
        add_sql_tool(reg, Workspace(tmp))
        self.assertIn("items (2 rows)", reg.run("sql_query", {"path": "shop.db"}))
        out = reg.run("sql_query", {"path": "shop.db", "sql": "SELECT name, price FROM items ORDER BY price"})
        self.assertEqual(out.splitlines()[:3], ["name\tprice", "teh\t8000.0", "kopi\t15000.0"])
        self.assertIn("readonly", reg.run("sql_query", {"path": "shop.db", "sql": "DELETE FROM items"}).lower())
        self.assertEqual(sqlite3.connect(db).execute("SELECT count(*) FROM items").fetchone()[0], 2)


# ---------------------------------------------------------------- web_watch

class WatchTests(unittest.TestCase):
    def test_snapshot_select_and_json_path(self):
        page = lambda url: (b"<html><body><h1>Toko</h1><p>Harga: Rp 15.000</p><p>Stok: 3</p></body></html>", "text/html")
        self.assertEqual(watch.snapshot({"url": "u", "select": r"Harga[^\n]*"}, page), "Harga: Rp 15.000")
        self.assertEqual(watch.snapshot({"url": "u", "select": r"Stok: (\d+)"}, page), "3")
        api = lambda url: (json.dumps({"data": {"items": [{"price": 7}]}}).encode(), "application/json")
        self.assertEqual(watch.snapshot({"url": "u", "json_path": "data.items[0].price"}, api), "7")
        with self.assertRaisesRegex(ToolError, "no key 'nope'"):
            watch.snapshot({"url": "u", "json_path": "data.nope"}, api)

    def test_watch_runs_through_the_scheduler(self):
        from smooth_agent.scheduler import Crons, Jobs, Scheduler
        from smooth_agent.store import Store
        home = Path(tempfile.mkdtemp())
        st = Store(home / "smooth.db")
        price = {"v": "Rp 15.000"}
        fake = lambda url: (f"<p>Harga: {price['v']}</p><p>jam {os.urandom(2).hex()}</p>".encode(), "text/html")
        reg, _ = registry()
        crons, jobs = Crons(st), Jobs(st)
        with mock.patch.object(watch, "_fetch", fake):
            watch.add_watch_tool(reg, crons, jobs)
            out = reg.run("web_watch", {"action": "create", "name": "harga", "url": "https://toko.example/kopi",
                                        "every": "15m", "select": r"Harga[^\n]*", "deliver": "telegram"})
            self.assertIn("Harga: Rp 15.000", out)
            shell = lambda url: (b"<html><head><title>App</title></head><body><div id=root></div></body></html>", "text/html")
            with mock.patch.object(watch, "_fetch", shell):
                self.assertIn("JavaScript", reg.run("web_watch", {"action": "create", "name": "spa", "url": "https://app.example"}))
            self.assertIn("matched nothing", reg.run("web_watch", {"action": "create", "name": "nm", "url": "https://toko.example",
                                                                   "select": "Diskon[^\\n]*"}))
            self.assertIn("at most every 5 minutes", reg.run("web_watch", {"action": "create", "name": "x",
                                                                          "url": "https://a.example", "every": "1m"}))
            model_calls, delivered = [], []
            sched = Scheduler(st, home, lambda prompt, cr: model_calls.append(prompt) or "ok",
                              deliver=[lambda cr, text, ok: delivered.append(text)], log=lambda m: None)
            jobs.enqueue("cron", {"name": "harga"})
            sched.run_one()  # same price, a changing clock elsewhere on the page: nothing
            self.assertEqual((delivered, model_calls), ([], []))
            price["v"] = "Rp 12.500"
            jobs.enqueue("cron", {"name": "harga"})
            sched.run_one()
            self.assertEqual(model_calls, [], "no prompt: the change is delivered without a model call")
            self.assertIn("-Harga: Rp 15.000", delivered[0])
            self.assertIn("+Harga: Rp 12.500", delivered[0])
            self.assertIn("harga\ton\tevery 15m", reg.run("web_watch", {"action": "list"}))
            self.assertIn("removed", reg.run("web_watch", {"action": "remove", "name": "harga"}))


if __name__ == "__main__":
    unittest.main()
