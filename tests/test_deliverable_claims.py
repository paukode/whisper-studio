"""server.goals.deliverables: pull the file paths and artifact references a
reply presents as deliverables out of provider-neutral messages, and verify
them. Twice in real sessions the reply said "saved" / "artifact card above"
and neither existed."""

import pytest

from server.chat.engine import midturn_inbox
from server.chat.engine.runner import _midturn_text, _remind
from server.chat.loop_hints import WIND_DOWN_AT, near_cap_reminder
from server.goals import deliverables as d


def test_claimed_paths_finds_wsfile_links_and_absolute_deliverable_paths():
    text = (
        "[Open it](#wsfile=/Users/me/Documents/Report_Validated.docx&open=os) and also "
        "Saved to: `/Users/me/Documents/Report_Standalone.docx`. See ~/out/summary.pdf. "
        "I edited /Users/me/repo/src/app.py and read docs/notes.md."
    )
    paths = d.claimed_paths(text)
    assert paths == [
        "/Users/me/Documents/Report_Validated.docx",
        "/Users/me/Documents/Report_Standalone.docx",
        "~/out/summary.pdf",
    ]


def test_claimed_paths_dedupes_and_ignores_prose_without_paths():
    text = "Saved `/a/b.docx`. Again: /a/b.docx. Nothing else here."
    assert d.claimed_paths(text) == ["/a/b.docx"]
    assert d.claimed_paths("all done, no files") == []


def test_turn_messages_start_after_the_last_real_user_prompt():
    msgs = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "ws_grep", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "hits"}]},
        {"role": "assistant", "content": "partial"},
        {"role": "user", "content": "[completion gate] not yet"},
        {"role": "assistant", "content": "final"},
    ]
    turn = d.turn_messages(msgs)
    assert len(turn) == 5 and turn[0]["role"] == "assistant"


def test_artifact_created_detects_anthropic_and_responses_shapes():
    anth = [
        {"role": "user", "content": "make it"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "create_artifact", "input": {}}],
        },
    ]
    resp = [
        {"role": "user", "content": "make it"},
        {"role": "assistant", "content": [{"type": "function_call", "name": "create_artifact"}]},
    ]
    none = [{"role": "user", "content": "make it"}, {"role": "assistant", "content": "done"}]
    assert d.artifact_created(anth) and d.artifact_created(resp)
    assert not d.artifact_created(none)


def test_check_claims_names_missing_files_and_passes_existing_ones(tmp_path):
    real = tmp_path / "ok.docx"
    real.write_bytes(b"x")
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    msgs = [
        {"role": "user", "content": "report please"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": f"Saved to `{real}`, `{empty}` and `{tmp_path}/missing.docx`.",
                }
            ],
        },
    ]
    fb = d.check_claims(msgs, None)
    assert fb and "missing.docx" in fb and "empty.pdf" in fb and "ok.docx" not in fb

    ok_only = [msgs[0], {"role": "assistant", "content": f"Saved to `{real}`."}]
    assert d.check_claims(ok_only, None) is None


def test_check_claims_resolves_relative_wsfile_links_against_the_workspace(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "r.md").write_text("x")
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "[Open](#wsfile=out/r.md&open=os)"},
    ]
    assert d.check_claims(msgs, str(tmp_path)) is None
    assert d.check_claims(msgs, None) is not None


def test_check_claims_flags_an_artifact_card_claim_without_a_create_artifact_call():
    msgs = [
        {"role": "user", "content": "build the app"},
        {"role": "assistant", "content": "Done. The app is available in the artifact card above."},
    ]
    fb = d.check_claims(msgs, None)
    assert fb and "artifact card" in fb and "create_artifact" in fb
    with_call = [
        msgs[0],
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "create_artifact", "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        msgs[1],
    ]
    assert d.check_claims(with_call, None) is None


# ── rows the engine writes into a running turn ─────────────────────────────
# When the loop carries on past an apparent end of turn (a late mid-turn
# message, a pause_turn, a max_tokens cut), what it writes next is a user row
# of its own after the assistant turn: the late message or a reminder
# (runner._remind), or the continuation prompt. None of them starts a new
# turn. Rows the runner has a helper for are built with it.


def _late(messages: list, text: str, kind: str = midturn_inbox.USER) -> list:
    _remind(messages, _midturn_text(midturn_inbox.Entry(kind, text)))
    return messages


def _artifact_turn() -> list:
    return [
        {"role": "user", "content": "draw the pipeline as a diagram"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "a1", "name": "create_artifact", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "a1", "content": "ok"}],
        },
        {"role": "assistant", "content": "Here it is in the artifact card above."},
    ]


def test_a_late_mid_turn_message_does_not_start_a_new_turn():
    msgs = _late(_artifact_turn(), "No, dont do that for sebastian, ignore it.")
    assert len(msgs) == 5 and msgs[-1]["role"] == "user"
    msgs.append({"role": "assistant", "content": "Understood, I left it as it is."})
    assert d.turn_messages(msgs) == msgs[1:]


@pytest.mark.parametrize(
    "add_row",
    [
        lambda msgs: _remind(msgs, near_cap_reminder(WIND_DOWN_AT)),
        lambda msgs: _late(msgs, "FINDINGS: save a pdf to downloads", midturn_inbox.AGENT_REPORT),
        lambda msgs: msgs.append(
            {
                "role": "user",
                "content": "Continue exactly where you left off. Do not repeat anything.",
            }
        ),
    ],
    ids=["reminder", "agent-report", "max-tokens-continuation"],
)
def test_engine_rows_after_an_assistant_turn_do_not_start_a_new_turn(add_row):
    msgs = _artifact_turn()
    add_row(msgs)
    msgs.append({"role": "assistant", "content": "done"})
    assert d.turn_messages(msgs) == msgs[1:]
    assert d.last_user_prompt(msgs) == "draw the pipeline as a diagram"


def test_an_artifact_created_before_a_late_message_still_counts():
    msgs = _late(_artifact_turn(), "also label the stages")
    msgs.append({"role": "assistant", "content": "Labelled, see the artifact card above."})
    assert d.artifact_created(msgs) is True
    assert d.check_claims(msgs, None) is None


@pytest.mark.parametrize("tail", [1, 3, 4], ids=["on-the-prompt", "on-a-tool-result", "own-row"])
def test_what_the_user_sent_mid_turn_is_part_of_the_request_wherever_it_lands(tail):
    # The runner folds a mid-turn message onto the user row that ends the
    # list, or gives it a row of its own after an assistant turn. An agent
    # report folded in at the same spot is agent output, never the request.
    msgs = _late(_artifact_turn()[:tail], "make it horizontal")
    _late(msgs, "FINDINGS: save a pdf to downloads", midturn_inbox.AGENT_REPORT)
    assert d.last_user_prompt(msgs) == "draw the pipeline as a diagram\n\nmake it horizontal"
