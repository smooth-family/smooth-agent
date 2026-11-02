import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from smooth_agent.agent import Agent
from smooth_agent.interact import add_clarify_tool, add_session_tools
from smooth_agent.memory import Memory, add_memory_tool
from smooth_agent.routers import Provider, Router
from smooth_agent.schedule import ScheduleError, get_tz, next_run, parse
from smooth_agent.scheduler import Crons, DaemonLock, Jobs, Scheduler, add_cron_tool
from smooth_agent.store import Store, fts_query
from smooth_agent.tools import Registry, ToolError

REPO = Path(__file__).resolve().parents[1]


def tmp_store():
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def serve(fn):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            body = json.dumps(fn(req)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def answer(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"total_tokens": 3}}


def router_for(base):
    return Router([Provider.from_config("p", {"provider": "openai-compatible", "model": "m", "base_url": base, "key_env": "K"}, env={"K": "x"})])


class StoreTests(unittest.TestCase):
    def test_sessions_messages_search(self):
        s = tmp_store()
        s.new_session("a")
        s.add_message("a", {"role": "user", "content": "the deploy key for the staging box"})
        s.add_message("a", {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "terminal", "arguments": '{"command":"ls"}'}}]})
        s.new_session("b")
        s.add_message("b", {"role": "user", "content": "unrelated"})
        self.assertEqual(s.sessions()[0]["id"], "b")
        self.assertEqual([m["role"] for m in s.session_messages("a")], ["user", "assistant"])
        self.assertEqual(s.one("SELECT title FROM sessions WHERE id='a'")["title"], "the deploy key for the staging box")
        hits = s.search_messages("staging deploy")
        self.assertEqual(hits[0]["session_id"], "a")
        self.assertIn("[staging]", hits[0]["snip"])
        self.assertEqual(s.search_messages("staging", exclude_session="a"), [])
        self.assertTrue(s.delete_session("a"))
        self.assertEqual(s.session_messages("a"), [])

    def test_fts_query_is_safe(self):
        self.assertEqual(fts_query('a "b" c*'), "")      # nothing long enough
        self.assertEqual(fts_query("x OR y"), '"or"')    # operators become quoted words
        self.assertEqual(fts_query("hello-world NEAR"), '"hello-world"* OR "near"*')


class MemoryTests(unittest.TestCase):
    def test_add_recall_tool(self):
        mem = Memory(tmp_store())
        reg = Registry()
        add_memory_tool(reg, mem)
        self.assertIn("remembered", reg.run("memory", {"action": "add", "target": "user", "content": "Owner lives in Jakarta"}))
        reg.run("memory", {"action": "add", "kind": "finding", "content": "The staging server needs sudo for docker"})
        reg.run("memory", {"action": "add", "kind": "decision", "content": "Use Postgres for the billing service"})
        self.assertIn("remembered #1", reg.run("memory", {"action": "add", "target": "user", "content": "owner lives in jakarta"}))  # dedup
        recall = mem.recall("deploy docker on staging")
        self.assertIn("Jakarta", recall)          # user facts always
        self.assertIn("sudo for docker", recall)  # matched
        self.assertNotIn("Postgres", recall)
        self.assertIn("updated", reg.run("memory", {"action": "replace", "old_text": "Postgres", "content": "Use SQLite for the billing service"}))
        self.assertIn("SQLite", reg.run("memory", {"action": "search", "content": "billing"}))
        self.assertIn("pinned", reg.run("memory", {"action": "pin", "old_text": "SQLite"}))
        self.assertIn("SQLite", mem.recall("unrelated words"))
        self.assertIn("forgot", reg.run("memory", {"action": "remove", "old_text": "sudo"}))
        self.assertIn("error", reg.run("memory", {"action": "remove", "old_text": "nothing like this"}))
        self.assertIn("error", reg.run("memory", {"action": "add", "content": "x" * 2000}))


