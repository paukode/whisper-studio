"""Claims that were read or checked wrongly, pinned.

Two reviews of the claim check found replies that were shown as verified
though false, true replies that were dropped and contradicted, and claims
never read at all. Each case here is one of them, in the words it was found
with.
"""

import asyncio
import json
import os
import time

import pytest

from server.chat.claim_guard import ClaimGuard
from server.chat.engine.events import RoundResult, TextDelta, ToolCallStart
from server.goals import claims as c
from server.goals.claims import Claim, read_claims
from server.goals.deliverables import check_claims
from server.goals.evidence import Evidence, check, succeeded


def _turn(*calls, prompt="go", before=()):
    rows = [*before, {"role": "user", "content": prompt}]
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


def _holds(text: str, *calls, **kw) -> list[bool]:
    ev = Evidence.of(_turn(*calls, **kw), started_at=time.time() - 5)
    return [check(cl, ev).ok for cl in read_claims(text)]


def _kinds(text: str) -> list[tuple[str, str, bool]]:
    return [(cl.kind, cl.target, cl.firm) for cl in read_claims(text)]


# ── false claims that were shown as verified ─────────────────────────────


def test_an_address_the_prompt_gave_is_no_evidence_of_the_act():
    url = "https://github.com/acme/app/pull/42"
    assert _holds(f"I merged the PR: {url} and deleted the branch.", prompt=f"merge {url}") == [
        False
    ]
    listed = ("github", {"args": ["pr", "list"]}, f"#12 {url[:-2]}12 open")
    assert _holds(f"I opened a pull request: {url[:-2]}12", listed) == [False]
    # "/pull/1" is not "/pull/12".
    made = ("git_push_pr", {"branch": "x"}, f"Pushed to origin/x and created PR:\n\n{url[:-2]}12")
    assert _holds(f"Opened PR {url[:-2]}1", made) == [False]


@pytest.mark.parametrize(
    "result",
    [
        "[MCP Error] invalid_grant",
        "[Hook denied] pushes to main need review",
        "[Loop guard] Blocked git_push: the same call failed 3 times",
        "[Plan Mode] Tool 'git_push' is blocked while plan mode is active.",
        '{"ok": false, "error": "channel_not_found"}',
        '{"data": null, "errors": [{"message": "Not Found"}]}',
        "Unknown memory tool: memory_write",
        "File not found: a.md",
        "nothing to commit, working tree clean",
        "[Stopped by user]",
    ],
)
def test_the_apps_own_failure_markers_are_failures(result):
    assert not succeeded("mcp__gmail__send_email", result)


@pytest.mark.parametrize(
    ("text", "call"),
    [
        (
            "Sent the email to dana@acme.com.",
            ("mcp__gmail__create_draft", {"to": "dana@acme.com"}, "{}"),
        ),
        (
            "All changes are committed and pushed to `main`.",
            (
                "ws_write_file",
                {"path": "ci.yml", "content": "git commit -am x && git push origin main"},
                "ok",
            ),
        ),
        ("Deployed the app to production.", ("ws_edit_file", {"path": "scripts/deploy.sh"}, "ok")),
        ("Deployed the app to production.", ("ws_run_command", {"command": "ls deploy/"}, "a")),
        (
            "Deployed the app to production.",
            (
                "ws_run_command",
                {"command": "vercel deploy --prod"},
                "[Background Task Started] task_id=1",
            ),
        ),
        ("Opened PR #12.", ("ws_run_command", {"command": "gh api repos/acme/app/pulls"}, "[]")),
        ("Filed issue #77.", ("github", {"args": ["issue", "comment", "77", "-b", "x"]}, "ok")),
        ("I scheduled a daily task to check the build.", ("cron_run", {"name": "x"}, "ran")),
        ("Sent the summary to the team.", ("notify_user", {"message": "x"}, "ok")),
        (
            "Pushed the fix to `main`.",
            ("ws_run_command", {"command": "git push --dry-run origin main"}, ""),
        ),
        ("Filed an issue for it.", ("mcp__jira__getJiraIssue", {"key": "X-1"}, "{}")),
    ],
)
def test_a_call_that_did_not_do_the_act_is_no_evidence(text, call):
    held = _holds(text, call)
    assert held and not any(held)


