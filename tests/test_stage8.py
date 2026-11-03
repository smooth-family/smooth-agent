"""Stage 8: named workflows (saved, parameterised playbooks) and their
integration with the scheduler (run now, and schedule via cron)."""
import json
import tempfile
import unittest
from pathlib import Path

from smooth_agent.scheduler import Crons, Jobs, Scheduler
from smooth_agent.store import Store
from smooth_agent.tools import Registry, ToolError
from smooth_agent.workflows import Workflows, add_workflow_tool


def store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "smooth.db")


class WorkflowCrudTests(unittest.TestCase):
    def setUp(self):
        self.wfs = Workflows(store())

    def test_create_and_get(self):
        w = self.wfs.create("research", "1. Search {{topic}}.\n2. Summarise.", description="helper",
                            params=[{"name": "topic"}])
        self.assertEqual(w["name"], "research")
        self.assertEqual(self.wfs.get("research")["description"], "helper")
        self.assertEqual([p["name"] for p in json.loads(w["params"])], ["topic"])

    def test_bad_name_rejected(self):
        with self.assertRaisesRegex(ToolError, "name"):
            self.wfs.create("has space", "do a thing")

    def test_empty_steps_rejected(self):
        with self.assertRaisesRegex(ToolError, "steps"):
            self.wfs.create("x", "   ")

    def test_duplicate_rejected(self):
        self.wfs.create("x", "step")
        with self.assertRaisesRegex(ToolError, "exists"):
            self.wfs.create("x", "step")

    def test_undeclared_placeholder_rejected(self):
        with self.assertRaisesRegex(ToolError, "undeclared"):
            self.wfs.create("x", "greet {{name}}")  # {{name}} never declared

    def test_param_with_default_is_optional(self):
        w = self.wfs.create("x", "hi {{who}}", params=[{"name": "who", "default": "world"}])
        spec = json.loads(w["params"])[0]
        self.assertFalse(spec["required"])
        self.assertEqual(spec["default"], "world")

    def test_string_params_shorthand(self):
        w = self.wfs.create("x", "use {{a}} and {{b}}", params=["a", "b"])
        self.assertEqual({p["name"] for p in json.loads(w["params"])}, {"a", "b"})

    def test_duplicate_param_rejected(self):
        with self.assertRaisesRegex(ToolError, "duplicate"):
            self.wfs.create("x", "hi {{a}}", params=["a", "a"])

    def test_update_changes_fields_and_revalidates(self):
        self.wfs.create("x", "hi {{a}}", params=["a"])
        self.wfs.update("x", description="new desc")
        self.assertEqual(self.wfs.get("x")["description"], "new desc")
        # changing steps to use an undeclared placeholder is rejected
        with self.assertRaisesRegex(ToolError, "undeclared"):
            self.wfs.update("x", steps="hi {{a}} and {{b}}")

    def test_remove(self):
        self.wfs.create("x", "step")
        self.assertTrue(self.wfs.remove("x"))
        self.assertFalse(self.wfs.remove("x"))
        with self.assertRaisesRegex(ToolError, "no workflow"):
            self.wfs.get("x")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.wfs = Workflows(store())
        self.wfs.create("research", "Search {{topic}} in {{lang}}.", description="r",
                       params=[{"name": "topic"}, {"name": "lang", "default": "English"}],
                       skills=["deep-research"])

    def test_render_substitutes_and_defaults(self):
        out = self.wfs.render("research", {"topic": "solar"})
        self.assertIn("Search solar in English.", out)
        self.assertIn("deep-research", out)  # skill note included
        self.assertIn("topic: solar", out)

    def test_render_missing_required(self):
        with self.assertRaisesRegex(ToolError, "needs: topic"):
            self.wfs.render("research", {})

    def test_render_unknown_param(self):
        with self.assertRaisesRegex(ToolError, "unknown parameter"):
            self.wfs.render("research", {"topic": "x", "bogus": "y"})

    def test_render_empty_value_treated_as_missing(self):
        with self.assertRaisesRegex(ToolError, "needs: topic"):
            self.wfs.render("research", {"topic": ""})


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.st = store()
        self.wfs = Workflows(self.st)
        self.jobs = Jobs(self.st)
        self.reg = Registry(lambda n, a: True)
        add_workflow_tool(self.reg, self.wfs,
                          enqueue=lambda name, params: self.jobs.enqueue("workflow", {"name": name, "params": params}))

    def test_create_list_show_remove(self):
        self.assertIn("created", self.reg.run("workflow", {"action": "create", "name": "x", "steps": "do {{a}}",
                                                            "params": ["a"]}))
        self.assertIn("x", self.reg.run("workflow", {"action": "list"}))
        show = self.reg.run("workflow", {"action": "show", "name": "x"})
        self.assertIn("- a (required)", show)
        self.assertIn("removed", self.reg.run("workflow", {"action": "remove", "name": "x"}))

    def test_run_enqueues_job(self):
        self.reg.run("workflow", {"action": "create", "name": "x", "steps": "do {{a}}", "params": ["a"]})
        out = self.reg.run("workflow", {"action": "run", "name": "x", "args": {"a": "1"}})
        self.assertIn("queued", out)
        self.assertEqual(len(self.jobs.list(state="queued")), 1)

    def test_run_missing_param_errors_before_queue(self):
        self.reg.run("workflow", {"action": "create", "name": "x", "steps": "do {{a}}", "params": ["a"]})
        out = self.reg.run("workflow", {"action": "run", "name": "x", "args": {}})
        self.assertIn("needs: a", out)
        self.assertEqual(self.jobs.list(state="queued"), [])  # nothing queued on bad params

    def test_run_without_queue_returns_steps(self):
        reg = Registry(lambda n, a: True)
        wfs = Workflows(store())
        add_workflow_tool(reg, wfs)  # no enqueue wired
        reg.run("workflow", {"action": "create", "name": "x", "steps": "do {{a}}", "params": ["a"]})
        out = reg.run("workflow", {"action": "run", "name": "x", "args": {"a": "42"}})
        self.assertIn("do 42", out)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.st = Store(self.home / "smooth.db")
        self.wfs = Workflows(self.st)
        self.seen = {}
        self.delivered = []
        self.sched = Scheduler(self.st, self.home, self._run_fn,
                               deliver=[lambda c, t, ok: self.delivered.append((c["name"], ok))],
                               log=lambda m: None)

    def _run_fn(self, prompt, cr):
        self.seen["prompt"] = prompt
        self.seen["cr"] = cr
        return f"RAN: {prompt[:30]}"

    def test_workflow_job_runs_and_counts(self):
        self.wfs.create("hello", "Hi {{name}}.", params=[{"name": "name", "default": "world"}])
        Jobs(self.st).enqueue("workflow", {"name": "hello", "params": {"name": "Angga"}})
        self.assertTrue(self.sched.run_one())
        self.assertIn("Angga", self.seen["prompt"])
        self.assertEqual(self.wfs.get("hello")["runs"], 1)
        self.assertEqual(self.delivered[-1], ("workflow-hello", True))
        self.assertTrue(list((self.home / "cron-output" / "workflow-hello").glob("*.md")))

    def test_cron_pointing_at_workflow(self):
        self.wfs.create("hello", "Hi {{name}}.", params=[{"name": "name", "default": "world"}])
        Crons(self.st).create("sched", "every 1h", "", workflow="hello")
        Jobs(self.st).enqueue("cron", {"name": "sched"})
        self.assertTrue(self.sched.run_one())
        self.assertIn("Hi world.", self.seen["prompt"])  # default applied

    def test_removed_workflow_job_finishes_cleanly(self):
        Jobs(self.st).enqueue("workflow", {"name": "ghost", "params": {}})
        self.assertTrue(self.sched.run_one())  # does not raise
        self.assertEqual(self.seen, {})  # run_fn never called

    def test_cron_create_requires_something_to_do(self):
        with self.assertRaisesRegex(ToolError, "prompt"):
            Crons(self.st).create("empty", "every 1h", "")


