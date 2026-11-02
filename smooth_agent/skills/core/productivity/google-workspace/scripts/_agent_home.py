"""Resolve SMOOTH_AGENT_HOME for standalone skill scripts.

Skill scripts may run outside the Smooth Agent process (e.g. system Python,
nix env, CI) where ``smooth_constants`` is not importable.  This module
provides the same ``get_agent_home()`` and ``display_agent_home()``
contracts as ``smooth_constants`` without requiring it on ``sys.path``.

When ``smooth_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``smooth_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``SMOOTH_AGENT_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from smooth_constants import display_agent_home as display_agent_home
    from smooth_constants import get_agent_home as get_agent_home
except (ModuleNotFoundError, ImportError):

    def get_agent_home() -> Path:
        """Return the Smooth Agent home directory (default: ~/.smooth-agent).

        Mirrors ``smooth_constants.get_agent_home()``."""
        val = os.environ.get("SMOOTH_AGENT_HOME", "").strip()
        return Path(val) if val else Path.home() / ".smooth-agent"

    def display_agent_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``smooth_constants.display_agent_home()``."""
        home = get_agent_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
