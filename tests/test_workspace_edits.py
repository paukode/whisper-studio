"""The stored edits behind the diff dialog (server/workspace/edits.py) and
the endpoint that serves them (GET /api/workspace/edits/{tool_use_id}).

A record is one JSON file per tool_use id under <data root>/edits, which
tests/conftest.py points at a per-test folder through WHISPER_USER_DIR. A
text over the cap, or a dotenv or credential file, is not kept and the
record says so. Records older than thirty days and leftover temporary files
go the first time an edit is recorded in a process and once a day after
that, and the oldest records go when all of them outgrow the size cap. The
diff counts are exact within their bounds and an estimate, marked
approximate, beyond them, so a large repetitive file never stalls a write.
"""

import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.paths import data_root
from server.workspace import edits
from server.workspace import router as ws_router


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    # Each test is its own process as far as the prune's bookkeeping goes.
    monkeypatch.setattr(edits, "_last_prune", None)
    monkeypatch.setattr(edits, "_total_bytes", 0)


def _lockfile(n: int) -> str:
    """A package-lock-like JSON text of about ``n`` lines, most of them
    repeated many times: the shape that made the exact diff cubic."""
    out = ["{\n", '  "packages": {\n']
    i = 0
    while len(out) < n - 2:
        out += [
            f'    "node_modules/pkg-{i}": {{\n',
            '      "version": "1.0.0",\n',
            '      "dev": true,\n',
            '      "license": "MIT"\n',
            "    },\n",
        ]
        i += 1
    return "".join(out + ["  }\n", "}\n"])


def _write_aged(name: str, age_seconds: float, body: str = "{}") -> str:
    os.makedirs(edits.edits_dir(), exist_ok=True)
    full = os.path.join(edits.edits_dir(), name)
    with open(full, "w") as f:
        f.write(body)
    then = time.time() - age_seconds
    os.utime(full, (then, then))
    return full


def _client():
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


def test_records_live_under_the_data_root():
    assert edits.edits_dir() == os.path.join(data_root(), "edits")


def test_diff_stats_count_lines_exactly():
    stats = edits.diff_stats("a\nb\nc\n", "a\nB\nc\nd\n")
    assert stats == {
        "added": 2,
        "removed": 1,
        "lines": 4,
        "bytes": 8,
        "stored": False,
        "approximate": False,
    }
    assert edits.diff_stats("", "x\n")["added"] == 1
    assert edits.diff_stats("x\n", "")["removed"] == 1
    assert edits.diff_stats("same\n", "same\n") == {
        "added": 0,
        "removed": 0,
        "lines": 1,
        "bytes": 5,
        "stored": False,
        "approximate": False,
    }


def test_lines_split_on_newlines_only_the_way_git_counts_them():
    # A form feed, U+2028 and U+0085 are inside a line, not line ends.
    text = "a\x0cb\n\u2028c\x85d\n"
    assert edits.split_lines(text) == ["a\x0cb\n", "\u2028c\x85d\n"]
    assert edits.diff_stats("", text)["lines"] == 2
    stats = edits.diff_stats("one\x0ctwo\n", "one\x0cTWO\n")
    assert (stats["added"], stats["removed"]) == (1, 1)
    # A last line with no newline is a line, and adding the newline changes it.
    assert edits.split_lines("x\ny") == ["x\n", "y"]
    stats = edits.diff_stats("x\ny", "x\ny\n")
    assert (stats["added"], stats["removed"], stats["lines"]) == (1, 1, 2)


def test_an_unreadable_original_leaves_the_change_unknown():
    stats = edits.diff_stats(None, "a\nb\n")
    assert stats["added"] is None and stats["removed"] is None
    assert (stats["lines"], stats["bytes"]) == (2, 4)


def test_a_one_line_edit_in_a_large_file_is_exact_and_quick():
    old = "".join(f"line {i}\n" for i in range(20_000))
    new = old.replace("line 10000\n", "changed\n")
    started = time.monotonic()
    stats = edits.diff_stats(old, new)
    assert time.monotonic() - started < 1.0
    assert (stats["added"], stats["removed"], stats["approximate"]) == (1, 1, False)


def test_a_replace_all_on_a_large_repetitive_file_returns_quickly():
    # Unbounded, the exact diff took 29 s on 4,000 lines and grows with the
    # cube of the size. Bounded, the counts are an estimate, marked so.
    for n in (1_000, 4_000, 15_000):
        old = _lockfile(n)
        new = old.replace('"dev": true', '"dev": false')
        changed = old.count('"dev": true')
        started = time.monotonic()
        stats = edits.diff_stats(old, new)
        assert time.monotonic() - started < 2.0, n
        assert (stats["added"], stats["removed"]) == (changed, changed)
        assert stats["approximate"] is True


