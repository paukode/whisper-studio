"""How the completion gate reads a turn's replies for claims.

Once the checks read the reply being gated (tests/test_gate_final_reply.py),
the claim check had to read prose the way a person does: a question, a
negated sentence or an offer naming a path is not a claim that the file
exists, plan mode writes nothing, and a wrong reading costs one round, never
a loop to the cap. A reply the gate skipped (a mid-turn message was pending)
or the first half of a max_tokens split is still a reply of the turn, and a
background-task note in front of the prompt does not hide the prompt.
"""

import asyncio
from types import SimpleNamespace

import pytest

from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.goals import gate
from server.goals.deliverables import (
    CLAIM_MARKER,
    OUTSIDE_WRITERS,
    asserted_paths,
    check_claims,
    claims_an_artifact,
    last_user_prompt,
    turn_messages,
)
from server.goals.requested_files import produced_a_file


def _say(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


_BACKGROUND_NOTE = (
    "<system-reminder>Background task updates since your last turn:\n"
    '- task t1 "audit" finished: completed. Summary: found 3 issues\n'
    "Factor these into your response.</system-reminder>"
)


# ── what counts as a claim ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Saved to ~/Downloads/report.docx.",
        "You can find it at ~/Downloads/report.docx.",
        "Saved to ~/Downloads/report.docx so you can open it in Word.",
        "Saved to ~/Downloads/report.docx, no errors.",
        "Done: [report](#wsfile=~/Downloads/report.docx&open=os) is ready.",
    ],
)
def test_a_statement_that_the_file_was_saved_is_a_claim(text):
    assert asserted_paths(text) == ["~/Downloads/report.docx"]


@pytest.mark.parametrize(
    ("text", "claimed"),
    [
        ("Saved to ~/a.docx and ~/b.xlsx, but could not export ~/c.pdf.", ["~/a.docx", "~/b.xlsx"]),
        ("Saved to ~/a.docx, but the logo wasn't included.", ["~/a.docx"]),
        ("Generated at ~/a.pdf, though one chart failed to render.", ["~/a.pdf"]),
        ("Created ~/a.pdf (12 pages, not including the appendix).", ["~/a.pdf"]),
        ("The first attempt failed, so I re-ran it and saved the report to ~/a.pdf.", ["~/a.pdf"]),
        ("Your report is ready at ~/a.pdf, want me to email it?", ["~/a.pdf"]),
        ("Saved to ~/a.pdf - want me to also export a Word copy?", ["~/a.pdf"]),
        ("You'll find the report at ~/a.pdf.", ["~/a.pdf"]),
        ("No worries, I saved it to ~/a.pdf.", ["~/a.pdf"]),
        ("I can confirm the report was saved to ~/a.pdf.", ["~/a.pdf"]),
        ("Let me know what you think of the draft at ~/a.md.", ["~/a.md"]),
        ("- **Report:** `~/a.docx`", ["~/a.docx"]),
        ("Saved: `~/a.md`, `~/b.md` and `~/c.md`.", ["~/a.md", "~/b.md", "~/c.md"]),
        ("I've created the following files:\n- ~/a.docx\n- ~/b.xlsx", ["~/a.docx", "~/b.xlsx"]),
    ],
)
def test_a_claim_is_kept_however_the_reply_goes_on(text, claimed):
    assert asserted_paths(text) == claimed


@pytest.mark.parametrize(
    "text",
    [
        "I could not save these files:\n- ~/a.docx\n- ~/b.xlsx",
        "I can create these files:\n- ~/a.docx\nShall I go ahead?",
        "Plan: 1. gather data 2. write ~/a.docx 3. review.",
        "Save cancelled for ~/a.docx.",
        "Deleted ~/a.docx.",
        "Run it like this:\n```\npython make.py --out ~/a.pdf\n```",
        "For example, the output would go to /tmp/out.json.",
        "I will export the files, e.g. ~/a.csv",
        "The report will be saved to ~/a.pdf after you approve the card.",
        "I read ~/Downloads/data.csv and summarized it.",
    ],
)
def test_a_plan_an_example_or_a_mention_is_not_a_claim(text):
    assert asserted_paths(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Want me to save this as ~/Downloads/report.docx?",
        "Should I export it to ~/Downloads/report.docx?",
        "I could not save ~/Downloads/report.docx.",
        "I couldn\u2019t write ~/Downloads/report.docx because the folder is locked.",
        "~/Downloads/report.docx was not generated: the script failed.",
        "No file was saved to ~/Downloads/report.docx.",
        "I will write ~/Downloads/report.docx once you confirm the layout.",
        "I'll export ~/Downloads/report.docx next.",
        "I can save it to ~/Downloads/report.docx if you like.",
        "Let me write ~/Downloads/report.docx after the review.",
    ],
)
def test_a_question_a_negation_or_an_offer_is_not_a_claim(text):
    assert asserted_paths(text) == []