def test_a_failed_edit_is_not_excused_by_a_read_of_the_file(tmp_path):
    app = tmp_path / "server" / "app.py"
    app.parent.mkdir()
    app.write_text("x")
    then = time.time() - 3600
    os.utime(app, (then, then))
    ev = Evidence.of(
        _turn(
            ("ws_read_file", {"path": "server/app.py"}, "x"),
            ("ws_edit_file", {"path": "server/app.py"}, "[Tool Error] old_string not found"),
        ),
        workspace=str(tmp_path),
        started_at=time.time() - 5,
    )
    (claim,) = read_claims("I updated `server/app.py` to retry on 503.")
    verdict = check(claim, ev)
    assert not verdict.ok and "was not written in this turn" in verdict.note


def test_a_pull_request_that_failed_to_open_is_not_one():
    call = (
        "git_push_pr",
        {"branch": "fix-x"},
        "Pushed to origin/fix-x successfully.\n\nBut PR creation failed: not authenticated",
    )
    assert _holds("Pushed `fix-x` and opened a pull request.", call) == [True, False]


def test_a_hidden_file_is_read_as_its_path_not_the_home_folder():
    assert [(k, t) for k, t, _f in _kinds("I updated ~/.ssh/config to add the host.")] == [
        (c.FOLDER, "~/.ssh/config")
    ]


def test_a_later_word_that_only_contains_the_target_settles_nothing():
    history = [
        {"role": "user", "content": "push it"},
        {"role": "assistant", "content": "Pushed the fix to `main`."},
        {"role": "assistant", "content": "The remaining docs look fine."},
    ]
    assert check_claims(history, None) is not None


def test_a_relative_app_link_with_no_workspace_is_not_read_against_the_server(tmp_path):
    (claim,) = read_claims("[Open](#wsfile=README.md&open=os)")
    assert not check(claim, Evidence.of(_turn())).ok


def test_plan_mode_checks_an_outright_claim_of_an_edit(tmp_path):
    ev = Evidence.of(_turn(), workspace=str(tmp_path), plan_mode=True, started_at=time.time())
    (claim,) = read_claims("I updated `server/app.py` to retry on 503.")
    assert not check(claim, ev).ok


# ── true replies that were dropped ───────────────────────────────────────


def test_a_recap_rests_on_what_an_earlier_reply_verified():
    before = [
        {"role": "user", "content": "push the fix and tell Dana"},
        {"role": "assistant", "content": "Pushed `fix-x` to origin. Emailed Dana the notes."},
    ]
    # The deliveries that reply's chips verified, as the chat sends them.
    receipts = [{"kind": "push", "target": "fix-x"}, {"kind": "message", "target": "Dana"}]
    reply = "Yes, I pushed `fix-x` and emailed Dana."
    turn = _turn(prompt="did you push it and tell Dana?", before=before)
    ev = Evidence.of(turn, receipts=receipts)
    assert [check(cl, ev).ok for cl in read_claims(reply)] == [True, True]
    # The earlier reply's words alone are no record of what happened.
    assert not any(check(cl, Evidence.of(turn)).ok for cl in read_claims(reply))
    # Tried again this turn and failed: then the claim is checked.
    failed = ("git_push", {"branch": "fix-x"}, "Error: rejected")
    ev = Evidence.of(_turn(failed, prompt="push again", before=before), receipts=receipts)
    assert not check(read_claims("I pushed `fix-x`.")[0], ev).ok


def test_the_updated_report_of_an_earlier_turn_needs_only_to_exist(tmp_path):
    report = tmp_path / "report.docx"
    report.write_text("x")
    then = time.time() - 3600
    os.utime(report, (then, then))
    before = [
        {"role": "user", "content": "update the report"},
        {"role": "assistant", "content": f"I saved the report to {report}."},
    ]
    ev = Evidence.of(_turn(prompt="where is it?", before=before), started_at=time.time())
    assert [check(cl, ev).ok for cl in read_claims(f"Here's the updated report: {report}")] == [
        True
    ]


def test_the_github_tool_is_evidence_of_what_it_ran():
    merged = (
        "github",
        {"args": ["pr", "merge", "12", "--squash"]},
        "Verified: pr #12 is now MERGED.",
    )
    assert _holds("Merged PR #12 into main.", merged) == [True]
    opened = ("github", {"args": ["pr", "create", "--fill"]}, "https://github.com/a/b/pull/12")
    assert _holds("Opened PR #12.", opened) == [True]


