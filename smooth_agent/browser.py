"""Browser automation for Smooth Agent (Playwright, Chromium).

Optional: `pip install "smooth-agent[browser]" && python -m playwright install chromium`,
then `[browser] enabled = true` in smooth-agent.toml.

The model sees a page as a compact snapshot: title, URL, the interactive elements
with ref ids ([@e1] button "Sign in", [@e2] textbox "Email" …) and the page text.
It acts with browser_click / browser_type / browser_press / browser_scroll using
those refs, and can ask the vision model about a screenshot (browser_vision).

All Playwright calls run on one dedicated thread (its sync API is not
thread-safe, and sub-agents run in parallel), so tools submit work to that thread.
A persistent profile under $SMOOTH_AGENT_HOME/browser/profile keeps logins.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .tools import Registry, ToolError, Workspace

SNAPSHOT_TEXT_CHARS = 6000
FULL_TEXT_CHARS = 15000
MAX_ELEMENTS = 150

# Marks interactive, visible elements with data-sa-ref and describes them.
_COLLECT_JS = r"""
(maxEls) => {
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],'
    + '[role=checkbox],[role=radio],[role=switch],[role=option],[role=combobox],[role=textbox],[contenteditable=""],'
    + '[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-sa-ref]').forEach(e => e.removeAttribute('data-sa-ref'));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
    if (el.type === 'hidden') continue;
    n += 1;
    const ref = 'e' + n;
    el.setAttribute('data-sa-ref', ref);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'combobox'
      : tag === 'textarea' ? 'textbox' : tag === 'input' ? ((['checkbox','radio','submit','button'].includes(el.type)) ? el.type : 'textbox') : tag);
    let name = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || el.innerText || el.value || el.getAttribute('alt') || '';
    if (!name && el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) name = l.innerText; }
    name = (name || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const item = {ref, role, name};
    if (tag === 'a' && el.href) item.href = el.href.slice(0, 120);
    if ((tag === 'input' || tag === 'textarea') && el.value && el.type !== 'password') item.value = String(el.value).slice(0, 60);
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = !!el.checked;
    const inView = r.bottom > 0 && r.top < innerHeight;
    if (!inView) item.offscreen = true;
    out.push(item);
    if (out.length >= maxEls) break;
  }
  return out;
}
"""


@dataclass
class BrowserSettings:
    enabled: bool = False
    headless: bool = True
    profile_dir: str = ""        # default $SMOOTH_AGENT_HOME/browser/profile ("" = persistent, "none" = fresh each run)
    timeout_ms: int = 30_000
    viewport: str = "1280x900"
    user_agent: str = ""

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "BrowserSettings":
        b = cfg.get("browser") or {}
        return cls(enabled=bool(b.get("enabled", False)), headless=bool(b.get("headless", True)),
                   profile_dir=str(b.get("profile_dir", "")), timeout_ms=int(b.get("timeout_ms", 30_000)),
                   viewport=str(b.get("viewport", "1280x900")), user_agent=str(b.get("user_agent", "")))


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


class BrowserSession:
    """One Chromium context + page, driven from a dedicated worker thread."""

    def __init__(self, settings: BrowserSettings, profile_root: Path | None = None):
        self.s = settings
        self.profile_root = profile_root
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pw = self._ctx = self._page = None
        self.console: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()

    # ----- worker thread plumbing -----
    def _run(self) -> None:
        while True:
            fn, box, done = self._jobs.get()
            if fn is None:
                break
            try:
                box["ok"] = fn()
            except Exception as e:  # handed back to the caller
                box["err"] = e
            done.set()
        self._close_in_thread()

    def call(self, fn: Callable[[], Any], timeout: float = 120) -> Any:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="smooth-browser", daemon=True)
                self._thread.start()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._jobs.put((fn, box, done))
        if not done.wait(timeout):
            raise ToolError("the browser did not respond in time")
        if "err" in box:
            e = box["err"]
            raise e if isinstance(e, ToolError) else ToolError(f"browser: {type(e).__name__}: {str(e).splitlines()[0][:300]}")
        return box.get("ok")

    def close(self) -> None:
        if self._thread and self._thread.is_alive():
            self._jobs.put((None, None, None))
            self._thread.join(timeout=10)

    # ----- runs on the worker thread -----
    def _ensure(self):
        if self._page and not self._page.is_closed():
            return self._page
        if not available():
            raise ToolError('browser support is not installed: pip install "smooth-agent[browser]" && python -m playwright install chromium')
        from playwright.sync_api import sync_playwright
        if not self._pw:
            self._pw = sync_playwright().start()
        w, _, h = self.s.viewport.partition("x")
        opts: dict[str, Any] = {"headless": self.s.headless, "viewport": {"width": int(w or 1280), "height": int(h or 900)}}
        if self.s.user_agent:
            opts["user_agent"] = self.s.user_agent
        profile = self.s.profile_dir
        if profile != "none":
            path = Path(profile).expanduser() if profile else (self.profile_root or Path.home() / ".smooth-agent") / "browser" / "profile"
            path.mkdir(parents=True, exist_ok=True)
            self._ctx = self._pw.chromium.launch_persistent_context(str(path), **opts)
        else:
            browser = self._pw.chromium.launch(headless=self.s.headless)
            self._ctx = browser.new_context(**{k: v for k, v in opts.items() if k != "headless"})
        self._ctx.set_default_timeout(self.s.timeout_ms)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._hook(self._page)
        self._ctx.on("page", self._on_new_page)
        return self._page

    def _hook(self, page) -> None:
        page.on("console", lambda m: self.console.append(f"[{m.type}] {m.text[:500]}"))
        page.on("pageerror", lambda e: self.console.append(f"[pageerror] {str(e)[:500]}"))

        def on_dialog(d):
            self.console.append(f"[dialog {d.type}] {d.message[:300]} (auto-{'accepted' if d.type != 'beforeunload' else 'dismissed'})")
            try:
                d.accept() if d.type != "beforeunload" else d.dismiss()
            except Exception:
                pass
        page.on("dialog", on_dialog)

    def _on_new_page(self, page) -> None:
        # a link that opens a new tab: follow it
        self._hook(page)
        self._page = page

    def _close_in_thread(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._ctx = self._page = None

    def _settle(self, page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=self.s.timeout_ms)
            page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass  # busy pages never go idle; the DOM is there

    def _locator(self, page, ref: str):
        r = str(ref or "").strip().lstrip("@")
        if not r:
            raise ToolError("ref is required (e.g. @e3 from the snapshot)")
        loc = page.locator(f'[data-sa-ref="{r}"]')
        if loc.count() == 0:
            raise ToolError(f"no element @{r} on the page any more; take a new browser_snapshot")
        return loc.first

    def snapshot_now(self, full: bool = False) -> str:
        page = self._ensure()
        els = page.evaluate(_COLLECT_JS, MAX_ELEMENTS)
        try:
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            text = ""
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        cap = FULL_TEXT_CHARS if full else SNAPSHOT_TEXT_CHARS
        lines = [f"title: {page.title()}", f"url: {page.url}", "", "interactive elements:"]
        for e in els:
            extra = "".join([f" href={e['href']}" if e.get("href") else "", f" value={e['value']!r}" if e.get("value") else "",
                             f" checked={e['checked']}" if "checked" in e else "", " (offscreen)" if e.get("offscreen") else ""])
            lines.append(f"[@{e['ref']}] {e['role']} \"{e['name']}\"{extra}")
        if not els:
            lines.append("(none)")
        lines += ["", "page text:", text[:cap] + (f"\n...[{len(text) - cap} more chars; browser_snapshot full=true or scroll]" if len(text) > cap else "")]
        return "\n".join(lines)

    # ----- actions (each runs on the worker) -----
    def navigate(self, url: str) -> str:
        if not str(url).startswith(("http://", "https://")):
            raise ToolError("url must start with http:// or https://")

        def go():
            page = self._ensure()
            resp = page.goto(url, wait_until="domcontentloaded")
            self._settle(page)
            status = f"HTTP {resp.status}\n" if resp is not None else ""
            return status + self.snapshot_now()
        return self.call(go)

    def snapshot(self, full: bool = False) -> str:
        return self.call(lambda: self.snapshot_now(full))

    def click(self, ref: str) -> str:
        def go():
            page = self._ensure()
            self._locator(page, ref).click()
            self._settle(page)
            time.sleep(0.3)
            return self.snapshot_now()
        return self.call(go)

    def type(self, ref: str, text: str, submit: bool = False, clear: bool = True) -> str:
        def go():
            page = self._ensure()
            loc = self._locator(page, ref)
            if clear:
                loc.fill(str(text))
            else:
                loc.press_sequentially(str(text))
            if submit:
                loc.press("Enter")
                self._settle(page)
            return self.snapshot_now()
        return self.call(go)

    def press(self, key: str) -> str:
        def go():
            page = self._ensure()
            page.keyboard.press(str(key))
            self._settle(page)
            return self.snapshot_now()
        return self.call(go)

    def scroll(self, direction: str, amount: int = 0) -> str:
        def go():
            page = self._ensure()
            dy = int(amount or 700) * (-1 if str(direction).lower() == "up" else 1)
            page.mouse.wheel(0, dy)
            time.sleep(0.4)
            return self.snapshot_now()
        return self.call(go)

    def back(self) -> str:
        def go():
            page = self._ensure()
            page.go_back()
            self._settle(page)
            return self.snapshot_now()
        return self.call(go)

    def screenshot(self, full_page: bool = False) -> bytes:
        return self.call(lambda: self._ensure().screenshot(full_page=full_page, type="png"))

    def images(self) -> str:
        def go():
            page = self._ensure()
            imgs = page.evaluate("() => [...document.images].filter(i => i.naturalWidth > 32).slice(0, 60)"
                                 ".map(i => ({src: i.currentSrc || i.src, alt: i.alt || '', w: i.naturalWidth, h: i.naturalHeight}))")
            return "\n".join(f"{i['w']}x{i['h']} {i['src'][:200]} alt={i['alt'][:80]!r}" for i in imgs) or "(no images)"
        return self.call(go)


def add_browser_tools(reg: Registry, session: BrowserSession, workspace: Workspace,
                      describe_image: Callable[[bytes, str], str] | None = None) -> None:
    ref = {"type": "string", "description": "element ref from the snapshot, e.g. @e3"}

    @reg.tool("browser_navigate", "Open a URL in the browser and return a snapshot of the page (interactive elements with refs + text).",
              {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    def _nav(a):
        return session.navigate(str(a["url"]))

    @reg.tool("browser_snapshot", "Refresh the snapshot of the current page. full=true includes much more of the page text.",
              {"type": "object", "properties": {"full": {"type": "boolean"}}, "required": []})
    def _snap(a):
        return session.snapshot(bool(a.get("full")))

    @reg.tool("browser_click", "Click an element by its ref (e.g. @e5). Returns the new snapshot.",
              {"type": "object", "properties": {"ref": ref}, "required": ["ref"]})
    def _click(a):
        return session.click(a["ref"])

    @reg.tool("browser_type", "Type text into an input by its ref. submit=true presses Enter after; clear=false appends.",
              {"type": "object", "properties": {"ref": ref, "text": {"type": "string"}, "submit": {"type": "boolean"},
                                                "clear": {"type": "boolean"}}, "required": ["ref", "text"]})
    def _type(a):
        return session.type(a["ref"], a.get("text", ""), bool(a.get("submit")), a.get("clear", True) is not False)

    @reg.tool("browser_press", "Press a key on the page (Enter, Tab, Escape, ArrowDown, Control+A …).",
              {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]})
    def _press(a):
        return session.press(str(a["key"]))

    @reg.tool("browser_scroll", "Scroll the page up or down (amount in pixels, default 700).",
              {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"]},
                                                "amount": {"type": "integer"}}, "required": ["direction"]})
    def _scroll(a):
        return session.scroll(str(a["direction"]), int(a.get("amount") or 0))

    @reg.tool("browser_back", "Go back to the previous page.", {"type": "object", "properties": {}, "required": []})
    def _back(_a):
        return session.back()

    @reg.tool("browser_console", "Console messages, page errors and dialogs seen so far. clear=true empties the log.",
              {"type": "object", "properties": {"clear": {"type": "boolean"}}, "required": []})
    def _console(a):
        lines = list(session.console)
        if a.get("clear"):
            session.console.clear()
        return "\n".join(lines[-100:]) or "(no console messages)"

    @reg.tool("browser_get_images", "List the images on the current page (size, URL, alt text).",
              {"type": "object", "properties": {}, "required": []})
    def _imgs(_a):
        return session.images()

    @reg.tool("browser_screenshot", "Save a PNG screenshot of the page in the workspace (screenshots/). full_page=true for the whole page.",
              {"type": "object", "properties": {"full_page": {"type": "boolean"}, "filename": {"type": "string"}}, "required": []})
    def _shot(a):
        data = session.screenshot(bool(a.get("full_page")))
        d = workspace.resolve("screenshots")
        d.mkdir(exist_ok=True)
        name = "".join(c for c in str(a.get("filename") or "") if c.isalnum() or c in "-_") or time.strftime("%Y%m%d-%H%M%S")
        p = d / f"{name}.png"
        p.write_bytes(data)
        return f"saved {workspace.show(p)}"

    if describe_image is not None:
        @reg.tool("browser_vision", "Look at the current page as an image and answer a question (layout, charts, captchas you may "
                  "not solve, what is visible). Uses the vision model.",
                  {"type": "object", "properties": {"question": {"type": "string"}, "full_page": {"type": "boolean"}},
                   "required": ["question"]})
        def _vision(a):
            return describe_image(session.screenshot(bool(a.get("full_page"))), str(a["question"]))


__all__ = ["BrowserSettings", "BrowserSession", "add_browser_tools", "available"]