def test_each_sentence_is_read_on_its_own():
    text = "Saved to ~/Downloads/a.md. Want me to also save ~/Downloads/b.md?"
    assert asserted_paths(text) == ["~/Downloads/a.md"]


@pytest.mark.parametrize(
    ("text", "claims"),
    [
        ("The chart is in the artifact card above.", True),
        ("See the artifact attached for the numbers.", True),
        ("Corrected: there is no artifact card.", False),
        ("I did not create an artifact card.", False),
        ("Want me to put it in an artifact card?", False),
        ("I can put the chart in an artifact card.", False),
    ],
)
def test_an_artifact_card_is_claimed_only_by_a_statement(text, claims):
    assert claims_an_artifact(text) is claims


# ── a file name with spaces ────────────────────────────────────────────────
# Mac file names often hold spaces. Where the reply marks both ends of the
# path, the whole name is read; a bare one still stops at the first space and
# names nothing, as before.


@pytest.mark.parametrize(
    "text",
    [
        "Saved to `~/Downloads/Q3 Report.docx`.",
        'Saved to "~/Downloads/Q3 Report.docx".',
        "Saved to \N{LEFT DOUBLE QUOTATION MARK}~/Downloads/Q3 Report.docx"
        "\N{RIGHT DOUBLE QUOTATION MARK}.",
        "Saved to [Q3 Report](<~/Downloads/Q3 Report.docx>).",
        "Saved to [Q3 Report](~/Downloads/Q3 Report.docx).",
        "Saved to [Q3 Report](~/Downloads/Q3%20Report.docx).",
        "Done: [Q3 Report.docx](#wsfile=~/Downloads/Q3%20Report.docx&open=os)",
        "Done: [Q3 Report.docx](#wsfile=~/Downloads/Q3 Report.docx&open=os)",
    ],
)
def test_a_marked_path_keeps_the_spaces_in_its_name(text):
    assert asserted_paths(text) == ["~/Downloads/Q3 Report.docx"]


@pytest.mark.parametrize(
    ("text", "claimed"),
    [
        # The words of a file name are not the reply's own words.
        ("Saved to `~/Downloads/Not Final, v2.docx`.", ["~/Downloads/Not Final, v2.docx"]),
        ("Saved as ~/Downloads/example.csv.", ["~/Downloads/example.csv"]),
        ("I read ~/Downloads/ready.docx and summarized it.", []),
        # Two paths in one pair of quotes are two paths.
        ('Saved "~/a.md and ~/b.md".', ["~/a.md", "~/b.md"]),
        # A bare name with a space names no file at all, rather than half of one.
        ("Saved to ~/Downloads/Q3 Report.docx", []),
        ('Want me to save it as "~/Downloads/Q3 Report.docx"?', []),
        ("I could not write `~/Downloads/Q3 Report.docx`.", []),
    ],
)
def test_a_file_name_is_read_as_a_name(text, claimed):
    assert asserted_paths(text) == claimed


def test_the_apps_own_link_to_a_file_with_spaces_checks_out(tmp_path):
    # The app's link url-quotes the path; read raw, a real file was reported
    # missing on every such save.
    from server.index.citations import created_file_link

    report = tmp_path / "Q3 Report (final).docx"
    report.write_bytes(b"docx")
    history = [
        {"role": "user", "content": "write the q3 report"},
        _say(f"Saved: {created_file_link(report.name, str(report))}"),
    ]
    assert check_claims(history, None) is None
    report.unlink()
    assert str(report) in check_claims(history, None)