class AgentStateTests(unittest.TestCase):
    def test_session_saved_resumed_and_memory_in_prompt(self):
        seen = []
        srv, base = serve(lambda req: (seen.append(req), answer("ok"))[1])
        try:
            st = tmp_store()
            mem = Memory(st)
            mem.add("Owner prefers short answers", "preference")
            a = Agent(router_for(base), Registry(), store=st, memory=mem, session_id="s1")
            a.run("hello there")
            self.assertIn("Owner prefers short answers", seen[-1]["messages"][0]["content"])
            self.assertEqual([m["role"] for m in st.session_messages("s1")], ["user", "assistant"])
            b = Agent(router_for(base), Registry(), store=st, memory=mem, session_id="s2")
            self.assertEqual(b.resume("s1"), 2)
            b.run("again")
            self.assertEqual([m["content"] for m in seen[-1]["messages"][1:]], ["hello there", "ok", "again"])
            self.assertEqual(len(st.session_messages("s1")), 4)  # resumed turns are saved to the same session
        finally:
            srv.shutdown()

    def test_compression_keeps_recent_and_pairs(self):
        calls = []

        def fn(req):
            calls.append(req)
            if req["messages"][0]["content"].startswith("Summarise the conversation"):
                return answer("- goal: test compression\n- decided X")
            return answer("fine")
        srv, base = serve(fn)
        try:
            a = Agent(router_for(base), Registry(), context_tokens=400, keep_recent=4)
            for i in range(8):
                a.history += [{"role": "user", "content": f"question {i} " + "x" * 120},
                              {"role": "assistant", "content": None, "tool_calls": [{"id": f"t{i}", "type": "function", "function": {"name": "now", "arguments": "{}"}}]},
                              {"role": "tool", "tool_call_id": f"t{i}", "content": "y" * 100},
                              {"role": "assistant", "content": f"answer {i}"}]
            events = []
            a.on_event = lambda e, d: events.append(e)
            a.run("final question")
            self.assertIn("compressed", events)
            sent = calls[-1]["messages"]
            self.assertIn("Summary of the earlier part", sent[1]["content"])
            self.assertEqual(sent[2]["role"], "user")  # kept part starts at a user message
            ids_called = {c["id"] for m in sent if m.get("tool_calls") for c in m["tool_calls"]}
            ids_answered = {m["tool_call_id"] for m in sent if m["role"] == "tool"}
            self.assertEqual(ids_called, ids_answered)  # no orphaned tool calls/results
        finally:
            srv.shutdown()

    def test_out_of_steps_notice_is_not_a_system_message(self):
        n = {"i": 0}

        def fn(req):
            n["i"] += 1
            if n["i"] <= 2:
                return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{n['i']}", "type": "function", "function": {"name": "now", "arguments": "{}"}}]}}]}
            return answer("done")
        srv, base = serve(fn)
        try:
            from smooth_agent.tools import Workspace, default_registry
            a = Agent(router_for(base), default_registry(Workspace(tempfile.mkdtemp()), allow_http=False), max_steps=2)
            t = a.run("go")
            self.assertEqual(t.stopped, "max_steps")
            self.assertEqual([m["role"] for m in a.history].count("system"), 1)
        finally:
            srv.shutdown()


class ScheduleTests(unittest.TestCase):
    def test_parse_and_next(self):
        tz = "Asia/Jakarta"
        base = datetime(2026, 11, 2, 10, 17, tzinfo=get_tz(tz)).timestamp()  # Monday 10:17
        nxt = lambda s: datetime.fromtimestamp(next_run(s, base, tz), get_tz(tz)).strftime("%a %d %H:%M")  # noqa: E731
        self.assertEqual(nxt("*/15 * * * *"), "Mon 02 10:30")
        self.assertEqual(nxt("weekdays 08:30"), "Tue 03 08:30")
        self.assertEqual(nxt("weekly fri 17:00"), "Fri 06 17:00")
        self.assertEqual(nxt("30 6 13 * 5"), "Fri 06 06:30")   # day-of-month OR day-of-week
        self.assertEqual(nxt("every 2h"), "Mon 02 12:17")
        self.assertIsNone(next_run("at 2026-01-01 07:00", base, tz))
        for bad in ("every 10s", "61 * * * *", "weekly xyz 09:00", ""):
            with self.assertRaises(ScheduleError):
                parse(bad)