def test_the_deadline_stops_the_exact_diff_and_falls_back(monkeypatch):
    monkeypatch.setattr(edits, "EXACT_DEADLINE_SECONDS", -1.0)
    stats = edits.diff_stats("a\nb\nc\n", "a\nB\nc\n")
    assert stats["approximate"] is True
    assert (stats["added"], stats["removed"]) == (1, 1)


def test_record_then_read_round_trips_and_marks_the_stats_stored():
    stats = {"added": 1, "removed": 1, "lines": 1, "bytes": 4, "stored": False}
    ok = edits.record_edit(
        "toolu_abc", "src/a.py", "old\n", "new\n", stats, full_path="/repo/src/a.py"
    )
    assert ok is True
    assert stats["stored"] is True
    record = edits.read_edit("toolu_abc")
    assert record["path"] == "src/a.py"
    assert record["full_path"] == "/repo/src/a.py"
    assert record["original"] == "old\n"
    assert record["modified"] == "new\n"
    assert record["added"] == 1 and record["removed"] == 1
    assert record["stored"] is True
    assert record["approximate"] is False
    assert record["at"].endswith("+00:00")


def test_a_text_over_the_cap_is_not_kept_and_the_record_says_so(monkeypatch):
    monkeypatch.setattr(edits, "EDIT_TEXT_CAP", 10)
    stats = {"added": 1, "removed": 0, "lines": 1, "bytes": 30, "stored": False}
    assert edits.record_edit("toolu_big", "big.txt", "", "x" * 30, stats) is True
    assert stats["stored"] is False
    record = edits.read_edit("toolu_big")
    assert record["original"] is None and record["modified"] is None
    assert record["stored"] is False
    assert record["added"] == 1


def test_the_cap_counts_bytes_not_characters(monkeypatch):
    monkeypatch.setattr(edits, "EDIT_TEXT_CAP", 10)
    stats = {"stored": False}
    # Six characters, twelve UTF-8 bytes: over a ten-byte cap.
    assert edits.record_edit("toolu_wide", "w.txt", "", "\u00e9" * 6, stats) is True
    assert stats["stored"] is False
    assert edits.read_edit("toolu_wide")["modified"] is None
    edits.record_edit("toolu_narrow", "n.txt", "", "\u00e9" * 5, stats)
    assert stats["stored"] is True


def test_an_id_that_is_not_a_plain_token_is_refused():
    stats = {"stored": False}
    assert edits.record_edit("../escape", "a", "", "x", stats) is False
    assert stats["stored"] is False
    assert edits.read_edit("../escape") is None
    assert edits.read_edit("") is None
    assert not os.path.exists(os.path.join(data_root(), "escape.json"))


def test_old_records_are_pruned_at_the_first_record_then_once_a_day():
    month = (edits.PRUNE_AFTER_DAYS + 1) * 86400
    stale = _write_aged("toolu_old.json", month)
    fresh = _write_aged("toolu_fresh.json", 0)

    edits.record_edit("toolu_new", "a", "", "x\n", {"stored": False})
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)

    # A second record the same day does not scan again.
    stale = _write_aged("toolu_old.json", month)
    edits.record_edit("toolu_next", "a", "", "y\n", {"stored": False})
    assert os.path.exists(stale)

    # A day later, a long-running process prunes again.
    edits._last_prune = time.time() - edits.PRUNE_EVERY_SECONDS - 1
    edits.record_edit("toolu_later", "a", "", "z\n", {"stored": False})
    assert not os.path.exists(stale)


def test_a_leftover_temporary_file_is_pruned_and_a_fresh_one_is_left():
    leftover = _write_aged("toolu_dead.json.tmp", edits.STALE_TMP_SECONDS + 60)
    in_flight = _write_aged("toolu_live.json.tmp", 0)
    edits.record_edit("toolu_t", "a", "", "x\n", {"stored": False})
    assert not os.path.exists(leftover)
    assert os.path.exists(in_flight)


