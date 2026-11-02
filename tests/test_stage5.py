"""Stage 5.1: chat commands, live progress, Telegram rendering, persona files, cut-off answers."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from smooth_agent.agent import Agent
from smooth_agent.channels import (Channel, ChannelConfig, ChannelHub, Inbound, Progress, balance_fences,
                                    describe_step, md_to_html)
from smooth_agent.persona import persona_text
from smooth_agent.routers import Completion
from smooth_agent.store import Store
from smooth_agent.tools import Registry


class StubProvider:
    def __init__(self, model):
        self.model = model


class StubRouter:
    """Plays back scripted completions; `script(messages)` returns a Completion."""

    def __init__(self, script, providers=("main", "backup")):
        self.script = script
        self.providers = {n: StubProvider(f"{n}-model") for n in providers}
        self.default = providers[0]
        self.fallbacks = list(providers[1:])
        self.calls = []

    def complete(self, messages, provider=None, model=None, max_tokens=None, extra=None):
        self.calls.append({"provider": provider, "messages": [dict(m) for m in messages]})
        return self.script(messages)


def answer(text, finish="stop"):
    return Completion(text=text, model="main-model", provider="main", finish_reason=finish,
                      message={"role": "assistant", "content": text})


def tool_call(name, args="{}", cid="c1"):
    call = {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
    return Completion(text="", model="main-model", provider="main", tool_calls=[call], finish_reason="tool_calls",
                      message={"role": "assistant", "content": None, "tool_calls": [call]})


class ProgressChannel(Channel):
    kind = "fake"
    supports_progress = True

    def __init__(self, owners=("1",)):
        super().__init__(ChannelConfig("fake", owners=list(owners)), log=lambda m: None)
        self.sent, self.posts, self.edits, self.deleted = [], [], [], []

    def send(self, chat_id, text, *, buttons=None):
        self.sent.append(text)

    def post(self, chat_id, text):
        self.posts.append(text)
        return "m1"

    def edit(self, chat_id, message_id, text):
        self.edits.append(text)

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)


def wait_for(cond, secs=6.0):
    end = time.time() + secs
    while not cond() and time.time() < end:
        time.sleep(0.02)
    return cond()


class CutOffAnswerTests(unittest.TestCase):
    def test_length_cut_is_continued_and_joined(self):
        replies = iter([answer("Langkahnya: satu, dua, ", "length"), answer("tiga. Selesai.", "stop")])
        router = StubRouter(lambda m: next(replies))
        turn = Agent(router, Registry()).run("jelasin")
        self.assertEqual(turn.text, "Langkahnya: satu, dua, tiga. Selesai.")
        self.assertIn("cut off", router.calls[1]["messages"][-1]["content"])

    def test_continues_at_most_max_continues(self):
        router = StubRouter(lambda m: answer("x", "length"))
        turn = Agent(router, Registry(), max_continues=2).run("go")
        self.assertEqual(turn.text, "xxx")
        self.assertEqual(len(router.calls), 3)


class PersonaTests(unittest.TestCase):
    def test_loaded_in_order_workspace_wins_and_capped(self):
        ws, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (ws / "USER.md").write_text("Name: Angga")
        (home / "USER.md").write_text("Name: someone else")  # workspace copy wins
        (home / "SOUL.md").write_text("Be warm.")
        (ws / "AGENTS.md").write_text("A" * 7000)
        (ws / "TOOLS.md").write_text("   ")  # empty files are skipped
        text = persona_text([ws, home])
        self.assertLess(text.index("## SOUL.md"), text.index("## USER.md"))
        self.assertIn("Name: Angga", text)
        self.assertNotIn("someone else", text)
        self.assertIn("AGENTS.md cut at", text)
        self.assertNotIn("TOOLS.md", text)
        self.assertEqual(persona_text([Path(tempfile.mkdtemp())]), "")

    def test_agent_reads_persona_each_turn(self):
        ws = Path(tempfile.mkdtemp())
        router = StubRouter(lambda m: answer("ok"))
        agent = Agent(router, Registry(), persona_dirs=[ws])
        agent.run("hi")
        self.assertNotIn("Persona files", router.calls[-1]["messages"][0]["content"])
        (ws / "SOUL.md").write_text("Always answer like a pirate.")
        agent.run("hi again")  # picked up without a restart
        self.assertIn("Always answer like a pirate.", router.calls[-1]["messages"][0]["content"])


class RenderTests(unittest.TestCase):
    def test_md_to_html(self):
        html = md_to_html("**Siap** pakai `ls -la` & <b>\n```bash\necho <hi>\n```\nlihat [docs](https://x.y/z)\n## Judul")
        self.assertIn("<b>Siap</b>", html)
        self.assertIn("<code>ls -la</code>", html)
        self.assertIn("&amp; &lt;b&gt;", html)  # raw HTML from the model is escaped
        self.assertIn("<pre>echo &lt;hi&gt;</pre>", html)
        self.assertIn('<a href="https://x.y/z">docs</a>', html)
        self.assertIn("<b>Judul</b>", html)
        self.assertEqual(md_to_html("2 ** 3 = 8"), "2 ** 3 = 8")

    def test_balance_fences(self):
        parts = balance_fences(["intro\n```py\na = 1", "b = 2\n```\nend"])
        self.assertTrue(parts[0].endswith("```"))
        self.assertTrue(parts[1].startswith("```\n"))
        self.assertTrue(all(p.count("```") % 2 == 0 for p in parts))

    def test_describe_step(self):
        self.assertEqual(describe_step("terminal", {"command": "git   pull"}), "$ git pull")
        self.assertEqual(describe_step("write_file", {"path": "app.py", "content": "x"}), "write app.py")
        self.assertEqual(describe_step("read_file", {"path": "/etc/hosts"}), "read /etc/hosts")
        self.assertEqual(describe_step("patch", {"path": "a.toml"}), "edit a.toml")
        self.assertEqual(describe_step("web_extract", {"urls": ["https://a.b"]}), "open a.b")
        self.assertEqual(describe_step("execute_code", {"code": "import os\nprint(1)"}), "python import os")
        self.assertEqual(describe_step("browser_click", {"ref": "@e1"}), "click @e1")
        self.assertEqual(describe_step("mcp_github_list_prs", {}), "mcp github_list_prs")
        self.assertTrue(describe_step("web_search", {"query": "q" * 200}).endswith("…"))


class StepTextTests(unittest.TestCase):
    def test_phone_friendly_steps(self):
        self.assertEqual(describe_step("terminal", {"command": "cd /tmp && curl -s https://x.y/app.js | head -c 300"}),
                         "$ curl -s https://x.y/app.js | head -c 300")
        self.assertEqual(describe_step("terminal", {"command": "cd /a; cd /b && python3 -c \"\nimport re\nprint(1)\""}),
                         '$ python3 -c " …')
        self.assertEqual(describe_step("web_extract", {"urls": ["https://smooth.family/"]}), "open smooth.family")
        self.assertEqual(describe_step("http_get", {"url": "https://www.example.com/api/v1"}), "fetch example.com/api/v1")
        self.assertLessEqual(len(describe_step("terminal", {"command": "echo " + "x" * 300})), 64)

    def test_progress_html_wraps_on_phones(self):
        from smooth_agent.channels import TelegramChannel
        html = TelegramChannel._progress_html("⚙️ Working · 4s\n✓ $ git pull\n⏳ read <a>.md\n… 3 earlier steps")
        self.assertTrue(html.startswith("<b>⚙️ Working · 4s</b>"))
        self.assertIn("✓ <code>$ git pull</code>", html)
        self.assertIn("⏳ <code>read &lt;a&gt;.md</code>", html)
        self.assertIn("<i>… 3 earlier steps</i>", html)
        self.assertNotIn("<pre>", html)

    def test_approval_text_is_readable(self):
        from smooth_agent.channels import approval_text
        t = approval_text("terminal", {"command": "rm -rf build"})
        self.assertIn("Run this command?", t)
        self.assertIn("```\nrm -rf build\n```", t)
        self.assertNotIn("{", t)
        self.assertIn("`/etc/hosts`", approval_text("files: write outside the workspace", {"path": "/etc/hosts"}))


class ProgressTests(unittest.TestCase):
    def test_post_edit_delete(self):
        ch = ProgressChannel()
        p = Progress(ch, "1", log=lambda m: None)
        p.EDIT_EVERY = 0.1
        p.on_event("tool", {"name": "web_search", "arguments": {"query": "berita AI"}})
        self.assertTrue(wait_for(lambda: ch.posts))
        p.on_event("tool", {"name": "read_file", "arguments": {"path": "a.md"}})
        p.on_event("tool_result", {"name": "read_file", "result": "error: no such file"})
        self.assertTrue(wait_for(lambda: ch.edits))
        p.finish()
        self.assertIn("search berita AI", ch.posts[0])
        self.assertIn("✗ read a.md", ch.edits[-1])
        self.assertIn("⏳ search berita AI", ch.edits[-1])  # no result yet: still running
        self.assertEqual(ch.deleted, ["m1"])

    def test_failed_command_is_marked(self):
        ch = ProgressChannel()
        p = Progress(ch, "1", log=lambda m: None)
        p.on_event("tool", {"name": "terminal", "arguments": {"command": "false"}})
        p.on_event("tool_result", {"name": "terminal", "result": "[exit 1]\n"})
        p.on_event("tool", {"name": "terminal", "arguments": {"command": "true"}})
        self.assertEqual(p.lines, ["✗ $ false", "⏳ $ true"])
        p.on_event("tool_result", {"name": "terminal", "result": "[exit 0]\n"})
        self.assertEqual(p.lines[-1], "✓ $ true")
        p.finish()

    def test_no_message_when_no_tools(self):
        ch = ProgressChannel()
        p = Progress(ch, "1", log=lambda m: None)
        p.finish()
        self.assertEqual((ch.posts, ch.deleted), ([], []))


class CommandTests(unittest.TestCase):
    def _hub(self, script, *, store=None, registry=None):
        ch = ProgressChannel()
        agents = {}

        def agent_for(sid, channel, chat):
            if store is not None:
                store.new_session(sid, channel=channel)
            a = agents[sid] = Agent(StubRouter(script), registry or Registry(), store=store, session_id=sid)
            return a
        hub = ChannelHub([ch], agent_for, log=lambda m: None)
        hub.start()
        self.addCleanup(hub.stop)
        return hub, ch, agents

    def say(self, hub, ch, text):
        hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text=text, user_id="1"))

    def test_help_and_unknown_command(self):
        hub, ch, agents = self._hub(lambda m: answer("handled by the agent"))
        self.say(hub, ch, "/help")
        self.assertIn("/new", ch.sent[-1])
        self.say(hub, ch, "/frobnicate now")  # not a command we know: the agent gets it
        self.assertTrue(wait_for(lambda: "handled by the agent" in ch.sent))

    def test_status_and_model_switch(self):
        hub, ch, agents = self._hub(lambda m: answer("ok"))
        self.say(hub, ch, "/status")
        self.assertIn("model: main → main-model", ch.sent[-1])
        self.assertIn("fallback: backup → backup-model", ch.sent[-1])
        self.say(hub, ch, "/model")
        self.assertIn("▶ main", ch.sent[-1])
        self.say(hub, ch, "/model nope")
        self.assertIn("No model named", ch.sent[-1])
        self.say(hub, ch, "/model backup")
        self.assertIn("now uses backup", ch.sent[-1])
        self.say(hub, ch, "hello")
        self.assertTrue(wait_for(lambda: "ok" in ch.sent))
        agent = agents["fake-1"]
        self.assertEqual(agent.router.calls[-1]["provider"], "backup")
        self.say(hub, ch, "/model default")
        self.assertIsNone(agent.provider)

    def test_stop_cancels_a_running_turn(self):
        started = threading.Event()
        reg = Registry()

        @reg.tool("slow", "takes a while", {"type": "object", "properties": {}})
        def _slow(args):
            started.set()
            time.sleep(0.3)
            return "done"
        hub, ch, agents = self._hub(lambda m: tool_call("slow", cid=f"c{len(m)}"), registry=reg)
        self.say(hub, ch, "do the long thing")
        self.assertTrue(started.wait(5))
        self.say(hub, ch, "/stop")
        self.assertTrue(wait_for(lambda: "⏹ Stopped." in ch.sent), ch.sent)
        agent = agents["fake-1"]
        # every tool call in the history has its result, so the next turn is a valid conversation
        ids = [c["id"] for m in agent.history for c in (m.get("tool_calls") or [])]
        results = [m["tool_call_id"] for m in agent.history if m.get("role") == "tool"]
        self.assertEqual(ids, results)
        self.say(hub, ch, "/stop")
        self.assertIn("Nothing running", ch.sent[-1])

    def test_new_archives_the_conversation(self):
        store = Store(Path(tempfile.mkdtemp()) / "s.db")
        hub, ch, agents = self._hub(lambda m: answer("siap"), store=store)
        self.say(hub, ch, "ingat ini")
        self.assertTrue(wait_for(lambda: "siap" in ch.sent))
        self.say(hub, ch, "/new")
        self.assertIn("Fresh conversation", ch.sent[-1])
        archived = [r["id"] for r in store.sessions(10) if r["id"].startswith("fake-1~")]
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(store.session_messages(archived[0])), 2)
        self.assertEqual(store.session_messages("fake-1"), [])
        self.say(hub, ch, "halo lagi")
        self.assertTrue(wait_for(lambda: ch.sent.count("siap") == 2))
        fresh = agents["fake-1"]
        self.assertEqual([m["content"] for m in fresh.history[1:]], ["halo lagi", "siap"])

    def test_progress_message_during_a_turn(self):
        reg = Registry()

        @reg.tool("web_search", "search", {"type": "object", "properties": {"query": {"type": "string"}}})
        def _search(args):
            time.sleep(1.8)  # long enough for the first progress post
            return "results"
        steps = iter([tool_call("web_search", '{"query": "berita AI"}'), answer("Ini beritanya.")])
        hub, ch, agents = self._hub(lambda m: next(steps), registry=reg)
        self.say(hub, ch, "cari berita AI")
        self.assertTrue(wait_for(lambda: "Ini beritanya." in ch.sent))
        self.assertTrue(ch.posts and "search berita AI" in ch.posts[0])
        self.assertEqual(ch.deleted, ["m1"])  # gone before the answer


class FileAccessTests(unittest.TestCase):
    def _reg(self, access, approve):
        from smooth_agent.tools import Workspace, default_registry
        self.asked = []

        def approver(name, args):
            self.asked.append((name, args.get("path")))
            return approve
        ws = Workspace(tempfile.mkdtemp(), access=access)
        return ws, default_registry(ws, allow_http=False, approver=approver)

    def setUp(self):
        self.outside = Path(tempfile.mkdtemp())
        (self.outside / "notes.txt").write_text("hello from outside")
        (self.outside / ".ssh").mkdir()
        (self.outside / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
        (self.outside / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAA public")
        (self.outside / ".env").write_text("API_KEY=secret")

    def test_workspace_mode_blocks_outside(self):
        ws, reg = self._reg("workspace", True)
        out = reg.run("read_file", {"path": str(self.outside / "notes.txt")})
        self.assertIn("outside the workspace", out)
        self.assertIn('files = "full"', out)
        self.assertIn("outside the workspace", reg.run("read_file", {"path": "~/.bashrc"}))
        self.assertEqual(self.asked, [])

    def test_full_mode_reads_anywhere(self):
        ws, reg = self._reg("full", False)
        self.assertIn("hello from outside", reg.run("read_file", {"path": str(self.outside / "notes.txt")}))
        self.assertIn("notes.txt", reg.run("list_dir", {"path": str(self.outside)}))
        self.assertIn("public", reg.run("read_file", {"path": str(self.outside / ".ssh" / "id_ed25519.pub")}))
        self.assertEqual(self.asked, [])  # plain reads never ask

    def test_full_mode_secrets_and_writes_ask(self):
        ws, reg = self._reg("full", False)
        out = reg.run("read_file", {"path": str(self.outside / ".ssh" / "id_ed25519")})
        self.assertIn("did not allow", out)
        self.assertNotIn("PRIVATE KEY", out)
        self.assertIn("did not allow", reg.run("read_file", {"path": str(self.outside / ".env")}))
        target = self.outside / "new.txt"
        self.assertIn("did not allow", reg.run("write_file", {"path": str(target), "content": "x"}))
        self.assertFalse(target.exists())
        self.assertEqual([a[0] for a in self.asked], ["files: read a file that may hold secrets"] * 2
                         + ["files: write outside the workspace"])
        # searching a folder never greps through the keys in it
        self.assertEqual(reg.run("search_files", {"pattern": "PRIVATE", "path": str(self.outside)}), "(no matches)")
        self.assertIn("notes.txt", reg.run("search_files", {"pattern": "hello", "path": str(self.outside)}))

    def test_full_mode_approved_write_and_edit(self):
        ws, reg = self._reg("full", True)
        target = self.outside / "conf.ini"
        self.assertIn(f"to {target}", reg.run("write_file", {"path": str(target), "content": "a=1"}))
        self.assertIn(f"in {target}", reg.run("patch", {"path": str(target), "old_string": "a=1", "new_string": "a=2"}))
        self.assertEqual(target.read_text(), "a=2")
        self.assertIn("API_KEY", reg.run("read_file", {"path": str(self.outside / ".env")}))  # approved
        # inside the workspace nothing is asked, even with full access
        n = len(self.asked)
        reg.run("write_file", {"path": "local.txt", "content": "x"})
        self.assertEqual(len(self.asked), n)


class PersonaTemplateTests(unittest.TestCase):
    def test_templates_are_created_empty_and_unused(self):
        from smooth_agent.persona import PERSONA_FILES, ensure_persona
        d = Path(tempfile.mkdtemp())
        self.assertEqual(ensure_persona(d), list(PERSONA_FILES))
        self.assertEqual(ensure_persona(d), [])  # never overwrites
        self.assertEqual(persona_text([d]), "")   # templates alone add nothing to the prompt
        (d / "USER.md").write_text((d / "USER.md").read_text() + "Name: Angga\n")
        self.assertIn("Name: Angga", persona_text([d]))

    def test_template_does_not_hide_a_filled_copy(self):
        from smooth_agent.persona import ensure_persona
        ws, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        ensure_persona(ws)
        (home / "SOUL.md").write_text("# SOUL\nCalm and kind.")
        self.assertIn("Calm and kind.", persona_text([ws, home]))

    def test_default_workspace_is_in_home_with_templates(self):
        from smooth_agent.runtime import build_runtime
        home = Path(tempfile.mkdtemp())
        cfg = home / "config.toml"
        cfg.write_text('default = "m"\n[providers.m]\nprovider = "openai-compatible"\nmodel = "x"\n'
                       'base_url = "http://127.0.0.1:9/v1"\nkey_env = "K"\n')
        with mock.patch.dict(os.environ, {"SMOOTH_AGENT_HOME": str(home), "K": "k"}):
            with build_runtime(str(cfg), connect_mcp=False) as rt:
                self.assertEqual(rt.workspace.root, (home / "workspace").resolve())
        self.assertTrue((home / "workspace" / "SOUL.md").exists())


class WhereaboutsTests(unittest.TestCase):
    def test_runtime_tells_the_agent_its_paths(self):
        from smooth_agent.runtime import build_runtime
        home = Path(tempfile.mkdtemp())
        cfg = home / "config.toml"
        cfg.write_text('default = "m"\n[providers.m]\nprovider = "openai-compatible"\nmodel = "x"\n'
                       'base_url = "http://127.0.0.1:9/v1"\nkey_env = "K"\n')
        with mock.patch.dict(os.environ, {"SMOOTH_AGENT_HOME": str(home), "K": "k"}):
            with build_runtime(str(cfg), connect_mcp=False, workspace=str(home / "ws")) as rt:
                sysprompt = rt.agent.system
        self.assertIn(f"home {home}", sysprompt)
        self.assertIn(f"config {cfg}", sysprompt)
        self.assertIn(f"workspace {home / 'ws'}", sysprompt)
        self.assertIn("Terminal/shell tools: off", sysprompt)


if __name__ == "__main__":
    unittest.main()
