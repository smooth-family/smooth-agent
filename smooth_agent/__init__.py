"""Smooth Agent — a self-hostable, multi-router AI agent.

Includes MIT-licensed third-party material; see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

__version__ = "0.3.0"

from .routers import Completion, Provider, Router, RouterError

__all__ = ["Router", "Provider", "Completion", "RouterError", "__version__"]