def test_the_oldest_records_go_when_all_of_them_outgrow_the_cap(monkeypatch):
    body = json.dumps({"path": "p", "original": "x" * 1000})
    oldest = _write_aged("toolu_a.json", 300, body)
    middle = _write_aged("toolu_b.json", 200, body)
    newest = _write_aged("toolu_c.json", 100, body)
    each = os.path.getsize(oldest)
    monkeypatch.setattr(edits, "EDITS_TOTAL_CAP", 3 * each)
    # The first record prunes: one old record more than the cap holds.
    edits.record_edit("toolu_d", "a", "", "x" * 700, {"stored": False})
    added = os.path.getsize(os.path.join(edits.edits_dir(), "toolu_d.json"))
    assert added < each < 2 * added  # the sizes this test relies on
    assert not os.path.exists(oldest)
    assert os.path.exists(middle) and os.path.exists(newest)
    assert edits.read_edit("toolu_d") is not None
    # The prune ran today, but outgrowing the cap prunes again at once.
    edits.record_edit("toolu_e", "a", "", "y" * 700, {"stored": False})
    assert not os.path.exists(middle)
    assert os.path.exists(newest)
    assert edits.read_edit("toolu_d") is not None
    assert edits.read_edit("toolu_e") is not None


def test_a_dotenv_or_credential_file_keeps_its_counts_but_not_its_texts():
    for path in (".env", "app/.env.local", "certs/server.pem", "gcp/credentials.json", ".npmrc"):
        assert edits.holds_secrets(path), path
        stats = {"added": 1, "removed": 1, "stored": True}
        assert edits.record_edit("toolu_secret", path, "A=1\n", "A=2\n", stats) is True
        assert stats["stored"] is False
        record = edits.read_edit("toolu_secret")
        assert record["original"] is None and record["modified"] is None
        assert (record["added"], record["removed"]) == (1, 1)
    for path in ("src/env.py", "docs/environment.md", "server/keys.py", "src/environment.ts"):
        assert not edits.holds_secrets(path), path


@pytest.mark.parametrize(
    "path",
    [
        "deploy/id_rsa",
        "keys/id_ed25519",
        ".envrc",
        "prod.env",
        "config/secrets.env",
        "secrets.yaml",
        "infra/terraform.tfvars",
        "infra/prod.tfstate",
        ".ENV",
        "App/.Env.Local",
        "certs/SERVER.PEM",
    ],
)
def test_key_and_secret_files_the_validator_does_not_name_keep_no_texts(path):
    # A pasted deploy key, a direnv file, *.env, Terraform variables and
    # state, and any case of a dotenv or key name (APFS ignores case).
    assert edits.holds_secrets(path)


@pytest.mark.parametrize("path", ["keys/id_rsa.pub", "src/environment.ts", "docs/secrets-guide.md"])
def test_a_public_key_or_a_file_about_secrets_keeps_its_texts(path):
    assert not edits.holds_secrets(path)


def test_a_created_file_is_marked_so_in_its_record():
    edits.record_edit("toolu_new", "pkg/__init__.py", "", "x = 1\n", {}, created=True)
    edits.record_edit("toolu_empty", "pkg/__init__.py", "", "x = 1\n", {})
    assert edits.read_edit("toolu_new")["created"] is True
    # An existing empty file has the same empty original, and was edited.
    assert edits.read_edit("toolu_empty")["created"] is False


def test_deleting_a_session_removes_the_records_of_its_calls():
    import uuid

    from server.infrastructure.sessions import _delete_session_sync, _ensure_db, _get_conn

    _ensure_db()
    for tool_use_id in ("toolu_mine", "toolu_trace", "toolu_other"):
        edits.record_edit(tool_use_id, "a.py", "1\n", "2\n", {})
    history = [
        {"role": "user", "content": "edit a.py"},
        {
            "role": "assistant",
            "content": "",
            # A call without a provider id is keyed by its tool name.
            "toolUse": [
                {"toolId": "toolu_mine", "toolName": "ws_write_file"},
                {"toolId": "ws_read_file", "toolName": "ws_read_file"},
            ],
            "traces": [{"id": "toolu_trace", "name": "ws_edit_file"}],
        },
    ]
    sid = str(uuid.uuid4())
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, chat_history)"
            " VALUES (?, ?, ?, ?, ?)",
            (sid, "t", "2026-10-18", "2026-10-18", json.dumps(history)),
        )
    assert edits.call_ids(history) == {"toolu_mine", "toolu_trace"}
    _delete_session_sync(sid)
    assert edits.read_edit("toolu_mine") is None
    assert edits.read_edit("toolu_trace") is None
    # Another session's record stays.
    assert edits.read_edit("toolu_other") is not None


def test_the_endpoint_serves_the_record_and_404s_without_one():
    edits.record_edit("toolu_http", "a.txt", "1\n", "2\n", {"added": 1, "removed": 1})
    client = _client()
    res = client.get("/api/workspace/edits/toolu_http")
    assert res.status_code == 200
    assert res.json()["original"] == "1\n"
    assert res.json()["modified"] == "2\n"
    assert client.get("/api/workspace/edits/toolu_none").status_code == 404
    assert client.get("/api/workspace/edits/..%2Fescape").status_code == 404
