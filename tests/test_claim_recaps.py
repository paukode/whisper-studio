"""A claim the turn did not back rests on earlier work only as a recap
(server/goals/evidence.py): the conversation shows it was done (a delivery
an earlier reply verified, which the chat sends with its history, or an
agent's report), the turn did not try it again, and the claim reads as a
recap: it says so, it states what now is, or the user asked about earlier
work rather than for new work.
"""

import json
import os
import time

from server.goals import claims as c
from server.goals.claims import read_claims
from server.goals.deliverables import check_claims, verified_deliveries
from server.goals.evidence import Evidence, check

PUSHED = [{"kind": "push", "target": "main", "label": "Pushed", "detail": "to main"}]
EMAILED = [{"kind": "message", "target": "Dana", "label": "Sent", "detail": "to Dana"}]


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


def _check(reply, *calls, prompt="go", receipts=None, before=(), **kw):
    ev = Evidence.of(
        _turn(*calls, prompt=prompt, before=before),
        receipts=receipts,
        started_at=time.time() - 5,
        **kw,
    )
    return [check(cl, ev).ok for cl in read_claims(reply)]


def test_a_new_request_is_never_met_by_the_same_act_done_earlier():
    for prompt in ("now fix the retry bug and push it", "push it", "go", "yes, do it"):
        assert _check("Pushed the retry fix to `main`.", prompt=prompt, receipts=PUSHED) == [False]
    assert _check(
        "Sent the revised deck to Dana.", prompt="send Dana the deck", receipts=EMAILED
    ) == [False]


def test_a_question_about_earlier_work_is_answered_from_what_was_verified():
    assert _check(
        "Yes, I pushed the fix to `main`.", prompt="did you push it?", receipts=PUSHED
    ) == [True]
    # "Did you?" asks about it; "can you?" asks for it, and is checked as new.
    asked = "did you push it? if not, push it now"
    assert _check("Yes, I pushed the fix to `main`.", prompt=asked, receipts=PUSHED) == [True]
    assert _check("Pushed the fix to `main`.", prompt="can you push it?", receipts=PUSHED) == [
        False
    ]
    # Nothing on record: no recap.
    assert _check("Yes, I pushed the fix to `main`.", prompt="did you push it?") == [False]
    # Another target on record: no recap either.
    assert _check("Yes, I pushed `fix-y`.", prompt="did you push it?", receipts=PUSHED) == [False]


def test_a_recap_says_so_in_its_own_clause_or_at_the_head_of_its_sentence():
    (said,) = read_claims("As I mentioned earlier, I pushed the fix to `main`.")
    assert said.earlier
    for text in (
        "I pushed the fix for the crash you reported earlier to `main`.",
        "Pushed the fix to `main`; the previously failing test now passes.",
        "I emailed Dana the numbers you asked for yesterday.",
    ):
        assert not any(x.earlier for x in read_claims(text)), text
    assert _check("Earlier I pushed the fix to `main`.", prompt="push it", receipts=PUSHED) == [
        True
    ]


def test_what_now_is_rests_on_what_was_verified():
    assert _check("The fix is on `main`.", prompt="push it", receipts=PUSHED) == [True]
    assert _check("The fix is on `main`.", prompt="push it") == [False]


def test_the_earlier_replies_words_alone_are_no_record():
    before = [
        {"role": "user", "content": "push the fix"},
        {"role": "assistant", "content": "Pushed the fix to `main`."},
    ]
    assert _check("Yes, I pushed it to `main`.", prompt="did you push it?", before=before) == [
        False
    ]


def test_trying_again_this_turn_closes_the_recap():
    failed = ("git_push", {"branch": "main"}, "Error: rejected")
    assert _check("Yes, I pushed to `main`.", failed, prompt="did it push?", receipts=PUSHED) == [
        False
    ]
    other = ("git_push", {"branch": "fix-y"}, "Error: rejected")
    assert _check("Yes, I pushed to `main`.", other, prompt="did it push?", receipts=PUSHED) == [
        True
    ]


