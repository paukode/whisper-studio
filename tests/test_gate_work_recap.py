"""The recap the work narration rule asks for passes the completion gate.

server/prompts/narration.py closes a chat turn with a recap: one sentence
with the outcome, bullets with a bold lead word, a table of the files the
turn wrote (each named by the link its write result gave), then what was left
undone. The claim check (deliverables.check_claims) must let that recap
through when the turn wrote, deleted or moved the files it lists, and still
hold what the turn did not back: the link in a row of a file table is a file
claim checked on disk, a row under a change column or a made verb in prose
needs the file written in this turn, a row whose verb of removing takes an
object holds when the file was written or removed, and a line count or a
sentence that names a file it read claims nothing.
"""

import os
import time

import pytest

from server.goals import claims as c
from server.goals.claims import read_claims
from server.goals.deliverables import check_claims
from server.goals.evidence import Verdict, not_done
from server.index.citations import created_file_link

REL = "server/prompts/narration.py"
TABLE = "| File | What changed | Lines |\n|---|---|---|\n"
READ_ONLY = "I read server/goals/claims.py and src/utils/wsFileLinks.ts to check the shape."


def _say(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _write_turn(ws, rel: str = REL) -> tuple[list, str]:
    """A turn whose one call wrote ``rel`` in the workspace ``ws``, with the
    result ws_write_file gives (server/approval/executors.py _do_write), and
    the link that result hands the model."""
    full = ws / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    body = 'WORK_NARRATION = "..."\n'
    full.write_text(body)
    link = created_file_link(rel, str(full))
    result = (
        f"Wrote {rel} ({len(body)} bytes). "
        f"To reference this file for the user, copy this link verbatim: {link}"
    )
    rows = [
        {"role": "user", "content": "add the narration rule to the chat prompt"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"Writing the rule into {rel} so it stays cached."},
                {
                    "type": "tool_use",
                    "id": "w1",
                    "name": "ws_write_file",
                    "input": {"path": rel, "content": body},
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "w1", "content": result}],
        },
    ]
    return rows, link


def _call_turn(call: dict, result: str) -> list:
    """A turn whose one call is ``call``, answered with ``result``."""
    return [
        {"role": "user", "content": "clean up what is dead"},
        {"role": "assistant", "content": [{"type": "text", "text": "Cleaning up."}, call]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": result}],
        },
    ]


def _stale(ws, rel: str) -> str:
    """``rel`` in ``ws``, last written a day ago; its full path."""
    full = ws / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("export const old = 1;\n")
    day_ago = time.time() - 86400
    os.utime(full, (day_ago, day_ago))
    return str(full)


def _recap(rows: str) -> str:
    """A recap in the shape the rule prescribes, around the table ``rows``."""
    return (
        "The narration rule is in the cloud chat prompt and its tests pass.\n\n"
        "- **Prompt**: one fixed section asks for the narration and the recap, so the "
        "cached block stays stable.\n"
        "- **Cleanup**: dropped the old builder, because nothing called it.\n\n"
        f"{TABLE}{rows}\n\n"
        f"{READ_ONLY}\n\n"
        "Not verified: the live app."
    )


def _gate(messages: list, ws) -> str | None:
    return check_claims(messages, str(ws), started_at=time.time() - 5)


def test_the_prescribed_recap_passes_when_the_turn_wrote_its_files(tmp_path):
    turn, link = _write_turn(tmp_path)
    recap = _recap(f"| {link} | removed the legacy builder and added the section | +18 -6 |")
    (claim,) = read_claims(recap)
    assert claim.kind == c.FILE and claim.target == str(tmp_path / REL)
    assert claim.firm and claim.deliverable and claim.made
    assert _gate([*turn, _say(recap)], tmp_path) is None


def test_a_row_naming_a_file_the_turn_never_wrote_is_held(tmp_path):
    turn, link = _write_turn(tmp_path)
    missing = tmp_path / "src/utils/pathLinks.ts"
    rows = (
        f"| {link} | new section | +18 -0 |\n"
        f"| {created_file_link('src/utils/pathLinks.ts', str(missing))} | new linkifier | +90 -0 |"
    )
    feedback = _gate([*turn, _say(_recap(rows))], tmp_path)
    assert feedback is not None
    held = [line for line in feedback.splitlines() if line.startswith('- "')]
    assert len(held) == 1 and f"`{missing}` does not exist" in held[0]


def test_a_row_that_removed_code_from_a_file_is_an_edit_of_it(tmp_path):
    # Read as a removal, the row held a true recap as "still exists".
    turn, link = _write_turn(tmp_path)
    edited = _recap(f"| {link} | removed the stepFormat pass | +0 -12 |")
    assert _gate([*turn, _say(edited)], tmp_path) is None
    gone = _recap(f"| {link} | deleted | +0 -12 |")
    feedback = _gate([*turn, _say(gone)], tmp_path)
    assert feedback is not None and "still exists" in feedback


