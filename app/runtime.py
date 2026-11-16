"""Runtime environment helpers (event loop compatibility).

psycopg's async connections reject the Windows ``ProactorEventLoop`` (Windows' default),
so every entrypoint that creates its own loop — tests, seed script, eval harness, the
local serve script — pins ``WindowsSelectorEventLoopPolicy`` first. Linux is a no-op.
"""
from __future__ import annotations

import asyncio
import sys


def ensure_selector_loop_on_windows() -> None:
    if sys.platform == "win32":
        policy_cls = asyncio.WindowsSelectorEventLoopPolicy  # type: ignore[attr-defined]
        if not isinstance(asyncio.get_event_loop_policy(), policy_cls):
            asyncio.set_event_loop_policy(policy_cls())
