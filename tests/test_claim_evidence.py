"""Whether a claimed delivery happened (server/goals/evidence.py).

A file must be on disk, not empty, and written since the turn began where
the reply says it was made; an act must match a call of the turn that did it
and succeeded, naming the claim's target. The model's word is never
evidence.
"""

import json
import os
import time

import pytest

from server.goals import claims as c
from server.goals.claims import Claim
from server.goals.evidence import Evidence, check, not_done, succeeded, turn_calls


def _turn(*calls: tuple[str, dict, str], prompt: str = "go") -> list:
    """A turn whose assistant made each (name, input, result) call."""
    rows: list = [{"role": "user", "content": prompt}]
    for i, (name, given, result) in enumerate(calls):
        rows.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": given}],
            }
        )
        rows.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": result}],
            }
        )
    return rows


def _aged(path, seconds: float = 3600) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


# ── files, folders and removals ──────────────────────────────────────────


def test_a_file_made_this_turn_holds_and_one_made_before_does_not(tmp_path):
    started = time.time() - 5
    new = tmp_path / "new.md"
    new.write_text("x")
    ev = Evidence.of(_turn(), started_at=started)
    assert check(Claim(c.FILE, str(new)), ev).ok

    old = tmp_path / "old.md"
    old.write_text("x")
    ev = Evidence.of(_turn(), started_at=time.time() + 60)
    verdict = check(Claim(c.FILE, str(old)), ev)
    assert not verdict.ok and "was not written in this turn" in verdict.note
    # Only where it is, or pointing back at an earlier turn: being there is enough.
    assert check(Claim(c.FILE, str(old), made=False), ev).ok
    assert check(Claim(c.FILE, str(old), earlier=True), ev).ok


def test_a_copy_that_kept_its_old_times_holds_when_a_call_named_it(tmp_path):
    target = tmp_path / "copy.md"
    target.write_text("x")
    later = time.time() + 60
    named = Evidence.of(
        _turn(("terminal_run", {"command": f"cp -p a.md {target}"}, "exit_code: 0\n")),
        started_at=later,
    )
    assert check(Claim(c.FILE, str(target)), named).ok
    failed = Evidence.of(
        _turn(("terminal_run", {"command": f"cp -p a.md {target}"}, "exit_code: 1\nno such file")),
        started_at=later,
    )
    assert not check(Claim(c.FILE, str(target)), failed).ok


def test_a_missing_or_empty_file_does_not_hold(tmp_path):
    ev = Evidence.of(_turn(), started_at=time.time() - 5)
    missing = check(Claim(c.FILE, str(tmp_path / "gone.docx")), ev)
    assert not missing.ok and "does not exist" in missing.note
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert "is empty" in check(Claim(c.FILE, str(empty)), ev).note
    assert not_done(Claim(c.FILE, str(empty)), check(Claim(c.FILE, str(empty)), ev)).startswith(
        "Not saved: "
    )


def test_a_file_receipt_links_to_the_file(tmp_path):
    report = tmp_path / "Q3 report.html"
    report.write_text("<p>hi</p>")
    verdict = check(Claim(c.FILE, str(report)), Evidence.of(_turn(), started_at=time.time() - 5))
    receipt = verdict.receipt
    assert receipt["label"] == "Q3 report.html" and receipt["target"] == str(report)
    assert receipt["href"].startswith("#wsfile=") and receipt["href"].endswith("&open=os")
    assert "%20" in receipt["href"] and "saved" in receipt["detail"]


def test_a_folder_written_to_needs_something_new_in_it(tmp_path):
    folder = tmp_path / "charts"
    folder.mkdir()
    (folder / "old.png").write_bytes(b"x")
    _aged(folder / "old.png")
    started = time.time() - 5
    ev = Evidence.of(_turn(), started_at=started)
    assert not check(Claim(c.FOLDER, str(folder)), ev).ok
    assert check(Claim(c.FOLDER, str(folder), made=False), ev).ok
    (folder / "q3.png").write_bytes(b"x")
    verdict = check(Claim(c.FOLDER, str(folder)), ev)
    assert verdict.ok and "q3.png" in verdict.receipt["detail"]