@pytest.mark.parametrize(
    ("rel", "change"),
    [
        ("src/utils/stepNarration.ts", "deleted the util, nothing calls it"),
        ("src/utils/stepNarration.ts", "removed the dead module"),
        ("src/utils/stepNarration.ts", "removed for good"),
        ("src/utils/stepNarration.ts", "deleted - dead code"),
        ("src/utils/stepNarration.ts", "deleted"),
        ("docs/old-guide.md", "removed the page"),
        ("docs/old-guide.md", "deleted the whole file"),
    ],
)
def test_a_row_for_a_file_the_turn_deleted_passes(tmp_path, rel, change):
    # A verb of removing with an object may say the file went: read as an
    # edit alone, this true recap was held as "does not exist".
    call = {"type": "tool_use", "id": "d1", "name": "ws_delete_file", "input": {"path": rel}}
    turn = _call_turn(call, f"Deleted {rel}")
    recap = _recap(f"| {rel} | {change} | +0 -40 |")
    assert _gate([*turn, _say(recap)], tmp_path) is None


@pytest.mark.parametrize("change", ["moved under lib/", "renamed for clarity", "moved to lib/a.ts"])
def test_a_row_for_a_file_the_turn_moved_passes(tmp_path, change):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/a.ts").write_text("export const a = 1;\n")
    command = "mv src/a.ts lib/a.ts"
    call = {"type": "tool_use", "id": "m1", "name": "ws_run_command", "input": {"command": command}}
    turn = _call_turn(call, "")
    assert _gate([*turn, _say(_recap(f"| src/a.ts | {change} | +0 -0 |"))], tmp_path) is None


def test_a_row_that_may_mean_either_is_held_when_neither_happened(tmp_path):
    stale = _stale(tmp_path, "src/b.ts")
    missing = str(tmp_path / "src/gone.ts")
    rows = (
        f"| {created_file_link('src/b.ts', stale)} | removed the dead module | +0 -40 |\n"
        f"| {created_file_link('src/gone.ts', missing)} | removed the dead module | +0 -9 |"
    )
    prompt = {"role": "user", "content": "clean up what is dead"}
    feedback = _gate([prompt, _say(_recap(rows))], tmp_path)
    held = [line for line in feedback.splitlines() if line.startswith('- "')]
    assert len(held) == 2
    assert f"`{stale}` was not written in this turn" in held[0]
    assert f"`{missing}` does not exist" in held[1]
    (claim, _missing) = read_claims(_recap(rows))
    assert claim.or_removed
    note = not_done(claim, Verdict(False, note=f"`{stale}` was not written in this turn"))
    assert note.startswith("Not changed: ")


@pytest.mark.parametrize(
    "lines",
    ["+18 -6", "+18 / -6", "18 added, 6 removed", "+18 lines, 6 removed", "removed 6 lines"],
)
def test_line_counts_in_any_notation_claim_no_removal(tmp_path, lines):
    # Read as a verb of the row's, "6 removed" held a written file as
    # "still exists".
    turn, link = _write_turn(tmp_path)
    recap = _recap(f"| {link} | new section | {lines} |")
    (claim,) = read_claims(recap)
    assert claim.kind == c.FILE and claim.made and not claim.or_removed
    assert _gate([*turn, _say(recap)], tmp_path) is None


def test_a_row_with_no_verb_needs_its_file_written_in_this_turn(tmp_path):
    # The mockup's cells name what changed with no verb ("Group label, diff
    # pill, detail list"); under a change column that still says the file
    # was changed in this turn.
    stale = _stale(tmp_path, "src/old.ts")
    link = created_file_link("src/old.ts", stale)
    asks = {"role": "user", "content": "add the pre-pass"}
    for cell in ("new section", "+18 -6", "Group label, diff pill, detail list"):
        feedback = _gate([asks, _say(_recap(f"| {link} | {cell} | +18 -6 |"))], tmp_path)
        assert feedback is not None and "was not written in this turn" in feedback, cell
    # Asked about earlier work, the same row is a recap of it.
    about = {"role": "user", "content": "what did you change in src/old.ts yesterday?"}
    assert _gate([about, _say(_recap(f"| {link} | new section | +18 -6 |"))], tmp_path) is None
    turn, written = _write_turn(tmp_path)
    assert _gate([*turn, _say(_recap(f"| {written} | new section | +18 -6 |"))], tmp_path) is None


