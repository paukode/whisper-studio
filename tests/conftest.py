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
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Give every test its own config user layer.

    config.USER_CONFIG_PATH and CONFIG_PATH are computed at import, during
    collection, before _isolate_user_root pins WHISPER_USER_DIR, so in a dev
    checkout they name the developer's live config.user.json and config.json
    at the repo root. A test that saves the user layer (a Settings write, the
    retired-key sweep, a model install) would rewrite that file. A test that
    patches the same names itself still wins."""
    import server.infrastructure.config as config

    user = tmp_path_factory.mktemp("user-config")
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(user / "config.user.json"))
    monkeypatch.setattr(config, "CONFIG_PATH", str(user / "config.json"))
    config._invalidate_cache()
    yield
    config._invalidate_cache()


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
    labels = sys.modules.get("server.chat.engine.activity_labels")
    if labels is not None:
        labels._reset_caption_client_cache()


@pytest.fixture(autouse=True)
def _no_live_tool_checks(monkeypatch):
    """A finished local-chat download starts a tool-call check
    (server/models_manager/tool_check.py), which loads the model in llama-server.
    In a unit test that would start a real model server on a fake file, so the
    launch is refused: the check never starts and its live state is dropped, as
    for any launch failure. Tests of the check call run_check themselves. Each
    test also starts with no check marked running."""
    from server.models_manager import tool_check

    def refuse(key):
        raise RuntimeError("no live tool-call checks in unit tests")

    monkeypatch.setattr(tool_check, "_launch", refuse)
    monkeypatch.setattr(tool_check, "_live", {})


@pytest.fixture(autouse=True)
def _no_live_gguf_header_reads(monkeypatch):
    """The memory-fit grade reads a model's GGUF header on Hugging Face
    (server/model_browser/hf_client.py::gguf_file_metadata) when the weights
    are not on disk. In a unit test that read answers as a failed one (None),
    so the grade keeps its flat allowance and no request leaves the test;
    tests of the KV estimate stub the header themselves. Each test also starts
    with an empty shape cache, so one test's header cannot answer the next."""
    from server.model_browser import hf_client, service

    monkeypatch.setattr(hf_client, "gguf_file_metadata", lambda repo_id, filename: None)
    monkeypatch.setattr(service, "_kv_shapes", {})


# Architectures a recent llama.cpp and mlx-lm load (a subset of llama.cpp
# b11851 and mlx-lm 0.32.2), standing in for the installed engines.
ENGINE_LLAMA_ARCHS = frozenset(
    {
        "llama", "qwen2", "qwen3", "qwen3moe", "qwen35", "qwen35moe", "qwen3next",
        "qwen4exp", "gemma", "gemma2", "gemma3", "gemma3n", "gemma4", "mistral3",
        "phi2", "phi3", "phimoe", "deepseek2", "deepseek4", "command-r", "cohere2",
        "granite", "granitemoe", "stablelm", "starcoder2", "mamba", "clip", "bert",
        "t5encoder",
    }
)  # fmt: skip
ENGINE_MLX_ARCHS = frozenset(
    {
        "llama", "mistral", "mistral3", "qwen2", "qwen3", "qwen3_moe", "qwen3_next",
        "qwen3_5", "qwen3_5_moe", "gemma", "gemma2", "gemma3", "gemma3_text", "gemma4",
        "gemma4_text", "phi3", "deepseek_v3", "deepseek_v41", "gpt_oss", "glm4",
    }
)  # fmt: skip


@pytest.fixture(autouse=True)
def _pinned_engine_archs_and_no_live_official_lane(monkeypatch):
    """Discover's architecture gate reads the installed engines
    (server/model_browser/engine_archs.py), and its official lane asks Hugging
    Face for the model makers' repos and their builds. In a unit test the
    engines answer with the fixed lists above, so no test runs the engine probe
    or depends on what this machine has installed, and the official lane finds
    nothing, so no request leaves the test. Tests of those pieces patch in
    their own answers."""
    from server.model_browser import engine_archs, hf_client

    monkeypatch.setattr(engine_archs, "llama_archs", lambda: ENGINE_LLAMA_ARCHS)
    monkeypatch.setattr(engine_archs, "mlx_archs", lambda: ENGINE_MLX_ARCHS)
    monkeypatch.setattr(hf_client, "official_models", lambda query, limit: [])
    monkeypatch.setattr(hf_client, "conversions", lambda base_model, fmt, limit: [])


@pytest.fixture(autouse=True)
def _no_live_activity_labels(monkeypatch):
    """A chat turn with a tool round asks the auxiliary model for an activity
    caption (server/chat/engine/activity_labels.py). In a unit test that call
    would reach whatever Bedrock client the test faked for the turn itself,
    or a real one. Refused here, so the caption falls back to None (the
    deterministic label) and no frame is added; tests of the labeller patch
    in their own fake. Imported, not looked up in sys.modules, so the guard
    also holds for a test that imports the engine inside its body. Each test
    also starts with no call in a thread, so one test's abandoned caption
    cannot skip the next test's."""
    from server.chat.engine import activity_labels

    def refuse(*a, **k):
        raise RuntimeError("no live activity label calls in unit tests")

    monkeypatch.setattr(activity_labels, "aux_one_shot", refuse)
    monkeypatch.setattr(activity_labels, "_in_thread", {})


@pytest.fixture(autouse=True)
def _isolate_aws_dir(tmp_path_factory, monkeypatch):
    """Never create folders in the developer's real ~/.aws: aws_cli and
    run_python create ~/.aws/cli/cache before they run
    (server/executors/code.py::_aws_cache_paths). Every test gets an ~/.aws
    that does not exist, so nothing is created; tests of the cache folders
    point _AWS_DIR at their own."""
    from server.executors import code

    monkeypatch.setattr(code, "_AWS_DIR", str(tmp_path_factory.mktemp("aws-home") / ".aws"))
