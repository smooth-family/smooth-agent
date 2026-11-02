"""The Smooth Agent banner and small terminal helpers (colours, steps, ticks).

Stdlib only. Colour is used only on a real terminal and never when NO_COLOR is
set (https://no-color.org); FORCE_COLOR=1 turns it on for pipes (CI logs). On a
terminal that cannot print the block characters, a plain-text title is used.
"""
from __future__ import annotations

import os
import sys
from typing import TextIO

# ANSI Shadow letters, built from glyphs so the columns always line up.
_GLYPHS = {
    "S": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "H": ["██╗  ██╗", "██║  ██║", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
}
# a teal -> violet gradient, one colour per row (xterm-256)
_GRADIENT = (51, 45, 39, 63, 99, 135)
TAGLINE = "a g e n t   ·   self-hosted   ·   any model   ·   your keys"


def word_rows(word: str) -> list[str]:
    return ["".join(_GLYPHS[ch][row] for ch in word) for row in range(6)]


def use_color(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


def can_draw(stream: TextIO | None = None) -> bool:
    enc = getattr(stream or sys.stdout, "encoding", None) or "ascii"
    try:
        "█╗║═".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def paint(text: str, code: str, stream: TextIO | None = None) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color(stream) else text


def bold(t: str) -> str:
    return paint(t, "1")


def dim(t: str) -> str:
    return paint(t, "2")


def green(t: str) -> str:
    return paint(t, "32")


def yellow(t: str) -> str:
    return paint(t, "33")


def red(t: str) -> str:
    return paint(t, "31")


def cyan(t: str) -> str:
    return paint(t, "36")


def banner(version: str = "", stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    if not can_draw(stream):
        return f"Smooth Agent {version}".rstrip() + "\n"
    color = use_color(stream)
    lines = []
    for row, text in zip(_GRADIENT, word_rows("SMOOTH")):
        lines.append(f"  \033[1;38;5;{row}m{text}\033[0m" if color else f"  {text}")
    tag = f"  {TAGLINE}" + (f"   v{version}" if version else "")
    lines.append(f"\033[38;5;141m{tag}\033[0m" if color else tag)
    return "\n" + "\n".join(lines) + "\n"


def print_banner(version: str = "", stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    stream.write(banner(version, stream) + "\n")
    stream.flush()


class Steps:
    """Numbered progress lines: `  [1/4] Provider ..... deepseek  ✓`."""

    def __init__(self, total: int, stream: TextIO | None = None):
        self.total = total
        self.n = 0
        self.stream = stream or sys.stdout
        self.tick = "✓" if can_draw(self.stream) else "ok"
        self.cross = "✗" if can_draw(self.stream) else "x"

    def start(self, title: str) -> None:
        self.n += 1
        self.stream.write(f"\n  {cyan(f'[{self.n}/{self.total}]')} {bold(title)}\n")
        self.stream.flush()

    def ok(self, msg: str) -> None:
        self.stream.write(f"        {green(self.tick)} {msg}\n")

    def warn(self, msg: str) -> None:
        self.stream.write(f"        {yellow('!')} {msg}\n")

    def fail(self, msg: str) -> None:
        self.stream.write(f"        {red(self.cross)} {msg}\n")

    def note(self, msg: str) -> None:
        self.stream.write(f"          {dim(msg)}\n")


__all__ = ["banner", "print_banner", "Steps", "use_color", "can_draw", "word_rows", "TAGLINE",
           "bold", "dim", "green", "yellow", "red", "cyan"]
