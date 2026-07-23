"""Four medium-severity memory fixes.

(1) Deleting a session removes its on-disk session-memory summary file
    (session_memory.drop_session), tolerating a missing file.
(2) The secret scanner catches modern OpenAI keys (sk-proj-/sk-svcacct-/
    sk-admin-) and base64url PyPI tokens, without false-positiving on prose.
(3) Project-memory slugs are disambiguated by a realpath hash, so two
    workspaces sharing a basename no longer collide; a legacy basename-only
    dir is migrated to the new slug only when unambiguous.
(4) memory_write / the REST save endpoint force a .md suffix and refuse
    dotfiles, so a memory can never be written into a name the scanner skips.

All keys below are synthetic (repeated filler around the fixed format markers),
never real secrets.
"""

import os

import pytest

import server.infrastructure.feature_flags as FF
import server.memory.executor as EX
import server.memory.memdir as MD
import server.memory.secret_scanner as SS
import server.memory.session_memory as SM

# ── (1) session-memory file cleanup on delete ────────────────────────────────


@pytest.fixture
def session_mem_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(SM, "SESSION_MEMORY_DIR", str(tmp_path / "session_memory"))
    return tmp_path / "session_memory"


def test_drop_session_removes_summary_file(session_mem_dir):
    path = SM.get_session_memory_path("sess-x")
    with open(path, "w", encoding="utf-8") as f:
        f.write("## Goals\nship it\n")
    assert os.path.isfile(path)
    SM._session_state["sess-x"] = {"chars_at_last_update": 1, "last_update_index": 1}

    SM.drop_session("sess-x")

    assert not os.path.exists(path)
    assert "sess-x" not in SM._session_state


def test_drop_session_tolerates_missing_file(session_mem_dir):
    # No file on disk and no in-memory state: must be a silent no-op.
    SM.drop_session("never-existed")  # must not raise
    assert not os.path.exists(SM.get_session_memory_path("never-existed"))


# ── (2) secret scanner: modern OpenAI + PyPI ─────────────────────────────────

_OPENAI_PROJ = "sk-proj-" + ("A1b2C3d4" * 6) + "T3BlbkFJ" + ("X9y8Z7w6" * 3)
_OPENAI_SVC = "sk-svcacct-" + ("a" * 44) + "T3BlbkFJ" + ("b" * 22)
_OPENAI_ADMIN = "sk-admin-" + ("Q" * 40) + "T3BlbkFJ" + ("z" * 20)
_OPENAI_LEGACY = "sk-" + ("a1B2c3D4e5" * 2) + "T3BlbkFJ" + ("f6G7h8I9j0" * 2)
_PYPI_TOKEN = "pypi-AgEIcHlwaS5vcmc" + ("A9b8C7d6-_" * 6)


@pytest.mark.parametrize(
    "key",
    [_OPENAI_PROJ, _OPENAI_SVC, _OPENAI_ADMIN, _OPENAI_LEGACY],
    ids=["proj", "svcacct", "admin", "legacy"],
)
def test_openai_keys_detected(key):
    findings = SS.scan_for_secrets(f"my key is {key} keep it secret")
    labels = {f["label"] for f in findings}
    assert "OpenAI API Key" in labels, findings


def test_pypi_token_detected():
    findings = SS.scan_for_secrets(f"upload token {_PYPI_TOKEN}")
    rules = {f["rule"] for f in findings}
    assert "pypi-token-modern" in rules, findings


def test_new_patterns_compile():
    # Every rule (including the two new ones) must have compiled.
    rules = SS._get_rules()
    ids = {rule_id for rule_id, _label, _pat in rules}
    assert "openai-api-key-modern" in ids
    assert "pypi-token-modern" in ids


@pytest.mark.parametrize(
    "prose",
    [
        "The admin scheduled a project sync; the svcacct rotation is next week.",
        "Install pypi-tools then run the build; T3 tier config unchanged.",
        "sk-proj is our internal shorthand for scikit project templates.",
        "No secrets here, just a normal sentence about API keys in general.",
    ],
)
def test_no_false_positive_on_prose(prose):
    findings = SS.scan_for_secrets(prose)
    labels = {f["label"] for f in findings}
    assert "OpenAI API Key" not in labels
    assert "PyPI Token" not in labels


def test_check_and_block_redacts_modern_openai_key():
    clean, redacted, findings = SS.check_and_block(f"token: {_OPENAI_PROJ}")
    assert clean is False
    assert _OPENAI_PROJ not in redacted
    assert findings