def test_an_agents_report_is_a_record_and_its_negations_are_not():
    report = {
        "agent_id": "a1",
        "status": "completed",
        "output": "Pushed `fix-x` to origin after the tests passed.",
    }
    spawn = ("spawn_agent", {"task": "push fix-x"}, json.dumps(report))
    assert _check("Pushed `fix-x` to origin.", spawn) == [True]
    assert _check("I pushed the fix to `main` and opened PR #12.", spawn) == [False, False]
    nothing = {
        "agent_id": "a1",
        "status": "completed",
        "output": "Nothing pushed; I pushed nothing.",
    }
    idle = ("spawn_agent", {"task": "review"}, json.dumps(nothing))
    assert _check("The reviewer approved it and I pushed the fix to `main`.", idle) == [False]


def test_a_report_row_before_the_turn_answers_a_question_about_it():
    row = (
        '[Agent reports from "agents": finished while no turn was running. These were not shown '
        "to the user; relay what matters and how confident each report is.]\n\n"
        "## a1 (general) [completed, 6 rounds]\n"
        "Pushed `fix-x` to origin and opened PR #12: https://github.com/acme/app/pull/12"
    )
    before = [
        {"role": "user", "content": "have an agent push fix-x and open the PR"},
        {"role": "assistant", "content": "Started a background agent for it."},
        {"role": "user", "content": row},
        {"role": "assistant", "content": "(no answer was produced)"},
    ]
    reply = "- Pushed `fix-x` to origin\n- Opened PR #12: https://github.com/acme/app/pull/12"
    assert _check(reply, prompt="so what happened?", before=before) == [True, True]
    rows = [
        *_turn(prompt="so what happened?", before=before),
        {"role": "assistant", "content": reply},
    ]
    assert check_claims(rows, None, started_at=time.time() - 5) is None


def test_a_file_the_turn_failed_to_write_is_not_there_because_an_old_copy_is(tmp_path):
    q3 = tmp_path / "q3.docx"
    q3.write_text("last week's report")
    old = time.time() - 7 * 86400
    os.utime(q3, (old, old))
    failed = ("create_docx", {"destination_path": str(q3)}, "[Tool Error] python-docx failed")
    for reply in (f"Here's the updated report: {q3}", f"The new version is at `{q3}`."):
        assert _check(reply, failed) == [False], reply
    assert _check(f"The report from last week is at `{q3}`.") == [True]


def test_plan_mode_checks_a_document_the_reply_says_it_saved():
    assert _check("I saved the plan to ~/Downloads/nobody-plan.md.", plan_mode=True) == [False]


def test_a_link_from_the_users_own_words_is_no_delivery():
    before = [
        {"role": "user", "content": "the gist is https://gist.github.com/acme/1"},
        {"role": "assistant", "content": "Noted."},
    ]
    reply = "I created a gist with the fixed script at https://gist.github.com/acme/1."
    assert _check(reply, before=before) == [False]


def test_the_chat_sends_verified_deliveries_and_the_prompt_never_holds_them():
    from server.chat.attachment_context import rebuild_history_message

    row = {
        "role": "assistant",
        "content": "Pushed to `main`.",
        "deliveries": [{"kind": "push", "target": "main", "label": "Pushed", "at": "x"}],
    }
    assert verified_deliveries([{"role": "user", "content": "q"}, row]) == [
        {"kind": "push", "target": "main", "detail": "", "href": ""}
    ]
    assert rebuild_history_message(row) == {"role": "assistant", "content": "Pushed to `main`."}


def test_the_gate_reads_the_same_record():
    rows = [
        *_turn(prompt="did you push it?"),
        {"role": "assistant", "content": "Yes, I pushed the fix to `main`."},
    ]
    assert check_claims(rows, None, started_at=time.time() - 5)
    assert check_claims(rows, None, started_at=time.time() - 5, receipts=PUSHED) is None
    assert c.PUSH == PUSHED[0]["kind"]


def test_only_a_correction_settles_a_claim_for_the_gate():
    turn = _turn(prompt="save the deck")
    false = {"role": "assistant", "content": "I saved the deck to ~/Downloads/nobody-q3.pptx."}
    for later, settled in (
        ("Open `nobody-q3.pptx` in Keynote to review it.", False),
        ("Sorry, I could not create `nobody-q3.pptx`.", True),
    ):
        rows = [
            *turn,
            false,
            {"role": "user", "content": "[completion gate] x"},
            {"role": "assistant", "content": later},
        ]
        assert (check_claims(rows, None, started_at=time.time() - 5) is None) is settled, later
