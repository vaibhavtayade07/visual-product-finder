"""Alembic migration runner (single head, linear; architecture §7)."""
from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config

from .settings import PROJECT_ROOT

log = logging.getLogger(__name__)


def sync_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


def run_migrations(database_url: str) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_url(database_url))
    log.info("running alembic migrations")
    command.upgrade(cfg, "head")


def downgrade(database_url: str, revision: str = "-1") -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_url(database_url))
    command.downgrade(cfg, revision)
