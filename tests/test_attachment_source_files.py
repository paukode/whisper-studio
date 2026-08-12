"""Uploaded documents keep their original bytes, so analysis reads the whole file.

Extraction is lossy on purpose: server/extract/sheet.py turns a large sheet
into a header plus a 20-row sample, and that sample is readable but not
computable. Asked for a total, a model summing 20 of 10,000 rows returns a
confident wrong number. These tests pin the three things that prevent that:

  - the bytes survive the upload and the round trip through sqlite,
  - the path reaches the model together with an instruction not to compute
    from the prompt text,
  - files outlive their row for no longer than the retention sweep allows.
"""

import os
import time

import pytest

from server import attachment_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point both the DB and the source-file directory at a temp dir."""
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(attachment_store, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(attachment_store, "DB_PATH", str(tmp_path / "storage" / "sessions.db"))
    attachment_store._ensure_table()
    return attachment_store


def _save(store, aid, content=b"a,b\n1,2\n", filename="data.csv", text="a,b"):
    path = store.save_source_file(aid, filename, content)
    store.save_attachment(
        aid, {"kind": "document", "filename": filename, "text": text, "source_path": path}
    )
    return path


# ------------------------------------------------------------------ storage


def test_bytes_survive_the_round_trip(store):
    path = _save(store, "a1", content=b"col\n42\n")
    assert os.path.exists(path)
    assert open(path, "rb").read() == b"col\n42\n"

    record = store.get_attachment("a1")
    assert record["source_path"] == path


def test_source_lives_under_the_data_root_not_the_repo(store, tmp_path):
    path = _save(store, "a2")
    assert str(tmp_path / "data" / "attachments") in path
    assert path.endswith(".csv"), "extension is kept so pandas can sniff the format"


def test_a_missing_file_is_not_reported_as_available(store):
    path = _save(store, "a3")
    os.unlink(path)
    # The row still names it; the reader must not hand the model a dead path.
    assert "source_path" not in store.get_attachment("a3")


def test_an_unwritable_directory_degrades_instead_of_failing(store, monkeypatch):
    monkeypatch.setattr(attachment_store, "source_files_dir", lambda: "/proc/nope")
    assert store.save_source_file("a4", "x.csv", b"data") == ""


# ---------------------------------------------------------------- retention


def test_the_sweep_removes_files_whose_row_is_gone(store):
    kept = _save(store, "keep")
    orphan = _save(store, "orphan")
    with store._get_conn() as conn:
        conn.execute("DELETE FROM attachments WHERE id = 'orphan'")
    # Backdate past the grace window that protects in-flight uploads.
    old = time.time() - store.UNBOUND_TTL_SECONDS - 60
    os.utime(orphan, (old, old))

    assert store.sweep_source_files() == 1
    assert os.path.exists(kept)
    assert not os.path.exists(orphan)


def test_the_sweep_spares_a_fresh_upload_whose_row_is_not_written_yet(store):
    path = store.save_source_file("inflight", "x.csv", b"data")
    assert store.sweep_source_files() == 0
    assert os.path.exists(path)


# ------------------------------------------------------------------- prompt


def test_the_model_is_told_to_compute_from_the_file_not_the_sample(store, monkeypatch):
    from server.chat import attachment_context

    monkeypatch.setattr(attachment_context, "_hot_cache", {})
    path = _save(store, "sheet", filename="sales.xlsx", text="[Large spreadsheet: ...]")

    texts, _ = attachment_context.render_attachment_blocks(["sheet"])
    body = texts[0]
    assert "[File: sales.xlsx]" in body, "filename marker drives re-injection"
    assert path in body
    assert "run_python" in body
    assert "never from the text above" in body


def test_documents_with_no_kept_bytes_render_unchanged(store, monkeypatch):
    from server.chat import attachment_context

    monkeypatch.setattr(attachment_context, "_hot_cache", {})
    store.save_attachment("plain", {"kind": "document", "filename": "n.txt", "text": "hi"})

    (body,), _ = attachment_context.render_attachment_blocks(["plain"])
    assert body == "[File: n.txt]\nhi"


# --------------------------------------------------------------------- caps


def test_the_chart_cap_is_one_megabyte():
    from server.visuals import MAX_SPEC_CHARS, validate_chart_spec

    assert MAX_SPEC_CHARS == 1_000_000
    spec = {"mark": "bar", "data": {"values": [{"a": "x" * MAX_SPEC_CHARS}]}}
    parsed, error = validate_chart_spec(spec)
    assert parsed is None
    assert "Aggregate the data" in error


def test_run_python_has_room_to_read_a_real_spreadsheet():
    from server.executors.code import RUN_PYTHON_TIMEOUT_S

    # pandas opens a 3MB / 10k-row xlsx in ~2s; attachments run to 50MB.
    assert RUN_PYTHON_TIMEOUT_S >= 60