@pytest.mark.parametrize(
    "text",
    [
        "After the tests pass, the image is pushed to ECR.",
        "On failure, an alert is sent to #ops.",
        "Every night the export is uploaded to s3://acme/exports/.",
        "According to the log, the branch was pushed at 10:02.",
        "According to the log, the fix was pushed at 10:02.",
        "In PR #140, `server/app.py` was refactored to split the loader.",
        "| File | Change |\n| --- | --- |\n| `server/app.py` | added retry on 503 |",
    ],
)
def test_a_description_of_how_something_works_is_not_held(text):
    # Read as a claim at most loosely, and nothing was tried: let through.
    assert all(ok for ok in _holds(text))


@pytest.mark.parametrize(
    "text",
    [
        "I updated the settings dialog in `src/components/SettingsDialog.tsx`.",
        "I kept the parsed index in memory to avoid a second read.",
        "I merged the two helpers into `main.py`.",
        "I pushed the validation down into the model layer.",
        "I published the event on the bus when the job ends.",
        "I committed the transaction before closing the cursor.",
        "I created the retry helper following https://docs.python.org/3/library/asyncio.html.",
        "The race is in the artifact store's save path.",
    ],
)
def test_coding_words_are_not_deliveries(text):
    kinds = {k for k, _t, _f in _kinds(text)}
    assert not kinds & {
        c.SETTING, c.MEMORY, c.MERGE, c.PUSH, c.PUBLISH, c.COMMIT, c.LINK, c.ARTIFACT
    }  # fmt: skip


def test_each_act_names_its_own_target():
    assert _kinds("Pushed the retry fix for `fetch_user` to `main`.")[0][:2] == (c.PUSH, "main")
    assert (
        _kinds("Committed 1500000 rows of fixtures.") == []
        or _kinds("Committed 1500000 rows of fixtures.")[0][1] == ""
    )
    assert _kinds("Emailed Dana a summary of PR #45.")[0][:2] == (c.MESSAGE, "Dana")


@pytest.mark.parametrize(
    ("text", "call"),
    [
        (
            "Published v2.11.2 to PyPI.",
            (
                "ws_run_command",
                {"command": "twine upload dist/*"},
                "Uploading whisper-2.11.2.tar.gz",
            ),
        ),
        (
            "Posted the update in #general.",
            ("mcp__slack__slack_post_message", {"channel": "general"}, '{"ok": true}'),
        ),
        (
            "Emailed the report to Dana Smith.",
            ("mcp__gmail__send_email", {"to": "dsmith@acme.com"}, '{"id": "1"}'),
        ),
        (
            "Uploaded the export to s3://acme/exports/q3.csv.",
            (
                "run_python",
                {"code": "s3.upload_file('q3.csv', 'acme', 'exports/q3.csv')"},
                "(exit code 0)",
            ),
        ),
        (
            "Uploaded the export to s3://acme/exports/q3.csv.",
            (
                "aws_cli",
                {"command": "aws s3 cp q3.csv s3://acme/exports/ --quiet"},
                "upload: ./q3.csv",
            ),
        ),
    ],
)
def test_a_true_act_matches_its_target_as_the_tool_names_it(text, call):
    assert _holds(text, call) == [True]


def test_removals_and_renames_are_read_as_what_they_did(tmp_path):
    assert [(k, t) for k, t, _f in _kinds("Removed unused imports across `server/app.py`.")] == [
        (c.FILE, "server/app.py")
    ]
    assert [(k, t) for k, t, _f in _kinds("I renamed `src/old.ts` to `src/new.ts`.")] == [
        (c.REMOVED, "src/old.ts"),
        (c.FILE, "src/new.ts"),
    ]


# ── claims that were never read ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Changes pushed to `main`.", c.PUSH),
        ("Email sent to dana@acme.com.", c.MESSAGE),
        ("PR opened: https://github.com/a/b/pull/12", c.PR),
        ("**Pushed** `fix-x` to origin.", c.PUSH),
        ("After the tests passed I pushed the fix to `main`.", c.PUSH),
        ("Per your request I emailed Dana the notes.", c.MESSAGE),
        ("Once CI was green I merged PR #12.", c.MERGE),
        ("Pushed the fix for the Not Found case to `main`.", c.PUSH),
    ],
)
def test_common_ways_of_saying_it_are_read(text, kind):
    (claim,) = read_claims(text)
    assert claim.kind == kind and claim.firm


def test_already_is_this_turns_work_and_when_is_no_claim():
    (claim,) = read_claims("I've already pushed the fix to `main`.")
    assert not claim.earlier
    assert read_claims("When I pushed, it failed.") == []
    assert read_claims("When you run it, the report is saved to ~/Downloads/q3.docx.") == []


