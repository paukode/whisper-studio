"""save_file: the one-shot 'save this file somewhere' tool.

Single-call protocol — it NEVER asks the user where to save:

  - destination_path present (the model resolved it from the user's own
    words, '~' allowed) -> that exact path.
  - Otherwise -> ~/Documents/<filename> (or ~/Downloads via
    suggested_location).

Either way the content is decoded and STAGED into the app sandbox
immediately, and the returned [WS_APPROVAL] (action=save_to_path) payload
carries staged_path — approving just moves the ready file into place. A
destination under the sensitive-path denylist is rejected here, before any
approval card is even shown.
"""

import base64
import json
import os

from server.workspace.executors import _exec_save_file
from server.workspace.paths import is_staged_path


def _parse_approval(output: str) -> dict:
    assert output.startswith("[WS_APPROVAL]"), output
    return json.loads(output[len("[WS_APPROVAL]") :])


def test_defaults_to_documents_and_stages_content():
    out = _exec_save_file({"filename": "report.pdf", "content": "hello"}, [], [])
    payload = _parse_approval(out)
    assert payload["action"] == "save_to_path"
    assert payload["path"] == os.path.expanduser("~/Documents/report.pdf")
    assert is_staged_path(payload["staged_path"])
    # The staged copy holds the content; the destination itself is only
    # written by the approval move, never by the tool executor.
    with open(payload["staged_path"], "rb") as f:
        assert f.read() == b"hello"


def test_suggested_location_downloads_changes_the_default():
    out = _exec_save_file(
        {"filename": "report.pdf", "content": "hi", "suggested_location": "Downloads"}, [], []
    )
    payload = _parse_approval(out)
    assert payload["path"] == os.path.expanduser("~/Downloads/report.pdf")


def test_destination_path_from_user_words_wins(tmp_path):
    dest = str(tmp_path / "report.pdf")
    out = _exec_save_file(
        {"filename": "report.pdf", "content": "hello world", "destination_path": dest},
        [],
        [],
    )
    payload = _parse_approval(out)
    assert payload["action"] == "save_to_path"
    assert payload["path"] == os.path.realpath(dest)
    assert payload["filename"] == "report.pdf"
    with open(payload["staged_path"], "rb") as f:
        assert f.read() == b"hello world"


def test_destination_path_tilde_is_expanded():
    out = _exec_save_file(
        {"filename": "x.txt", "content": "hi", "destination_path": "~/Downloads/x.txt"},
        [],
        [],
    )
    payload = _parse_approval(out)
    assert payload["path"] == os.path.realpath(os.path.expanduser("~/Downloads/x.txt"))


def test_never_touches_workspace_state(monkeypatch):
    # get_workspace_path must not even be consulted for save_file — this is
    # the whole point of the one-shot flow (never connects a workspace).
    called = {"hit": False}

    def _boom():
        called["hit"] = True
        raise AssertionError("save_file must not call get_workspace_path")

    monkeypatch.setattr("server.workspace.executors.get_workspace_path", _boom)
    _exec_save_file({"filename": "x.txt", "content": "hi"}, [], [])
    assert called["hit"] is False


def test_missing_filename_errors():
    out = _exec_save_file({"filename": "", "content": "hi"}, [], [])
    assert out.startswith("Error")


def test_base64_content_is_decoded_before_staging(tmp_path):
    dest = str(tmp_path / "image.png")
    raw = b"\x89PNG\r\n\x1a\nfakebinarydata"
    encoded = base64.b64encode(raw).decode("ascii")
    out = _exec_save_file(
        {
            "filename": "image.png",
            "content": encoded,
            "content_encoding": "base64",
            "destination_path": dest,
        },
        [],
        [],
    )
    payload = _parse_approval(out)
    with open(payload["staged_path"], "rb") as f:
        assert f.read() == raw


def test_rejects_invalid_base64(tmp_path):
    dest = str(tmp_path / "bad.bin")
    out = _exec_save_file(
        {
            "filename": "bad.bin",
            "content": "not-valid-base64!!!",
            "content_encoding": "base64",
            "destination_path": dest,
        },
        [],
        [],
    )
    assert out.startswith("Error")


def test_rejects_relative_destination():
    out = _exec_save_file(
        {"filename": "x.txt", "content": "hi", "destination_path": "relative/path.txt"},
        [],
        [],
    )
    assert out.startswith("Error")
    assert "absolute" in out


def test_rejects_sensitive_destination(tmp_path, monkeypatch):
    # Point the denylist at a throwaway tmp_path directory instead of a real
    # user path (e.g. ~/.ssh) — this must never risk touching a real
    # developer's actual credentials regardless of whether the check works.
    fake_denied = tmp_path / "denied"
    fake_denied.mkdir()
    monkeypatch.setattr(
        "server.security.sensitive_paths.expanded_sandbox_paths",
        lambda: [str(fake_denied)],
    )
    dest = str(fake_denied / "secret.txt")
    out = _exec_save_file(
        {"filename": "secret.txt", "content": "should not be written", "destination_path": dest},
        [],
        [],
    )
    assert out.startswith("Error")
    assert "sensitive" in out.lower()
    assert not (fake_denied / "secret.txt").exists()


def test_rejects_unknown_encoding():
    out = _exec_save_file(
        {
            "filename": "x.txt",
            "content": "hi",
            "content_encoding": "utf-16",
            "destination_path": "/tmp/wherever/x.txt",
        },
        [],
        [],
    )
    assert out.startswith("Error")
