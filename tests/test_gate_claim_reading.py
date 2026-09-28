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


def _run(sid: str, adapter, prompt: str, *, plan_mode: bool = False) -> str:
    ctx = TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": prompt}],
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
