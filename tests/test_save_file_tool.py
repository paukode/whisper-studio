"""save_file: the one-shot 'save this file somewhere' tool.

Two-call protocol, mirroring ws_create_file's [WS_APPROVAL] pattern but with
its own [WS_WORKSPACE_PROMPT]-sentinel pause on the first call instead of an
immediate approval:

  1. First call (no destination_path) -> pauses via the SAME
     [WS_WORKSPACE_PROMPT] sentinel _workspace_prompt_payload uses, but with
     reason='save_location' (server/workspace/state.py's
     _save_location_prompt_payload). No workspace is ever consulted.
  2. Re-issued call (destination_path set) -> packages a [WS_APPROVAL]
     (action=save_to_path) for the approval pipeline to actually write.
     A destination under the sensitive-path denylist is rejected here,
     before any approval card is even shown.
"""

import base64
import json

from server.workspace.executors import _exec_save_file


def _parse_prompt(output: str) -> dict:
    assert output.startswith("[WS_WORKSPACE_PROMPT]"), output
    return json.loads(output[len("[WS_WORKSPACE_PROMPT]") :])


def _parse_approval(output: str) -> dict:
    assert output.startswith("[WS_APPROVAL]"), output
    return json.loads(output[len("[WS_APPROVAL]") :])


def test_first_call_without_destination_pauses_with_save_location_reason():
    out = _exec_save_file({"filename": "report.pdf", "content": "hello"}, [], [])
    payload = _parse_prompt(out)
    assert payload["reason"] == "save_location"
    assert payload["tool_name"] == "save_file"
    assert payload["suggested_name"] == "report.pdf"
    assert "documents_dir" in payload
    assert "downloads_dir" in payload


def test_first_call_never_touches_workspace_state(monkeypatch):
    # get_workspace_path must not even be consulted for save_file — this is
    # the whole point of the one-shot flow (never connects a workspace).
    called = {"hit": False}

    def _boom():
        called["hit"] = True
        raise AssertionError("save_file must not call get_workspace_path")

    monkeypatch.setattr("server.workspace.executors.get_workspace_path", _boom)
    _exec_save_file({"filename": "x.txt", "content": "hi"}, [], [])
    assert called["hit"] is False


def test_missing_filename_errors_without_pausing():
    out = _exec_save_file({"filename": "", "content": "hi"}, [], [])
    assert out.startswith("Error")


def test_resume_call_with_destination_builds_ws_approval(tmp_path):
    dest = str(tmp_path / "report.pdf")
    out = _exec_save_file(
        {
            "filename": "report.pdf",
            "content": "hello world",
            "destination_path": dest,
        },
        [],
        [],
    )
    payload = _parse_approval(out)
    assert payload["action"] == "save_to_path"
    assert payload["path"] == dest
    assert payload["content"] == "hello world"
    assert payload["content_encoding"] == "utf-8"
    assert payload["filename"] == "report.pdf"


def test_resume_call_with_base64_content(tmp_path):
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
    assert payload["content_encoding"] == "base64"
    assert base64.b64decode(payload["content"]) == raw


def test_resume_call_rejects_invalid_base64(tmp_path):
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


def test_resume_call_rejects_relative_destination():
    out = _exec_save_file(
        {
            "filename": "x.txt",
            "content": "hi",
            "destination_path": "relative/path.txt",
        },
        [],
        [],
    )
    assert out.startswith("Error")
    assert "absolute" in out


def test_resume_call_rejects_sensitive_destination(tmp_path, monkeypatch):
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
        {
            "filename": "secret.txt",
            "content": "should not be written",
            "destination_path": dest,
        },
        [],
        [],
    )
    assert out.startswith("Error")
    assert "sensitive" in out.lower()
    assert not (fake_denied / "secret.txt").exists()


def test_resume_call_rejects_unknown_encoding():
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
