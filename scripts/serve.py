"""Start the server with a psycopg-compatible event loop (Windows included).

    uv run python scripts/serve.py [--host 127.0.0.1] [--port 8000]

uvicorn's default loop on Windows is the ProactorEventLoop, which psycopg's async
connections reject; this launcher pins a Selector loop before uvicorn takes over
(``loop="none"``: uvicorn must not install its own loop factory). On Linux it behaves
like plain ``uvicorn app.main:app``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime import ensure_selector_loop_on_windows  # noqa: E402

import uvicorn  # noqa: E402


async def _serve(host: str, port: int, log_level: str) -> None:
    config = uvicorn.Config("app.main:app", host=host, port=port, loop="none",
                            log_level=log_level, workers=1)
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Visual Product Finder server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    ensure_selector_loop_on_windows()
    asyncio.run(_serve(args.host, args.port, args.log_level))


if __name__ == "__main__":
    main()
