"""Migration runner — discovers and applies pending migrations on startup.

Usage:
    from server.migrations.runner import run_migrations
    run_migrations()  # Call once during app lifespan init
"""

import importlib
import logging
import os
import pkgutil
import sqlite3
from contextlib import contextmanager

log = logging.getLogger("whisper-studio")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
DB_PATH = os.path.join(STORAGE_DIR, "sessions.db")


@contextmanager
def _get_conn():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {row["version"] for row in rows}


def _discover_migrations() -> list[dict]:
    """Discover migration modules in server/migrations/ matching pattern NNN_*.py."""
    migrations_dir = os.path.dirname(__file__)
    migrations = []

    for info in pkgutil.iter_modules([migrations_dir]):
        if info.name.startswith("_") or info.name == "runner":
            continue
        # Only include files starting with digits (e.g., 001_add_cost_table)
        if not info.name[0].isdigit():
            continue

        module = importlib.import_module(f"server.migrations.{info.name}")
        version = getattr(module, "VERSION", None)
        description = getattr(module, "DESCRIPTION", info.name)
        migrate_fn = getattr(module, "migrate", None)

        if version is None or migrate_fn is None:
            log.warning("Migration %s missing VERSION or migrate(), skipping", info.name)
            continue

        migrations.append(
            {
                "version": version,
                "description": description,
                "migrate": migrate_fn,
                "name": info.name,
            }
        )

    migrations.sort(key=lambda m: m["version"])
    return migrations


def run_migrations() -> int:
    """Apply all pending migrations. Returns count of migrations applied."""
    with _get_conn() as conn:
        _ensure_schema_version_table(conn)
        applied = _get_applied_versions(conn)
        migrations = _discover_migrations()

        count = 0
        for m in migrations:
            if m["version"] in applied:
                continue
            log.info(
                "Applying migration %03d: %s",
                m["version"],
                m["description"],
            )
            try:
                m["migrate"](conn)
                conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                    (m["version"], m["description"]),
                )
                conn.commit()
                count += 1
                log.info("Migration %03d applied successfully", m["version"])
            except Exception:
                conn.rollback()
                log.error(
                    "Migration %03d failed — stopping migration runner",
                    m["version"],
                    exc_info=True,
                )
                raise

    if count:
        log.info("Applied %d migration(s)", count)
    else:
        log.debug("No pending migrations")
    return count