@pytest.mark.parametrize(
    ("text", "claimed"),
    [
        ("The report is available at https://acme.com/report.pdf.", []),
        ("Uploaded to s3://bucket/q3/report.csv and saved to ~/r.csv.", ["~/r.csv"]),
        ("Saved `~/a.md` (the guide is at https://example.com/help.html).", ["~/a.md"]),
        ("Saved to file:///Users/me/Q3%20Report.docx.", ["/Users/me/Q3 Report.docx"]),
        ("Saved:/Users/me/q3.docx", ["/Users/me/q3.docx"]),
    ],
)
def test_a_web_address_is_not_a_file_on_disk(text, claimed):
    assert asserted_paths(text) == claimed


# ── a table of files ───────────────────────────────────────────────────────
# A table whose header names a file, path, location, output or saved column
# lists what was made, with no completion word in the row to say so.


@pytest.mark.parametrize("column", ["File", "Path", "Location", "Output", "Saved to", "file name"])
def test_a_table_that_lists_files_claims_its_rows(column):
    table = (
        f"| Deliverable | {column} |\n|---|:---:|\n"
        "| Report | ~/Downloads/q3.docx |\n"
        "| Chart | `~/Downloads/Q3 Chart.png` |\n"
        "| Notes | ~/Downloads/Q3 Notes.md |"
    )
    assert asserted_paths("Done:\n\n" + table) == [
        "~/Downloads/q3.docx",
        "~/Downloads/Q3 Chart.png",
        "~/Downloads/Q3 Notes.md",
    ]


@pytest.mark.parametrize(
    "text",
    [
        # No file column: a path in a cell is a mention, as in prose.
        "| Step | Result |\n|---|---|\n| Export | ~/Downloads/q3.docx |",
        # The row itself negates, offers or asks.
        "| File | Status |\n|---|---|\n| ~/Downloads/q3.docx | not created |",
        "| File | Status |\n|---|---|\n| ~/Downloads/q3.docx | will be written after review |",
        "| File | Status |\n|---|---|\n| ~/Downloads/q3.docx | create it now? |",
        # So does the line that introduces the table.
        "I will create these files:\n\n| File | Purpose |\n|---|---|\n| ~/Downloads/q3.docx | report |",
        "I could not save these:\n\n| File | Reason |\n|---|---|\n| ~/Downloads/q3.docx | locked |",
        # A table of changes lists a file that is gone.
        "| File | Change |\n|---|---|\n| /Users/me/repo/docs/old-guide.md | Deleted |",
        "| File | Change |\n|---|---|\n| /Users/me/repo/notes.md | moved to the archive |",
    ],
)
def test_a_table_row_that_does_not_say_the_file_was_made_is_not_a_claim(text):
    assert asserted_paths(text) == []


def test_only_the_row_says_its_file_is_gone():
    # The lead-in may mention what went away; the rows are what was made.
    table = (
        "I removed the old drafts and created these:\n\n| File |\n|---|\n| ~/Downloads/q3.docx |"
    )
    assert asserted_paths(table) == ["~/Downloads/q3.docx"]
    changes = "| File | Change |\n|---|---|\n| ~/a.md | updated |\n| ~/b.md | removed |"
    assert asserted_paths(changes) == ["~/a.md"]


def test_a_row_of_a_file_table_names_the_missing_file(tmp_path):
    (tmp_path / "q3.docx").write_bytes(b"docx")
    reply = (
        "Everything is saved:\n\n| File | Contents |\n|---|---|\n"
        f"| {tmp_path}/q3.docx | the report |\n| {tmp_path}/q3 chart.png | the chart |"
    )
    feedback = check_claims([{"role": "user", "content": "write the q3 report"}, _say(reply)], None)
    assert feedback is not None
    assert f"{tmp_path}/q3 chart.png" in feedback and f"{tmp_path}/q3.docx" not in feedback


# ── which rows the checks read ────────────────────────────────────────────


def test_a_background_task_note_ahead_of_the_prompt_does_not_hide_it():
    history = [
        {"role": "user", "content": "what does the audit agent do?"},
        _say("It audits the repo."),
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _BACKGROUND_NOTE},
                {"type": "text", "text": "save the findings as findings.md to ~/Downloads"},
            ],
        },
    ]
    assert last_user_prompt(history) == "save the findings as findings.md to ~/Downloads"
    assert turn_messages(history) == []


def test_a_row_of_engine_blocks_only_still_continues_the_turn():
    history = [
        {"role": "user", "content": "write the report"},
        _say("Writing it now."),
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "<system-reminder>You are near the round cap.</system-reminder>",
                },
                {
                    "type": "text",
                    "text": "<user_message_mid_turn>note\n\nuse A4</user_message_mid_turn>",
                },
            ],
        },
    ]
    assert last_user_prompt(history) == "write the report\n\nuse A4"
    assert len(turn_messages(history)) == 2