class SchedulerTests(unittest.TestCase):
    def test_cron_tick_run_retry_and_repeat(self):
        st = tmp_store()
        home = Path(tempfile.mkdtemp())
        crons = Crons(st)
        crons.create("news", "every 1h", "summarise news", repeat=2)
        with st.tx() as c:
            c.execute("UPDATE crons SET next_run=? WHERE name='news'", (time.time() - 1,))
        attempts = {"n": 0}

        def run(prompt, cr):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("model down")
            return f"result for {cr['name']}: {prompt}"
        delivered = []
        sched = Scheduler(st, home, run, deliver=[lambda cr, text, ok: delivered.append((cr["name"], ok))], log=lambda m: None)
        self.assertEqual(sched.tick_crons(), 1)
        self.assertTrue(sched.run_one())               # first attempt fails -> queued again with backoff
        job = Jobs(st).list()[0]
        self.assertEqual((job["state"], job["attempts"]), ("queued", 1))
        self.assertEqual(delivered, [])
        with st.tx() as c:
            c.execute("UPDATE jobs SET run_at=?", (time.time() - 1,))
        self.assertTrue(sched.run_one())
        job = Jobs(st).list()[0]
        self.assertEqual(job["state"], "done")
        self.assertIn("summarise news", job["result"])
        self.assertEqual(delivered, [("news", True)])
        self.assertTrue(list((home / "cron-output" / "news").glob("*.md")))
        # second due run reaches repeat=2 and switches the cron off
        with st.tx() as c:
            c.execute("UPDATE crons SET next_run=? WHERE name='news'", (time.time() - 1,))
        sched.tick_crons()
        self.assertEqual(crons.get("news")["enabled"], 0)

    def test_expired_lease_is_retried(self):
        st = tmp_store()
        jobs = Jobs(st)
        jid = jobs.enqueue("cron", {"name": "x"})
        self.assertEqual(jobs.claim("w1")["id"], jid)
        self.assertIsNone(jobs.claim("w2"))  # leased
        with st.tx() as c:
            c.execute("UPDATE jobs SET locked_until=? WHERE id=?", (time.time() - 1, jid))
        again = jobs.claim("w2")              # the first worker "died"
        self.assertEqual((again["id"], again["attempts"]), (jid, 2))

    def test_cron_tool_and_validation(self):
        st = tmp_store()
        reg = Registry()
        add_cron_tool(reg, Crons(st), Jobs(st), Path(tempfile.mkdtemp()))
        out = reg.run("cronjob", {"action": "create", "name": "standup", "schedule": "weekdays 09:00", "prompt": "list my open PRs"})
        self.assertIn("created 'standup'", out)
        self.assertIn("not running", out)
        self.assertIn("standup", reg.run("cronjob", {"action": "list"}))
        self.assertIn("paused", reg.run("cronjob", {"action": "pause", "name": "standup"}))
        self.assertIn("queued", reg.run("cronjob", {"action": "run", "job_id": "standup"}))
        self.assertIn("error", reg.run("cronjob", {"action": "create", "name": "bad name!", "schedule": "daily 09:00", "prompt": "x"}))
        self.assertIn("error", reg.run("cronjob", {"action": "create", "name": "past", "schedule": "at 2020-01-01 00:00", "prompt": "x"}))
        self.assertIn("removed", reg.run("cronjob", {"action": "remove", "name": "standup"}))

    def test_script_jobs_and_silent(self):
        st = tmp_store()
        home = Path(tempfile.mkdtemp())
        crons = Crons(st)
        crons.create("plain", "every 1h", "", script="echo new-item-42")
        crons.create("quiet", "every 1h", "", script="true")
        crons.create("mixed", "every 1h", "Summarise these items", script="echo item-a; echo item-b")
        crons.create("agent-silent", "every 1h", "check things")
        with st.tx() as c:
            c.execute("UPDATE crons SET next_run=?", (time.time() - 1,))
        prompts, delivered = [], []

        def run(prompt, cr):
            prompts.append(prompt)
            return "[SILENT]" if cr["name"] == "agent-silent" else "summary: a, b"
        sched = Scheduler(st, home, run, deliver=[lambda cr, text, ok: delivered.append((cr["name"], text.strip()))], log=lambda m: None)
        sched.tick_crons()
        while sched.run_one():
            pass
        self.assertIn(("plain", "new-item-42"), delivered)
        self.assertIn(("mixed", "summary: a, b"), delivered)
        self.assertNotIn("quiet", [d[0] for d in delivered])         # empty output: nothing to report
        self.assertNotIn("agent-silent", [d[0] for d in delivered])  # [SILENT] from the agent
        self.assertTrue(any("item-a" in p and "Summarise" in p for p in prompts))
        self.assertEqual(len(prompts), 2)                             # scripts alone never call the model

    def test_cron_tool_scripts_are_gated(self):
        st = tmp_store()
        args = {"action": "create", "name": "w", "schedule": "every 5m", "script": "curl -s example.com"}
        reg = Registry()
        add_cron_tool(reg, Crons(st), Jobs(st), Path(tempfile.mkdtemp()), scripts_allowed=False)
        self.assertIn("terminal = true", reg.run("cronjob", args))
        reg = Registry(approver=lambda n, a: False)
        add_cron_tool(reg, Crons(st), Jobs(st), Path(tempfile.mkdtemp()), scripts_allowed=True)
        self.assertIn("denied", reg.run("cronjob", args))
        reg.approver = lambda n, a: "curl" in a["command"]
        self.assertIn("created 'w'", reg.run("cronjob", args))

    def test_daemon_lock(self):
        home = Path(tempfile.mkdtemp())
        a = DaemonLock(home)
        self.assertTrue(a.acquire())
        self.assertTrue(DaemonLock.running(home))
        self.assertFalse(DaemonLock(home).acquire())


