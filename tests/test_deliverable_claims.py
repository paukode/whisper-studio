"""server.goals.deliverables: pull the file paths and artifact references a
reply presents as deliverables out of provider-neutral messages, and verify
them. Twice in real sessions the reply said "saved" / "artifact card above"
and neither existed."""

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