def test_a_user_who_types_continue_starts_a_turn_of_their_own():
    history = [
        {"role": "user", "content": "draft the notes"},
        _say("Here is the first half."),
        {"role": "user", "content": "Continue exactly where you left off, and add a summary"},
    ]
    assert last_user_prompt(history) == "Continue exactly where you left off, and add a summary"


def test_a_claim_in_a_reply_the_gate_skipped_is_still_checked(tmp_path):
    # A pending mid-turn message kept the gate off the first reply; the reply
    # that answers the message does not repeat the claim.
    missing = tmp_path / "report.docx"
    history = [
        {"role": "user", "content": "summarize the meeting"},
        _say(f"I saved the report to {missing}."),
        {
            "role": "user",
            "content": "<user_message_mid_turn>note\n\nwhich font?</user_message_mid_turn>",
        },
        _say("The font is Inter."),
    ]
    feedback = check_claims(history, None)
    assert feedback is not None and str(missing) in feedback


def test_the_first_half_of_a_max_tokens_split_is_checked(tmp_path):
    missing = tmp_path / "report.docx"
    history = [
        {"role": "user", "content": "summarize the meeting"},
        _say(f"Saved to {missing}. The long summary follows"),
        {"role": "user", "content": "Continue exactly where you left off. Do not repeat anything."},
        _say(" and ends here."),
    ]
    feedback = check_claims(history, None)
    assert feedback is not None and str(missing) in feedback


def test_the_halves_of_a_max_tokens_split_are_read_as_one_reply(tmp_path):
    # The chat joins them, and a claim can straddle the cut.
    missing = tmp_path / "report.docx"
    history = [
        {"role": "user", "content": "summarize the meeting"},
        _say("The summary is done and saved the report to"),
        {"role": "user", "content": "Continue exactly where you left off. Do not repeat anything."},
        _say(f" {missing}."),
    ]
    feedback = check_claims(history, None)
    assert feedback is not None and str(missing) in feedback


def test_an_offer_or_a_correction_in_the_reply_is_not_held(tmp_path):
    for reply in (
        f"Here is the summary. Want me to save it as {tmp_path}/notes.md?",
        f"Correction: {tmp_path}/notes.md was not saved.",
    ):
        history = [{"role": "user", "content": "summarize the meeting"}, _say(reply)]
        assert check_claims(history, None) is None, reply


def test_a_reply_a_stop_hook_held_first_is_still_checked(tmp_path):
    # A Stop hook blocks before the claim check runs, so its feedback row
    # does not mean the reply before it was checked.
    missing = tmp_path / "report.docx"
    history = [
        {"role": "user", "content": "write the report and fix the lint"},
        _say(f"Saved the report to {missing} and fixed the lint."),
        {"role": "user", "content": "[completion gate] Stop hook: ruff reports 2 errors"},
        _say("Fixed the remaining two ruff errors."),
    ]
    feedback = check_claims(history, None)
    assert feedback is not None and str(missing) in feedback


def test_an_image_prompt_starts_a_turn_of_its_own(tmp_path):
    # A screenshot prompt carries an image block; the previous turn's claim
    # is not this turn's.
    missing = tmp_path / "report.docx"
    history = [
        {"role": "user", "content": "write the report"},
        _say(f"Saved to {missing}."),
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": ""},
                },
                {"type": "text", "text": "what is in this screenshot?"},
            ],
        },
        _say("It is a login page."),
    ]
    assert last_user_prompt(history) == "what is in this screenshot?"
    assert check_claims(history, None) is None


def test_one_claim_nudge_per_turn(tmp_path):
    missing = tmp_path / "report.docx"
    history = [
        {"role": "user", "content": "summarize the meeting"},
        _say(f"Saved to {missing}."),
    ]
    first = check_claims(history, None)
    assert first is not None and first.startswith(CLAIM_MARKER)
    history += [
        {"role": "user", "content": f"[completion gate] {first}"},
        _say(f"Saved to {missing}."),
    ]
    assert check_claims(history, None) is None