# ── (3) slug disambiguation + legacy migration ───────────────────────────────


@pytest.fixture
def memory_base(tmp_path, monkeypatch):
    base = tmp_path / "memory"
    monkeypatch.setattr(MD, "MEMORY_BASE", str(base))
    return base


def _make_ws(tmp_path, sub):
    p = tmp_path / sub / "app"
    p.mkdir(parents=True)
    return p


def test_same_basename_different_paths_get_distinct_slugs(tmp_path, memory_base):
    ws_a = _make_ws(tmp_path, "a")
    ws_b = _make_ws(tmp_path, "b")

    slug_a = MD.get_workspace_slug(str(ws_a))
    slug_b = MD.get_workspace_slug(str(ws_b))

    assert slug_a != slug_b
    assert slug_a.startswith("app-") and slug_b.startswith("app-")

    dir_a = MD.get_memory_dir(str(ws_a))
    dir_b = MD.get_memory_dir(str(ws_b))
    assert dir_a != dir_b
    assert os.path.isdir(dir_a) and os.path.isdir(dir_b)


def test_legacy_basename_dir_migrated_when_unambiguous(tmp_path, memory_base):
    ws = _make_ws(tmp_path, "a")
    # Pre-two-tier layout: a basename-only "app" dir with a topic file.
    legacy_dir = memory_base / "app"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "note.md").write_text("---\nname: N\ndescription: d\ntype: user\n---\n\nbody\n")

    new_dir = MD.get_memory_dir(str(ws))

    assert os.path.basename(new_dir) == MD.get_workspace_slug(str(ws))
    assert not legacy_dir.exists()  # renamed, not copied
    assert (memory_base / os.path.basename(new_dir) / "note.md").read_text().endswith("body\n")


def test_legacy_migration_skipped_when_target_exists(tmp_path, memory_base):
    ws = _make_ws(tmp_path, "a")
    slug = MD.get_workspace_slug(str(ws))

    legacy_dir = memory_base / "app"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "legacy.md").write_text("legacy")

    # New slug dir already populated: migration must not clobber it.
    new_dir = memory_base / slug
    new_dir.mkdir(parents=True)
    (new_dir / "current.md").write_text("current")

    returned = MD.get_memory_dir(str(ws))

    assert returned == str(new_dir)
    assert legacy_dir.exists()  # left untouched — ambiguous, so no rename
    assert (new_dir / "current.md").read_text() == "current"
    assert not (new_dir / "legacy.md").exists()


# ── (4) filename normalization / dotfile rejection ───────────────────────────


@pytest.fixture
def write_env(tmp_path, monkeypatch):
    """No-workspace global-tier writes with auto_memory forced on."""
    monkeypatch.setattr(MD, "MEMORY_BASE", str(tmp_path / "memory"))
    monkeypatch.setattr(MD, "GLOBAL_MEMORY_DIR", str(tmp_path / "global_memory"))
    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "auto_memory")
    monkeypatch.setattr(EX, "get_workspace_path", lambda: None)
    return tmp_path / "global_memory"


def test_missing_md_extension_appended(write_env):
    result = EX.execute_memory_write(
        {"filename": "notes", "name": "Notes", "type": "user", "content": "c"}
    )
    assert "notes.md" in result
    assert (write_env / "notes.md").is_file()
    assert not (write_env / "notes").exists()


def test_dotfile_filename_refused(write_env):
    result = EX.execute_memory_write(
        {"filename": ".secret", "name": "S", "type": "user", "content": "c"}
    )
    assert result.startswith("Error:")
    assert "." in result
    # Rejected before any write, so nothing landed under either candidate name.
    assert not (write_env / ".secret").exists()
    assert not (write_env / ".secret.md").exists()


def test_dot_md_filename_refused(write_env):
    result = EX.execute_memory_write(
        {"filename": ".hidden.md", "name": "H", "type": "user", "content": "c"}
    )
    assert result.startswith("Error:")


def test_normalize_helper_direct():
    assert MD.normalize_memory_filename("notes") == ("notes.md", "")
    assert MD.normalize_memory_filename("sub/notes") == ("sub/notes.md", "")
    assert MD.normalize_memory_filename("already.md") == ("already.md", "")
    # Cased extension is left as-is (the entrypoint guard handles MEMORY.MD).
    assert MD.normalize_memory_filename("keep.MD") == ("keep.MD", "")
    fixed, err = MD.normalize_memory_filename(".env")
    assert fixed == "" and err
