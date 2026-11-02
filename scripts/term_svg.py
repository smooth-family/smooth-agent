#!/usr/bin/env python3
"""Render real Smooth Agent terminal output as SVG images for the README.

    python scripts/term_svg.py            # regenerates docs/assets/*.svg

Each screenshot runs the actual command in a throwaway home with colours
forced on, then draws the coloured output inside a terminal window. The block
and box-drawing characters of the banner are drawn as shapes, not font glyphs,
so they look the same in every browser. Only the temporary paths are replaced
with `~/.smooth-agent` so the images do not show /tmp/xyz.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "assets"
sys.path.insert(0, str(REPO))

BG, FG, BAR = "#0d1117", "#c9d1d9", "#161b22"
FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', 'Liberation Mono', monospace"
SIZE, CW, LH, PAD = 14, 8.43, 20, 22  # font px, char advance, line height, padding
BASIC = {30: "#484f58", 31: "#ff7b72", 32: "#3fb950", 33: "#d29922", 34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf",
         37: "#b1bac4", 90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341", 94: "#79c0ff", 95: "#d2a8ff",
         96: "#56d4dd", 97: "#f0f6fc"}
SGR = re.compile(r"\x1b\[([0-9;]*)m")
BOX = set("█═║╔╗╚╝")


def xterm256(n: int) -> str:
    if n < 16:
        return BASIC.get(30 + n if n < 8 else 82 + n, FG)
    if n < 232:
        n -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (lv[n // 36], lv[(n // 6) % 6], lv[n % 6])
    g = 8 + 10 * (n - 232)
    return "#%02x%02x%02x" % (g, g, g)


def parse(text: str) -> list[list[tuple[str, dict]]]:
    """ANSI text -> lines of (chunk, style) runs."""
    lines, state = [], {"fg": None, "bold": False, "dim": False}
    for raw in text.rstrip("\n").split("\n"):
        runs, pos = [], 0
        for m in SGR.finditer(raw):
            if m.start() > pos:
                runs.append((raw[pos:m.start()], dict(state)))
            codes = [int(c) for c in m.group(1).split(";") if c] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    state = {"fg": None, "bold": False, "dim": False}
                elif c == 1:
                    state["bold"] = True
                elif c == 2:
                    state["dim"] = True
                elif c in BASIC:
                    state["fg"] = BASIC[c]
                elif c == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                    state["fg"] = xterm256(codes[i + 2])
                    i += 2
                i += 1
            pos = m.end()
        if pos < len(raw):
            runs.append((raw[pos:], dict(state)))
        lines.append(runs)
    return lines


def box_shape(ch: str, x: float, y: float, w: float, h: float, color: str) -> str:
    """One block / double-line box character as SVG shapes (y = top of the cell)."""
    if ch == "█":
        return f'<rect x="{x:.2f}" y="{y:.2f}" width="{w + 0.4:.2f}" height="{h + 0.4:.2f}" fill="{color}"/>'
    cx, cy, t = x + w / 2, y + h / 2, max(1.6, w * 0.22)
    segs = {"═": [(x, cy, x + w, cy)], "║": [(cx, y, cx, y + h)],
            "╔": [(cx, cy, x + w, cy), (cx, cy, cx, y + h)], "╗": [(x, cy, cx, cy), (cx, cy, cx, y + h)],
            "╚": [(cx, y, cx, cy), (cx, cy, x + w, cy)], "╝": [(cx, y, cx, cy), (x, cy, cx, cy)]}[ch]
    return "".join(f'<line x1="{a:.2f}" y1="{b:.2f}" x2="{c:.2f}" y2="{d:.2f}" stroke="{color}" '
                   f'stroke-width="{t:.2f}" stroke-linecap="square" opacity="0.55"/>' for a, b, c, d in segs)


def render(text: str, title: str) -> str:
    lines = parse(text)
    cols = max((sum(len(c) for c, _ in ln) for ln in lines), default=20)
    width = int(PAD * 2 + cols * CW)
    top = 36
    height = int(top + PAD + len(lines) * LH + PAD * 0.6)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'role="img" aria-label="{escape(title)}">',
           f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>',
           f'<path d="M0 10a10 10 0 0 1 10-10h{width - 20}a10 10 0 0 1 10 10v{top - 10}H0z" fill="{BAR}"/>',
           *[f'<circle cx="{20 + i * 20}" cy="18" r="6" fill="{c}"/>' for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f"))],
           f'<text x="{width / 2:.0f}" y="23" fill="#8b949e" font-family="{FONT}" font-size="12" text-anchor="middle">{escape(title)}</text>']
    for row, runs in enumerate(lines):
        y_top = top + PAD + row * LH - LH * 0.78
        base = top + PAD + row * LH
        col = 0
        spans = []
        for chunk, st in runs:
            color = st["fg"] or FG
            buf, buf_col = "", col
            for ch in chunk:
                if ch in BOX:
                    if buf:
                        spans.append((buf_col, buf, st))
                        buf = ""
                    out.append(box_shape(ch, PAD + col * CW, y_top + 1, CW, LH, color))
                    col += 1
                    buf_col = col
                else:
                    if not buf:
                        buf_col = col
                    buf += ch
                    col += 1
            if buf:
                spans.append((buf_col, buf, st))
        for c0, s, st in spans:
            if not s.strip():
                continue
            attrs = f'fill="{st["fg"] or FG}"'
            if st["bold"]:
                attrs += ' font-weight="700"'
            if st["dim"]:
                attrs += ' opacity="0.6"'
            out.append(f'<text x="{PAD + c0 * CW:.2f}" y="{base:.2f}" {attrs} font-family="{FONT}" font-size="{SIZE}" '
                       f'xml:space="preserve">{escape(s)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def banner_svg() -> str:
    """The README header: SMOOTH in block letters with a teal->violet gradient."""
    from smooth_agent.banner import word_rows
    rows = word_rows("SMOOTH")
    cw, ch = 11.0, 21.0
    w, h = 900, 300
    x0 = (w - len(rows[0]) * cw) / 2
    y0 = 52
    x1, y1 = x0 + len(rows[0]) * cw, y0 + 6 * ch
    stops = ('<stop offset="0" stop-color="#00e5ff"/><stop offset="0.5" stop-color="#5b8cff"/>'
             '<stop offset="1" stop-color="#b65cff"/>')
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
             'aria-label="Smooth Agent">',
             # one gradient across the whole word (user space), not one per block
             f'<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" x1="{x0:.1f}" y1="{y0:.1f}" '
             f'x2="{x1:.1f}" y2="{y1:.1f}">{stops}</linearGradient>'
             f'<linearGradient id="t" x1="0" y1="0" x2="1" y2="0">{stops}</linearGradient>'
             '<radialGradient id="bg" cx="0.5" cy="0" r="1"><stop offset="0" stop-color="#161b33"/>'
             '<stop offset="1" stop-color="#0b0e17"/></radialGradient></defs>',
             f'<rect width="{w}" height="{h}" rx="18" fill="url(#bg)"/>']
    for r, line in enumerate(rows):
        for c, glyph in enumerate(line):
            if glyph in BOX:
                parts.append(box_shape(glyph, x0 + c * cw, y0 + r * ch, cw, ch, "url(#g)" if glyph == "█" else "#7c6cff"))
    ty = y0 + 6 * ch + 46
    parts.append(f'<text x="{w / 2}" y="{ty}" text-anchor="middle" font-family="{FONT}" font-size="26" '
                 'font-weight="700" letter-spacing="14" fill="url(#t)">AGENT</text>')
    parts.append(f'<text x="{w / 2}" y="{ty + 34}" text-anchor="middle" font-family="{FONT}" font-size="15" '
                 'fill="#8b949e">self-hosted · any model · your keys · chat from anywhere</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def run(cmd: list[str], home: Path, *, env_extra: dict | None = None, stdin: str | None = None) -> str:
    env = {**os.environ, "SMOOTH_AGENT_HOME": str(home), "FORCE_COLOR": "1", "PYTHONPATH": str(REPO),
           "HOME": str(home.parent), **(env_extra or {})}
    env.pop("NO_COLOR", None)
    env.pop("SMOOTH_AGENT_CONFIG", None)
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=home.parent, input=stdin, timeout=300)
    return (p.stdout + p.stderr).replace(str(home), "~/.smooth-agent").replace(str(home.parent), "~")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "banner.svg").write_text(banner_svg(), encoding="utf-8")
    py = [sys.executable, "-m", "smooth_agent.cli"]
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / ".smooth-agent"
        setup = run(py + ["setup", "--provider", "deepseek", "--key", "sk-demo", "--channels", "telegram,slack"], home)
        (OUT / "setup.svg").write_text(render(setup, "smooth-agent setup"), encoding="utf-8")
        cfg = home / "config.toml"
        text = cfg.read_text().replace('owners = []\nauto_approve', 'owners = ["5512345678"]\nauto_approve', 1)
        cfg.write_text(text)
        env = {"TELEGRAM_BOT_TOKEN": "demo", "SLACK_BOT_TOKEN": "demo", "SLACK_SIGNING_SECRET": "demo"}
        doctor = run(py + ["doctor"], home, env_extra=env)
        doctor = "\n".join(ln for ln in doctor.splitlines() if not re.match(r"^(\x1b\[[0-9;]*m)?(ok |-- )(\x1b\[0m)? (git|node|npx|ffmpeg|gh) ", ln)
                           and "some skills want" not in ln)
        (OUT / "doctor.svg").write_text(render("$ smooth-agent doctor\n" + doctor, "smooth-agent doctor"), encoding="utf-8")
        inst = subprocess.run(["bash", str(REPO / "install.sh"), "--source", str(REPO), "--method", "venv", "--no-setup"],
                              capture_output=True, text=True, timeout=600,
                              env={**os.environ, "HOME": tmp, "FORCE_COLOR": "1", "PATH": "/usr/bin:/bin"})
        out = (inst.stdout + inst.stderr).replace(tmp, "~").replace(str(REPO), "~/smooth-agent")
        out = "$ ./install.sh --source . --method venv --no-setup   # from a checkout\n" + out
        (OUT / "install.svg").write_text(render(out, "install"), encoding="utf-8")
    for f in sorted(OUT.glob("*.svg")):
        print(f"{f.relative_to(REPO)}  {f.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