def test_plan_mode_checks_the_artifact_card_but_not_the_files_it_will_write(tmp_path):
    plan = f"Plan: gather the data, then the report goes to {tmp_path}/q3.docx."
    history = [{"role": "user", "content": "plan the quarterly report"}, _say(plan)]
    assert check_claims(history, None) is not None  # the same words, out of plan mode
    assert check_claims(history, None, plan_mode=True) is None
    history[-1] = _say(plan + " The outline is in the artifact card above.")
    assert "artifact card" in check_claims(history, None, plan_mode=True)


def _plan_turn(tool: str, reply: str, tool_input: dict | None = None) -> list:
    return [
        {"role": "user", "content": "plan the quarterly report and save the outline"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Working on the outline."},
                {"type": "tool_use", "id": "t1", "name": tool, "input": tool_input or {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        _say(reply),
    ]


# How each tool plan mode lets write names the file it writes, and the
# extension that file gets.
_WRITES = {
    "save_file": (
        ".docx",
        lambda p: {"filename": "outline.docx", "content": "x", "destination_path": p},
    ),
    "create_docx": (
        ".docx",
        lambda p: {"path": "outline.docx", "html_content": "<p>x</p>", "destination_path": p},
    ),
    "create_pptx": (".pptx", lambda p: {"path": "o.pptx", "slides": [], "destination_path": p}),
    "create_xlsx": (".xlsx", lambda p: {"path": "o.xlsx", "sheets": [], "destination_path": p}),
    "create_pdf": (".pdf", lambda p: {"path": "o.pdf", "paragraphs": [], "destination_path": p}),
    "office_script": (
        ".docx",
        lambda p: {"path": "o.docx", "code": "doc.save(OUTPUT_PATH)", "destination_path": p},
    ),
    "run_python": (".docx", lambda p: {"code": f"doc.save({p!r})"}),
    "terminal_run": (".docx", lambda p: {"command": f"pandoc outline.md -o '{p}'"}),
    "terminal_send": (".docx", lambda p: {"input": f"cp /tmp/outline.docx {p}\n"}),
    "aws_cli": (".docx", lambda p: {"command": f"aws s3 cp s3://bucket/outline.docx {p}"}),
}


def test_every_tool_plan_mode_lets_write_names_its_target_here():
    assert set(_WRITES) == OUTSIDE_WRITERS


@pytest.mark.parametrize("tool", sorted(OUTSIDE_WRITERS))
def test_plan_mode_checks_the_file_a_writing_call_targeted(tool, tmp_path):
    # Plan mode refuses only the ws_* writers; these still run there behind
    # their approval card, so "Saved to" the file one of them wrote is a
    # claim like any other.
    ext, given = _WRITES[tool]
    target = f"{tmp_path}/outline{ext}"
    history = _plan_turn(tool, f"Saved the outline to {target}.", given(target))
    feedback = check_claims(history, None, plan_mode=True)
    assert feedback is not None and target in feedback
    (tmp_path / f"outline{ext}").write_bytes(b"x")
    assert check_claims(history, None, plan_mode=True) is None


def test_plan_mode_leaves_a_planned_file_alone_after_a_command_that_looked_around(tmp_path):
    # Plan mode refuses ws_run_command, so the model looks around with
    # terminal_run, then lists the files its plan will make.
    plan = (
        "Here is the plan:\n\n| File | Purpose |\n|---|---|\n"
        f"| {tmp_path}/q3.docx | the report |\n| {tmp_path}/q3 chart.png | the chart |"
    )
    history = _plan_turn("terminal_run", plan, {"command": f"ls -la {tmp_path}"})
    assert check_claims(history, None, plan_mode=True) is None
    # Out of plan mode the same rows are claims, as before.
    assert f"{tmp_path}/q3.docx" in check_claims(history, None)


def test_plan_mode_checks_only_the_file_the_save_targeted(tmp_path):
    reply = (
        f"Saved the outline to {tmp_path}/outline.docx.\n\n"
        f"Plan: the report is saved to {tmp_path}/q3.docx once you approve it."
    )
    given = _WRITES["save_file"][1](f"{tmp_path}/outline.docx")
    feedback = check_claims(_plan_turn("save_file", reply, given), None, plan_mode=True)
    assert f"{tmp_path}/outline.docx" in feedback and f"{tmp_path}/q3.docx" not in feedback


@pytest.mark.parametrize(
    ("tool", "given", "saved"),
    [
        # A document tool adds its extension to a bare destination.
        (
            "create_docx",
            {"path": "outline", "destination_path": "~/Reports/outline"},
            "~/Reports/outline.docx",
        ),
        # save_file into a folder lands the file under its own name there.
        (
            "save_file",
            {"filename": "outline.docx", "destination_path": "~/Reports"},
            "~/Reports/outline.docx",
        ),
        # With no destination, a save goes to the folder it suggests.
        (
            "save_file",
            {"filename": "outline.docx", "suggested_location": "Downloads"},
            "~/Downloads/outline.docx",
        ),
    ],
)
def test_plan_mode_reads_where_a_writing_call_writes(tool, given, saved, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    history = _plan_turn(tool, f"Saved the outline to {saved}.", given)
    feedback = check_claims(history, None, plan_mode=True)
    assert feedback is not None and saved in feedback


def test_plan_mode_reads_a_workspace_path_against_the_workspace(tmp_path):
    history = _plan_turn(
        "create_docx",
        "Saved: [q3.docx](#wsfile=reports/q3.docx&open=os)",
        {"path": "reports/q3.docx", "html_content": "<p>x</p>"},
    )
    feedback = check_claims(history, str(tmp_path), plan_mode=True)
    assert feedback is not None and "reports/q3.docx" in feedback
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "q3.docx").write_bytes(b"docx")
    assert check_claims(history, str(tmp_path), plan_mode=True) is None


@pytest.mark.parametrize("tool", ["ws_read_file", "ws_write_file", "web_search"])
def test_plan_mode_leaves_the_plans_files_alone_when_nothing_could_write_them(tool, tmp_path):
    # A read, a search, or a workspace write that plan mode refused.
    target = f"{tmp_path}/outline.docx"
    history = _plan_turn(tool, f"Plan: the outline goes to {target}.", {"path": target})
    assert check_claims(history, None, plan_mode=True) is None
    assert check_claims(history, None, plan_mode=False) is not None


def test_the_plan_mode_writers_are_tools_plan_mode_lets_write():
    # Register the executors the way server/main.py does.
    import server.documents.executors  # noqa: F401
    import server.executors.code  # noqa: F401
    import server.executors.terminal_run  # noqa: F401
    import server.workspace  # noqa: F401
    from server.chat.tool_pool import assemble_full_catalog
    from server.executors import EXECUTOR_META
    from server.tool_executor import _PLAN_MODE_BLOCKED

    catalog = {t["name"] for t in assemble_full_catalog(plan_mode=True, ws_connected=True)}
    for name in OUTSIDE_WRITERS:
        assert name in catalog, name
        assert name not in _PLAN_MODE_BLOCKED, name
        assert EXECUTOR_META[name]["read_only"] is False, name


def test_a_file_named_in_an_earlier_reply_of_the_turn_counts_as_produced(tmp_path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")
    history = [
        {"role": "user", "content": f"generate the report as report.pdf to {tmp_path}"},
        _say(f"Saved to {report}"),
        {
            "role": "user",
            "content": "<user_message_mid_turn>note\n\nhow many pages?</user_message_mid_turn>",
        },
        _say("It has 12 pages."),
    ]
    assert produced_a_file(history, None, on_disk=True)


def test_an_input_file_a_reply_mentions_is_not_the_deliverable(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("the notes")
    history = [
        {
            "role": "user",
            "content": f"summarize {notes} and save the summary as a pdf to Downloads",
        },
        _say(f"I read {notes} and summarized it."),
        {
            "role": "user",
            "content": "<user_message_mid_turn>note\n\nmake it shorter</user_message_mid_turn>",
        },
        _say("Here is a shorter summary: three points."),
    ]
    assert not produced_a_file(history, None, on_disk=True)


# ── the engine ─────────────────────────────────────────────────────────────


class _Replies:
    provider = "test"

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        text = self.replies.pop(0)
        yield TextDelta(text=text)
        yield RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": text}], usage=Usage()
        )


@pytest.fixture
def claims_only(monkeypatch):
    from server import hooks

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "deliverable_check")


def _run(sid: str, adapter, prompt: str, *, plan_mode: bool = False, history: tuple = ()) -> str:
    ctx = TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[*history, {"role": "user", "content": prompt}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=12, completion_gate=True),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        plan_mode=plan_mode,
    )

    async def go():
        return "".join([c async for c in run_turn(ctx)])

    return asyncio.run(go())


def test_a_plan_that_names_its_output_file_ends_the_turn(claims_only, tmp_path):
    plan = f"Plan: gather the data, then the report goes to {tmp_path}/q3.docx."
    adapter = _Replies(plan, *[plan] * 10)
    out = _run("claims-plan-mode", adapter, "plan the quarterly report", plan_mode=True)
    assert len(adapter.calls) == 1
    assert "stop_hook_block" not in out


def test_an_honest_correction_ends_the_turn_after_one_nudge(claims_only, tmp_path):
    missing = tmp_path / "report.docx"
    correction = f"Correction: {missing} was not produced."
    adapter = _Replies(f"Saved to {missing}.", *[correction] * 10)
    out = _run("claims-correction", adapter, "summarize the meeting")
    assert len(adapter.calls) == 2
    assert out.count("stop_hook_block") == 1
    assert "goal_cap_reached" not in out


# The history a follow-up turn arrives with, as the chat route rebuilds it:
# plain text, with no trace of the create_artifact call.
_CARD_TURN = (
    {"role": "user", "content": "draw the pipeline as a diagram"},
    {"role": "assistant", "content": "Here it is in the artifact card above."},
)
_ABOUT_THE_CARD = "The deploy stage in the artifact card above is green."


def test_a_reply_about_a_card_an_earlier_turn_made_ends_the_turn(claims_only):
    from server import artifacts

    sid = "claims-earlier-card"
    artifacts.forget_session(sid)
    # What the earlier turn's create_artifact call left (tool_router).
    artifacts.record_artifact(sid, title="Pipeline", html="<p>pipeline</p>")
    try:
        adapter = _Replies(_ABOUT_THE_CARD, *["I rebuilt the card."] * 10)
        out = _run(sid, adapter, "which colour is the deploy stage?", history=_CARD_TURN)
        assert len(adapter.calls) == 1
        assert "stop_hook_block" not in out
    finally:
        artifacts.forget_session(sid)


def test_a_reply_about_a_card_the_session_never_had_is_held_once(claims_only):
    from server import artifacts

    sid = "claims-no-card"
    artifacts.forget_session(sid)
    correction = "Correction: there is no artifact card in this session yet."
    adapter = _Replies(_ABOUT_THE_CARD, *[correction] * 10)
    out = _run(sid, adapter, "which colour is the deploy stage?", history=_CARD_TURN)
    assert len(adapter.calls) == 2
    assert out.count("stop_hook_block") == 1
    feedback = adapter.calls[1][-1]
    assert feedback["role"] == "user" and "create_artifact" in feedback["content"]


class _LookThenPlan:
    """Round 0 looks around with terminal_run, as a plan-mode turn does since
    ws_run_command is refused there; each later round answers with the next
    reply."""

    provider = "test"

    def __init__(self, command: str, *replies: str):
        self.command = command
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        if round_num == 0:
            content = [
                {"type": "text", "text": "Looking at the folder first."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "terminal_run",
                    "input": {"command": self.command},
                },
            ]
            yield TextDelta(text=content[0]["text"])
            yield RoundResult(stop_reason="tool_use", content=content, usage=Usage())
            return
        text = self.replies.pop(0)
        yield TextDelta(text=text)
        yield RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": text}], usage=Usage()
        )


def test_the_engine_ends_a_plan_mode_turn_whose_plan_lists_its_files(
    claims_only, monkeypatch, tmp_path
):
    import server.tool_executor as TE

    async def batch(tool_uses, **kw):
        return list(tool_uses)

    async def process(states, budget_fn, **kw):
        results = [
            {"type": "tool_result", "tool_use_id": t["id"], "content": "q3-data.csv"}
            for t in states
        ]
        return (results, [], False, False)

    monkeypatch.setattr(TE, "execute_tool_batch", batch)
    monkeypatch.setattr(TE, "process_tool_results", process)
    plan = (
        "Here is the plan:\n\n| File | Purpose |\n|---|---|\n"
        f"| {tmp_path}/q3.docx | the report |\n| {tmp_path}/q3.png | the chart |"
    )
    adapter = _LookThenPlan(f"ls -la {tmp_path}", plan, *["Nothing was saved yet."] * 5)
    out = _run("claims-plan-looked-around", adapter, "plan the q3 report", plan_mode=True)

    # The look-around round and the plan; the gate did not hold the plan.
    assert len(adapter.calls) == 2
    assert "stop_hook_block" not in out