def test_a_made_verb_needs_the_file_written_in_this_turn(tmp_path):
    old = tmp_path / "src/a.ts"
    old.parent.mkdir(parents=True)
    old.write_text("export const a = 1;\n")
    hour_ago = time.time() - 3600
    os.utime(old, (hour_ago, hour_ago))
    said = "Edited src/a.ts to add the pre-pass."
    (claim,) = read_claims(said)
    assert claim.kind == c.FILE and claim.made and claim.firm
    prompt = {"role": "user", "content": "add the pre-pass"}
    feedback = _gate([prompt, _say(said)], tmp_path)
    assert feedback is not None and "was not written in this turn" in feedback
    turn, _link = _write_turn(tmp_path, "src/a.ts")
    assert _gate([*turn, _say(said)], tmp_path) is None


def test_counts_and_files_read_claim_nothing(tmp_path):
    for text in (
        "Changed 3 files, +120 -14 lines.",
        "3 files changed, 120 insertions, 14 deletions.",
        READ_ONLY,
        "Not verified: I did not open src/a.ts in the browser.",
    ):
        assert read_claims(text) == [], text
    prompt = {"role": "user", "content": "how big was the change?"}
    reply = "Changed 3 files, +120 -14 lines. " + READ_ONLY
    assert _gate([prompt, _say(reply)], tmp_path) is None


@pytest.mark.parametrize(
    "change",
    [
        "legacy fallback removed",
        "removed now-unused imports",
        "removed in-place mutation",
        "dead code removed; retry added",
        "removed and replaced the retry loop",
        "removed as part of the cleanup: the old flag",
    ],
)
def test_a_row_that_removed_a_part_of_a_written_file_passes(tmp_path, change):
    # Read as the file going, each of these true recaps of an edit was held
    # as "still exists": the verb of removing neither opens the cell nor
    # follows the file, or its object follows a word read as "no object".
    turn, link = _write_turn(tmp_path)
    recap = _recap(f"| {link} | {change} | +3 -9 |")
    (claim,) = read_claims(recap)
    assert claim.kind == c.FILE and claim.or_removed
    assert _gate([*turn, _say(recap)], tmp_path) is None


@pytest.mark.parametrize("change", ["removed", "file removed", "the whole file deleted"])
def test_a_row_whose_file_itself_went_still_claims_its_removal(tmp_path, change):
    turn, link = _write_turn(tmp_path)
    (claim,) = read_claims(_recap(f"| {link} | {change} | +0 -9 |"))
    assert claim.kind == c.REMOVED


def _listing_turn(ask: str, home) -> list:
    command = f"ls -lt {home}"
    call = {"type": "tool_use", "id": "l1", "name": "terminal_run", "input": {"command": command}}
    return [
        {"role": "user", "content": ask},
        {"role": "assistant", "content": [{"type": "text", "text": "Listing the folder."}, call]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "l1", "content": "q3.pdf"}],
        },
    ]


@pytest.mark.parametrize(
    ("ask", "table"),
    [
        (
            "Show the files in my Reports folder, newest first",
            "Here are the files, newest first:\n\n| File | Size | Modified |\n|---|---|---|\n"
            "| `{doc}` | 5 B | 2026-10-15 |\n",
        ),
        (
            "Find the PDFs in my Reports folder",
            "Found one PDF:\n\n| File | Last modified |\n|---|---|\n| `{doc}` | Oct 15 |\n",
        ),
        (
            "Review Sam's PR #42",
            "Sam's PR changes these files:\n\n| File | Change |\n|---|---|\n"
            "| `{doc}` | adds a VPN section |\n",
        ),
    ],
)
def test_a_change_column_over_a_plain_path_says_where_the_file_is(tmp_path, ask, table):
    # A date column in a listing, or a review of someone else's change, is
    # no claim that this turn wrote the file: only the write result's own
    # link under a change column is (test_a_row_with_no_verb_needs_its_file_
    # written_in_this_turn).
    doc = _stale(tmp_path, "Reports/q3.pdf")
    reply = table.format(doc=doc)
    (claim,) = read_claims(reply)
    assert claim.kind == c.FILE and not claim.made
    assert _gate([*_listing_turn(ask, tmp_path), _say(reply)], tmp_path) is None


def test_the_prescribed_header_is_a_change_column(tmp_path):
    # The rule names the header, so the reader finds the change column in
    # every recap the model writes to it, and a false row there is held.
    from server.prompts.narration import WORK_NARRATION

    header = "| File | What changed | Lines |"
    assert header in WORK_NARRATION
    stale = _stale(tmp_path, "src/old.ts")
    row = f"| {created_file_link('src/old.ts', stale)} | new pre-pass for paths | +18 -6 |"
    reply = f"Added the pre-pass.\n\n{header}\n|---|---|---|\n{row}\n"
    asks = {"role": "user", "content": "add the pre-pass"}
    feedback = _gate([asks, _say(reply)], tmp_path)
    assert feedback is not None and "was not written in this turn" in feedback