def test_earlier_marks_the_whole_sentence():
    (claim,) = read_claims("As I mentioned earlier, I pushed the fix to `main`.")
    assert claim.earlier


# ── the stream ───────────────────────────────────────────────────────────


async def _aiter(items):
    for item in items:
        yield item


def _wrap(guard, events, messages=None):
    async def go():
        return [
            ev
            async for ev in guard.wrap(
                _aiter(events), messages or [{"role": "user", "content": "q"}]
            )
        ]

    return asyncio.run(go())


def test_text_before_a_tool_call_goes_out_before_the_call():
    guard = ClaimGuard(started_at=time.time())
    out = _wrap(
        guard,
        [
            TextDelta(text="The config looks fine. Let me check the logs."),
            ToolCallStart(name="ws_read_file"),
            RoundResult(
                stop_reason="tool_use",
                content=[{"type": "tool_use", "id": "t1", "name": "ws_read_file"}],
            ),
        ],
    )
    at_call = next(i for i, ev in enumerate(out) if isinstance(ev, ToolCallStart))
    before = "".join(ev.text for ev in out[:at_call] if isinstance(ev, TextDelta))
    assert before == "The config looks fine. Let me check the logs."


def test_a_run_with_no_gate_closes_its_last_round_with_the_note(tmp_path):
    guard = ClaimGuard(started_at=time.time(), notes_at_round_end=True)
    missing = tmp_path / "out.csv"
    out = _wrap(
        guard,
        [
            TextDelta(text=f"Exported the table to {missing}."),
            RoundResult(stop_reason="end_turn", content=[{"type": "text", "text": "x"}]),
        ],
    )
    at_end = next(i for i, ev in enumerate(out) if isinstance(ev, RoundResult))
    text = "".join(ev.text for ev in out[:at_end] if isinstance(ev, TextDelta))
    assert "Not saved" in text and guard.finish() == []


def test_a_salvage_or_verify_row_does_not_start_a_new_turn():
    push = ("git_push", {"branch": "main"}, "Pushed to origin/main successfully.")
    rows = _turn(push, prompt="push it")
    salvage = {
        "role": "user",
        "content": "[The conversation no longer fits the model's context window.]",
    }
    verify = {"role": "user", "content": "[verify] The task is not yet complete."}
    for row in (salvage, verify):
        ev = Evidence.of([*rows, row])
        assert check(read_claims("I pushed the fix to `main`.")[0], ev).ok


def test_a_call_compaction_took_away_still_counts():
    push = ("git_push", {"branch": "main"}, "Pushed to origin/main successfully.")
    kept = Evidence.of(_turn(push)).calls
    ev = Evidence.of(_turn(prompt="summary of the work so far"), extra_calls=kept)
    assert check(read_claims("I pushed the fix to `main`.")[0], ev).ok


def test_a_claim_after_a_code_block_with_a_blank_line_is_held():
    guard = ClaimGuard(started_at=time.time())
    text = "Run:\n```bash\necho one\n\necho two\n```\nCommitted the fix to the repo.\n"
    out = _wrap(guard, [TextDelta(text=text), RoundResult(stop_reason="end_turn", content=[])])
    shown = "".join(ev.text for ev in out if isinstance(ev, TextDelta))
    assert "Committed the fix" not in shown and "echo two" in shown


def test_a_claim_word_holds_its_sentence_from_the_start():
    guard = ClaimGuard(started_at=time.time())
    sentence = (
        "I made two new commits that cover the "
        + "edge cases in the loader " * 6
        + "and the parser."
    )
    deltas = [TextDelta(text=sentence[i : i + 9]) for i in range(0, len(sentence), 9)]
    out = _wrap(guard, [*deltas, RoundResult(stop_reason="end_turn", content=[])])
    shown = "".join(ev.text for ev in out if isinstance(ev, TextDelta))
    assert "I made two" not in shown


def test_a_cut_is_no_sentence_end_and_the_continuation_completes_it(tmp_path):
    missing = tmp_path / "q3.docx"
    guard = ClaimGuard(started_at=time.time())
    out = _wrap(
        guard,
        [
            TextDelta(text="Half of the answer. I saved the report to"),
            RoundResult(stop_reason="max_tokens", content=[]),
        ],
    )
    at_end = next(i for i, ev in enumerate(out) if isinstance(ev, RoundResult))
    first = "".join(ev.text for ev in out[:at_end] if isinstance(ev, TextDelta))
    assert first == "Half of the answer."
    out = _wrap(
        guard,
        [TextDelta(text=f" `{missing}`. It has three sections."), RoundResult("end_turn", [])],
    )
    second = "".join(ev.text for ev in out if isinstance(ev, TextDelta))
    assert "saved the report" not in second and "It has three sections." in second
    notes = "".join(json.loads(f[6:]).get("text", "") for f in guard.finish())
    assert f"Not saved: `{missing}` does not exist." in notes