# ================================================================ Stage 8.2 connectors

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from smooth_agent import connect
from smooth_agent.services import (CalendarSettings, Drive, Notion, add_calendar_tool, drive_action, notion_action)


class _Svc(BaseHTTPRequestHandler):
    last = {}

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self):
        n = int(self.headers.get("content-length", 0))
        _Svc.last = {"path": self.path, "method": self.command, "auth": self.headers.get("authorization"),
                     "notion_version": self.headers.get("notion-version"),
                     "body": self.rfile.read(n).decode() if n else ""}
        return _Svc.last

    def do_GET(self):
        self._record()
        p = self.path
        if "/events" in p:
            return self._send(200, json.dumps({"items": [
                {"summary": "Standup", "start": {"dateTime": "2026-11-05T09:00:00Z"}, "location": "Zoom"}]}))
        if "/blocks/" in p and "/children" in p:
            return self._send(200, json.dumps({"results": [
                {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title"}]}},
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Body text"}]}}]}))
        if "/files/" in p and "/export" in p:
            return self._send(200, "exported doc text", "text/plain")
        if "/files/" in p and "alt=media" in p:
            return self._send(200, "raw,csv\n1,2", "text/csv")
        if "/files/" in p:  # metadata
            return self._send(200, json.dumps({"id": "F1", "name": "Notes", "mimeType": "application/vnd.google-apps.document"}))
        if "/files" in p:
            return self._send(200, json.dumps({"files": [
                {"id": "F1", "name": "Notes", "mimeType": "application/vnd.google-apps.document"}]}))
        return self._send(404, "{}")

    def do_POST(self):
        self._record()
        if self.path.endswith("/search"):
            return self._send(200, json.dumps({"results": [
                {"object": "page", "id": "PAGE1", "properties": {"Name": {"type": "title", "title": [{"plain_text": "My Page"}]}}}]}))
        if "/databases/" in self.path and self.path.endswith("/query"):
            return self._send(200, json.dumps({"results": [
                {"properties": {"Name": {"type": "title", "title": [{"plain_text": "Row A"}]},
                                "Done": {"type": "checkbox", "checkbox": True}}}]}))
        if "/events" in self.path:
            return self._send(200, json.dumps({"htmlLink": "https://cal/evt1"}))
        return self._send(404, "{}")

    def do_PATCH(self):
        self._record()
        return self._send(200, json.dumps({"results": []}))

    def do_REPORT(self):
        self._record()
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Dentist\r\nDTSTART:20261106T140000Z\r\n"
               "END:VEVENT\r\nEND:VCALENDAR")
        xml = ('<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><response><propstat><prop>'
               f'<C:calendar-data>{ics}</C:calendar-data></prop></propstat></response></multistatus>')
        return self._send(207, xml, "application/xml")

    def do_PUT(self):
        self._record()
        return self._send(201, "")


class ConnectorServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Svc)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def reg(self, answer=True):
        self.asked = []
        return Registry(lambda n, a: self.asked.append((n, a)) or answer)

    # ---- calendar: Google ----
    def test_google_calendar_agenda_and_create(self):
        st = CalendarSettings(kind="google", token="ya29-secret", api=self.url, calendar_id="primary")
        reg = self.reg()
        add_calendar_tool(reg, st)
        out = reg.run("calendar", {"action": "agenda"})
        self.assertIn("Standup", out)
        self.assertIn("2026-11-05 09:00", out)
        self.assertEqual(_Svc.last["auth"], "Bearer ya29-secret")  # token sent, from env not model
        # create asks first
        out = reg.run("calendar", {"action": "create", "summary": "Call", "start": "2026-11-05T09:00:00Z",
                                   "end": "2026-11-05T09:30:00Z"})
        self.assertIn("created", out)
        self.assertEqual(self.asked[-1][0], "calendar")

    def test_calendar_create_denied(self):
        st = CalendarSettings(kind="google", token="t", api=self.url)
        reg = self.reg(answer=False)
        add_calendar_tool(reg, st)
        out = reg.run("calendar", {"action": "create", "summary": "x", "start": "2026-11-05T09:00:00Z",
                                   "end": "2026-11-05T10:00:00Z"})
        self.assertIn("denied", out)

    # ---- calendar: CalDAV ----
    def test_caldav_agenda(self):
        st = CalendarSettings(kind="caldav", url=self.url + "/dav/cal", user="u", password="p")
        reg = self.reg()
        add_calendar_tool(reg, st)
        out = reg.run("calendar", {"action": "agenda"})
        self.assertIn("Dentist", out)
        self.assertIn("2026-11-06 14:00", out)
        self.assertEqual(_Svc.last["method"], "REPORT")
        self.assertTrue(_Svc.last["auth"].startswith("Basic "))

    # ---- Notion ----
    def test_notion_search_page_database(self):
        client = Notion("ntn-secret", api=self.url)
        self.assertIn("My Page", notion_action(client, {"action": "search", "query": "page"}))
        self.assertEqual(_Svc.last["notion_version"], "2022-06-28")
        page = notion_action(client, {"action": "page", "id": "PAGE1"})
        self.assertIn("# Title", page)
        self.assertIn("Body text", page)
        db = notion_action(client, {"action": "database", "id": "DB1"})
        self.assertIn("Name=Row A", db)

    def test_notion_append_asks(self):
        from smooth_agent.services import add_notion_tool
        reg = self.reg(answer=False)
        add_notion_tool(reg, {"notion": {}}, {"NOTION_TOKEN": "t"})
        # point the client at the mock by monkeypatching the api base is overkill; just check the gate
        out = reg.run("notion", {"action": "append", "id": "PAGE1", "text": "hi"})
        self.assertIn("denied", out)

    # ---- Google Drive ----
    def test_drive_list_and_read(self):
        client = Drive("ya29", api=self.url)
        self.assertIn("Notes", drive_action(client, {"action": "list"}))
        out = drive_action(client, {"action": "read", "id": "F1"})
        self.assertIn("exported doc text", out)  # Google Doc exported to text
        self.assertIn("# Notes", out)


if __name__ == "__main__":
    unittest.main()
