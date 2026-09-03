"""Pytest configuration: makes the project root importable so test modules can
do `from server.security.command_validator import ...` without packaging, and
keeps the suite from writing into the running checkout's real app databases.
"""

import importlib
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# Every module that resolves a sqlite path from storage_root() at IMPORT time.
# They all point at the same ``<storage_root()>/sessions.db``.
_DB_MODULES = (
    "server.infrastructure.sessions",
    "server.infrastructure.grounding_store",
    "server.tasks.registry",
    "server.tasks_tracker",
    "server.attachment_store",
    "server.costs.tracker",
    "server.cron_history",
    "server.notifications",
    "server.migrations.runner",
)


@pytest.fixture(scope="session")
def _db_template(tmp_path_factory):
    """One migrated, empty sessions.db, built once and copied per test.

    Tests used to inherit the developer's already-migrated database, so any
    that touched a migration-created table (attachments, cron_runs, …) passed
    only because real app data happened to be there. A per-test copy of this
    template makes them hermetic without paying for a migration run each time.
    """
    template = tmp_path_factory.mktemp("db-template") / "sessions.db"

    from server.infrastructure import sessions
    from server.migrations import runner

    saved = [(m, m.STORAGE_DIR, m.DB_PATH) for m in (sessions, runner)]
    for mod in (sessions, runner):
        mod.STORAGE_DIR = str(template.parent)
        mod.DB_PATH = str(template)
    try:
        sessions._ensure_db()  # base schema (sessions table)
        runner.run_migrations()  # everything versioned on top of it
    finally:
        for mod, storage_dir, db_path in saved:
            mod.STORAGE_DIR = storage_dir
            mod.DB_PATH = db_path
    return str(template)


@pytest.fixture(autouse=True)
def _isolate_user_root(tmp_path_factory, monkeypatch):
    """Keep every test away from the REAL ~/.whisper.

    user_root() resolves to ~/.whisper whenever WHISPER_HOME is set, so any
    test that flips WHISPER_HOME would otherwise read or (worse) bootstrap
    into the developer's live user data. Pinning WHISPER_USER_DIR to a
    per-test tmp makes that impossible; tests that assert the derivation
    itself delete the variable explicitly and fake $HOME instead."""
    monkeypatch.setenv("WHISPER_USER_DIR", str(tmp_path_factory.mktemp("user-root")))


@pytest.fixture(autouse=True)
def _isolate_app_databases(tmp_path_factory, monkeypatch, _db_template):
    """Give every test its own sessions.db.

    Because those paths are computed at import time, patching WHISPER_HOME or
    storage_root() later has no effect — so a test that writes a chat session,
    a background task, a cost row, a cron run, an attachment or a notification
    puts it in the REAL database of whichever checkout is running the suite.
    The developer then finds phantom rows in their own app (a workflow run
    mirrored as a background task is how this surfaced), and a leaked row can
    make a later test pass or fail for the wrong reason.

    Nine test files already patched some of these by hand. This makes isolation
    the default for all of them, including the tests nobody thought to isolate,
    and for any module added later. A test that patches the same names itself
    still wins: its monkeypatch is applied after this fixture's.

    Unlike the lazy sys.modules lookup below, this imports the modules — a hole
    here means real data loss risk, and a module imported mid-test would slip
    through a sys.modules check.

    The storage dir comes from tmp_path_factory, NOT the test's own tmp_path:
    plenty of tests assert on the exact contents of tmp_path, and an app-storage
    directory appearing inside it breaks them.
    """
    storage = tmp_path_factory.mktemp("app-storage")
    db_path = str(storage / "sessions.db")
    shutil.copyfile(_db_template, db_path)
    for name in _DB_MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 — a missing optional module isn't fatal
            continue
        for attr in ("STORAGE_DIR", "_STORAGE_DIR"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, str(storage))
        for attr in ("DB_PATH", "_DB_PATH"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, db_path)


# Every module holding an import-time COPY of the workspace-state file paths.
_WS_STATE_MODULES = (
    "server.workspace.paths",
    "server.workspace.state",
    "server.workspace.routes.recent",
    "server.workspace",
)

_WS_STATE_FILES = {
    "WORKSPACE_CONFIG_PATH": "workspace_config.json",
    "RECENT_WORKSPACES_PATH": "recent_workspaces.json",
    "SAVE_LOCATIONS_PATH": "save_locations.json",
}


@pytest.fixture(autouse=True)
def _isolate_workspace_state(tmp_path_factory, monkeypatch):
    """Give every test its own workspace_config.json / recent_workspaces.json /
    save_locations.json.

    Same import-time-copy problem as the databases above, with a sharper edge.
    A test that connects a workspace and then disconnects writes
    ``{"path": null}`` into the REAL config of whichever WHISPER_HOME is in the
    environment, and connecting also pushes its tmp_path into the real recents
    list. When that WHISPER_HOME is an installed app's Application Support
    directory (a dev backend launched with the packaged app's home, which is
    how this surfaced), the suite silently disconnects the user's RUNNING app:
    the frontend keeps rendering its cached tree while every turn assembles the
    tool pool with ws_connected=False, so the git and GitHub tools disappear
    from the catalog with no visible cause.
    """
    ws_state = tmp_path_factory.mktemp("app-workspace-state")
    for name in _WS_STATE_MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 — a missing optional module isn't fatal
            continue
        for attr, filename in _WS_STATE_FILES.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, str(ws_state / filename))


@pytest.fixture(autouse=True)
def _drop_cached_bedrock_clients():
    """server.chat.infra caches one boto3 bedrock-runtime client per region for
    the whole process. A test whose code path reaches _get_bedrock_client()
    (e.g. POSTing /api/chat, whose endpoint grabs the client before the 409
    guard) leaves a REAL client in that cache; a later test that monkeypatches
    boto3.client never sees its fake because the cache short-circuits client
    creation, and with live AWS credentials the "unit" test silently calls the
    real endpoint. Drop the cache after every test so each builds clients under
    its own patches. Lazy sys.modules lookup: never import the server package
    for tests that don't touch it."""
    yield
    infra = sys.modules.get("server.chat.infra")
    if infra is not None:
        infra._reset_bedrock_client_cache()
    recall = sys.modules.get("server.memory.recall")
    if recall is not None:
        recall._reset_recall_client_cache()