class InteractTests(unittest.TestCase):
    def test_clarify(self):
        reg = Registry()
        add_clarify_tool(reg, lambda q, c: c[1])
        self.assertEqual(reg.run("clarify", {"question": "which?", "choices": ["a", "b"]}), "The owner answered: b")
        reg2 = Registry()
        add_clarify_tool(reg2, None)
        self.assertIn("No one is available", reg2.run("clarify", {"question": "which?"}))

    def test_session_search_tool(self):
        st = tmp_store()
        st.new_session("old")
        st.add_message("old", {"role": "user", "content": "the wifi password for the office router"})
        reg = Registry()
        add_session_tools(reg, st, lambda: "now")
        self.assertIn("[old]", reg.run("session_search", {"query": "office router"}))


class CLITests(unittest.TestCase):
    def cli(self, *argv, home):
        env = {**os.environ, "SMOOTH_AGENT_HOME": str(home), "PYTHONPATH": str(REPO)}
        env.pop("SMOOTH_AGENT_CONFIG", None)
        return subprocess.run([sys.executable, "-m", "smooth_agent.cli", *argv], capture_output=True, text=True,
                              env=env, cwd=tempfile.mkdtemp(), timeout=120, stdin=subprocess.DEVNULL)

    def test_memory_cron_sessions_commands(self):
        home = Path(tempfile.mkdtemp()) / "h"
        self.assertEqual(self.cli("setup", "--provider", "deepseek", "--model", "deepseek-chat", "--key", "k", home=home).returncode, 0)
        self.assertIn("remembered #1", self.cli("memory", "add", "Owner", "is", "a", "farmer", "--kind", "user", home=home).stdout)
        self.assertIn("farmer", self.cli("memory", "search", "farmer", home=home).stdout)
        r = self.cli("cron", "add", "morning", "daily 07:00", "Give me the weather", home=home)
        self.assertIn("added 'morning'", r.stdout, r.stderr)
        self.assertIn("morning", self.cli("cron", "list", home=home).stdout)
        self.assertIn("error", self.cli("cron", "add", "bad", "whenever", "x", home=home).stderr)
        self.assertIn("paused", self.cli("cron", "pause", "morning", home=home).stdout)
        self.assertEqual(self.cli("sessions", home=home).returncode, 0)
        self.assertEqual(self.cli("daemon", "--once", home=home).returncode, 0)


if __name__ == "__main__":
    unittest.main()