def test_a_removed_file_must_be_gone_and_removed_by_a_call(tmp_path):
    ev = Evidence.of(_turn(), started_at=time.time() - 5)
    kept = tmp_path / "kept.md"
    kept.write_text("x")
    assert "still exists" in check(Claim(c.REMOVED, str(kept)), ev).note
    # A file that was never there was not removed by anything.
    never = check(Claim(c.REMOVED, str(tmp_path / "never.md")), ev)
    assert not never.ok and "no call in this turn removed" in never.note
    gone = tmp_path / "gone.md"
    removed = Evidence.of(_turn(("ws_run_command", {"command": f"rm {gone}"}, "Done.")))
    assert check(Claim(c.REMOVED, str(gone)), removed).ok
    # Removing its folder removes it too.
    folder = Evidence.of(_turn(("ws_run_command", {"command": f"rm -rf {tmp_path}/build"}, "")))
    assert check(Claim(c.REMOVED, str(tmp_path / "build" / "a.o")), folder).ok


def test_a_relative_path_reads_against_the_workspace(tmp_path):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "app.py").write_text("x")
    ev = Evidence.of(_turn(), workspace=str(tmp_path), started_at=time.time() - 5)
    assert check(Claim(c.FILE, "server/app.py"), ev).ok
    # With no workspace there is nothing to read a bare relative path against.
    unread = check(Claim(c.FILE, "server/app.py"), Evidence.of(_turn()))
    assert unread.ok and not unread.checked


# ── acts ─────────────────────────────────────────────────────────────────


def test_a_push_holds_only_with_a_push_that_succeeded():
    ok = _turn(("git_push", {"branch": "main"}, "Pushed main to origin"))
    assert check(Claim(c.PUSH, "main"), Evidence.of(ok)).ok
    assert check(Claim(c.PUSH, "origin/main"), Evidence.of(ok)).ok
    failed = _turn(("git_push", {"branch": "main"}, "Error: rejected (non-fast-forward)"))
    verdict = check(Claim(c.PUSH, "main"), Evidence.of(failed))
    assert not verdict.ok and verdict.note == "no push to `main` succeeded in this turn"
    other = _turn(("git_push", {"branch": "dev"}, "Pushed dev to origin"))
    assert not check(Claim(c.PUSH, "main"), Evidence.of(other)).ok
    assert not check(Claim(c.PUSH), Evidence.of(_turn())).ok


def test_a_shell_command_counts_by_its_exit_status():
    ran = _turn(("ws_run_command", {"command": "git push origin fix-x"}, "To github.com:a/b\n"))
    assert check(Claim(c.PUSH, "fix-x"), Evidence.of(ran)).ok
    failed = _turn(
        ("ws_run_command", {"command": "git push origin fix-x"}, "denied by remote\n(exit code 1)")
    )
    assert not check(Claim(c.PUSH, "fix-x"), Evidence.of(failed)).ok


def test_a_call_still_waiting_for_approval_did_nothing():
    waiting = _turn(("aws_cli", {"command": "aws s3 cp a.csv s3://b/a.csv"}, "[WS_APPROVAL]{}"))
    assert not check(Claim(c.UPLOAD, "s3://b/a.csv"), Evidence.of(waiting)).ok
    done = _turn(("aws_cli", {"command": "aws s3 cp a.csv s3://b/a.csv"}, "upload: ./a.csv"))
    assert check(Claim(c.UPLOAD, "s3://b/a.csv"), Evidence.of(done)).ok


def test_a_message_holds_with_a_send_that_named_the_recipient():
    sent = _turn(("mcp__gmail__send_email", {"to": "dana@acme.com", "body": "hi"}, '{"id": "m1"}'))
    assert check(Claim(c.MESSAGE, "Dana"), Evidence.of(sent)).ok
    assert check(Claim(c.MESSAGE, "dana@acme.com"), Evidence.of(sent)).ok
    assert not check(Claim(c.MESSAGE, "Bob"), Evidence.of(sent)).ok
    read = _turn(("mcp__gmail__search", {"q": "dana"}, "[]"))
    assert not check(Claim(c.MESSAGE, "Dana"), Evidence.of(read)).ok


def test_a_pull_request_holds_with_the_call_that_opened_it():
    opened = _turn(
        (
            "github_api_write",
            {"endpoint": "/repos/a/b/pulls", "method": "POST"},
            json.dumps({"number": 12, "html_url": "https://github.com/a/b/pull/12"}),
        )
    )
    verdict = check(Claim(c.PR, "#12"), Evidence.of(opened))
    assert verdict.ok and verdict.receipt["href"] == "https://github.com/a/b/pull/12"
    assert not check(Claim(c.PR, "#13"), Evidence.of(opened)).ok