def test_a_true_claim_keeps_its_chip_when_a_sibling_fails(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("x")
    guard = ClaimGuard(started_at=time.time() - 5)
    out = _wrap(
        guard,
        [
            TextDelta(text=f"I pushed to `main` and saved the notes to `{notes}`."),
            RoundResult(stop_reason="end_turn", content=[]),
        ],
    )
    chips = [r for ev in out if hasattr(ev, "payload") for r in ev.payload["deliveries"]["items"]]
    assert [r["target"] for r in chips] == [str(notes)]


def test_a_sentence_after_etc_survives_its_neighbour_being_dropped(tmp_path):
    guard = ClaimGuard(started_at=time.time())
    missing = tmp_path / "q3.docx"
    out = _wrap(
        guard,
        [
            TextDelta(text=f"I saved the docs to {missing}, etc. The tests pass now."),
            RoundResult(stop_reason="end_turn", content=[]),
        ],
    )
    shown = "".join(ev.text for ev in out if isinstance(ev, TextDelta))
    assert "The tests pass now." in shown and "saved the docs" not in shown


def test_a_paused_turn_hands_its_held_sentence_to_the_continuation(tmp_path):
    report = tmp_path / "report.md"
    ctx_messages = [{"role": "user", "content": "save the report"}]

    class Ctx:
        turn_scope_id = None
        session_id = "s-carry"
        messages = ctx_messages
        ws_path = ""
        plan_mode = False
        cost_source = "chat"
        policy = None

    guard = ClaimGuard.for_turn(Ctx())
    _wrap(
        guard,
        [
            TextDelta(text=f"Saved the report to {report}."),
            RoundResult(
                stop_reason="tool_use",
                content=[{"type": "tool_use", "id": "t1", "name": "save_file"}],
            ),
        ],
        ctx_messages,
    )
    assert guard.finish(paused=True) == []
    report.write_text("done")  # the approved save ran
    resumed = ClaimGuard.for_turn(Ctx())
    out = _wrap(resumed, [RoundResult(stop_reason="end_turn", content=[])], ctx_messages)
    assert f"Saved the report to {report}." in "".join(
        ev.text for ev in out if isinstance(ev, TextDelta)
    )


def test_a_long_paragraph_with_no_break_streams_in_linear_time():
    guard = ClaimGuard(started_at=time.time())
    words = ("\N{CJK UNIFIED IDEOGRAPH-4E00}" * 7) * 3000
    deltas = [TextDelta(text=words[i : i + 7]) for i in range(0, len(words), 7)]
    started = time.monotonic()
    _wrap(guard, [*deltas, RoundResult(stop_reason="end_turn", content=[])])
    assert time.monotonic() - started < 2.0


def test_a_read_that_shows_an_address_made_no_link():
    fetched = (
        "web_fetch",
        {"url": "https://gist.github.com/a/1"},
        "https://gist.github.com/a/1 ...",
    )
    assert _holds("I created a gist at https://gist.github.com/a/1.", fetched) == [False]


def test_a_tool_with_its_own_success_words_needs_them():
    assert not succeeded("memory_write", "Done.")
    assert succeeded("memory_write", "Memory file saved: a.md [scope: global]")


def test_the_feedback_never_asks_to_repeat_an_action():
    history = [
        {"role": "user", "content": "email Dana"},
        {"role": "assistant", "content": "I emailed the notes to dana@acme.com."},
    ]
    feedback = check_claims(history, None)
    assert feedback and "Never repeat an action that may already have happened" in feedback


def test_a_passive_about_what_will_happen_is_no_claim():
    assert read_claims("Look at the review comments when they are posted and respond.") == []
    assert read_claims("The job runs after that lock is released.") == []


def test_a_later_word_that_only_contains_the_target_keeps_the_note():
    guard = ClaimGuard(started_at=time.time())
    _wrap(
        guard,
        [
            TextDelta(text="Pushed the fix to `main`. The remaining docs look fine."),
            RoundResult(stop_reason="end_turn", content=[]),
        ],
    )
    frames = guard.finish()
    assert any("Not pushed" in f for f in frames)