def test_a_link_holds_where_a_call_made_it_or_an_earlier_reply_verified_it():
    made = _turn(("run_python", {"code": "..."}, "(exit code 0)\nhttps://gist.github.com/a/1"))
    assert check(Claim(c.LINK, "https://gist.github.com/a/1"), Evidence.of(made)).ok
    # Named only by the user: no call made it.
    before = [
        {"role": "user", "content": "the gist is https://gist.github.com/a/1"},
        {"role": "assistant", "content": "Noted."},
        *_turn(),
    ]
    assert not check(Claim(c.LINK, "https://gist.github.com/a/1"), Evidence.of(before)).ok
    receipts = [{"kind": "link", "target": "https://gist.github.com/a/1"}]
    recap = Claim(c.LINK, "https://gist.github.com/a/1", earlier=True)
    assert check(recap, Evidence.of(before, receipts=receipts)).ok
    assert not check(Claim(c.LINK, "https://gist.github.com/a/2"), Evidence.of(made)).ok


def test_an_earlier_act_holds_only_where_an_earlier_reply_verified_it():
    earlier = Claim(c.PUSH, "main", earlier=True)
    assert not check(earlier, Evidence.of(_turn())).ok
    receipts = [{"kind": "push", "target": "main", "label": "Pushed", "detail": "to main"}]
    verdict = check(earlier, Evidence.of(_turn(), receipts=receipts))
    assert verdict.ok and not verdict.checked and verdict.receipt is None
    other = Claim(c.PUSH, "fix-y", earlier=True)
    assert not check(other, Evidence.of(_turn(), receipts=receipts)).ok


def test_an_artifact_card_holds_with_a_card_this_turn_or_the_sessions():
    none = Evidence.of(_turn())
    assert not check(Claim(c.ARTIFACT), none).ok
    made = Evidence.of(_turn(("create_artifact", {"title": "x"}, "created")))
    assert check(Claim(c.ARTIFACT), made).ok
    assert check(Claim(c.ARTIFACT), Evidence.of(_turn(), session_has_artifact=True)).ok


# ── reading results ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "result", "ok"),
    [
        ("terminal_run", "exit_code: 0\ndone", True),
        ("terminal_run", "exit_code: 128\nfatal: not a git repository", False),
        ("run_python", "(exit code 1)\nTraceback (most recent call last):", False),
        ("ws_run_command", "Everything up-to-date", True),
        ("ws_run_command", "! [rejected]        main -> main (fetch first)", False),
        ("save_file", "[Tool Error] disk full", False),
        ("git_push", "Error: authentication failed", False),
        ("ws_merge_worktree", "Failed to merge worktree: conflict", False),
        ("github_api_write", '{"error": "Not Found"}', False),
        ("aws_cli", "[WS_APPROVAL]{}", False),
        # What an approval card sends back (src/hooks/chatStream/sseStream.ts).
        ("save_file", "[User approved] save_to_path: a.md. The action succeeded.", True),
        (
            "save_file",
            "[User approved but the operation FAILED] save_to_path: a.md. Error: x",
            False,
        ),
        (
            "save_file",
            "[User approved but the operation was NOT executed] save_to_path: a.md.",
            False,
        ),
        (
            "aws_cli",
            "[User denied] run_aws: s3 cp. The user rejected this action; it did not run.",
            False,
        ),
        ("save_file", "Saved to /Users/me/a.md", True),
        ("ws_run_command", "[Background Task Started] task_id=1\nCommand: vercel deploy", False),
    ],
)
def test_a_result_says_whether_the_call_succeeded(name, result, ok):
    assert succeeded(name, result) is ok


def test_a_call_with_no_result_did_nothing():
    rows = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "git_push", "input": {}}],
        },
    ]
    (call,) = turn_calls(rows)
    assert not call.ok


def test_a_failed_workspace_command_says_its_exit_code(tmp_path):
    # Without it a failing command looked like a success to the claim check.
    from server.workspace.executors import run_workspace_command

    failed = run_workspace_command('sh -c "echo nope; exit 3"', str(tmp_path))
    assert failed.text.rstrip().endswith("(exit code 3)")
    assert not succeeded("ws_run_command", failed.text)
    done = run_workspace_command("echo fine", str(tmp_path))
    assert "exit code" not in done.text and succeeded("ws_run_command", done.text)
