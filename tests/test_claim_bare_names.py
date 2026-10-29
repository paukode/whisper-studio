"""A file named by its name alone is read and checked (claim_names.py).

The claim reader needed a folder in a path, so "I created `inside.txt`" went
unread. Measured on the user's sessions (45 sessions, 7,846 sentences of
prose), that let 3 true claims through unchecked, and no other sentence. The
sentences below are those sessions' own words, with private names and paths
replaced: the 3 claims and the user's 2 example forms must be read, and the
25 sentences the wide scan found that claim nothing must stay unread.
"""

import os
import time

import pytest

from server.goals import claims as c
from server.goals.evidence import Evidence, check, not_done

# ── what is read ─────────────────────────────────────────────────────────

# (sentence, target, made, bare)
READ = [
    (
        'Done: ran `echo hi > inside.txt` (exit code 0), which created `inside.txt` containing "hi"'
        " in your workspace root.",
        "inside.txt",
        True,
        True,
    ),
    (
        "The refresh saved `snapshot-20261021T192122Z.json` successfully.",
        "snapshot-20261021T192122Z.json",
        True,
        True,
    ),
    (
        "The refresh completed successfully and saved `snapshot-20261022T081552Z.json`.",
        "snapshot-20261022T081552Z.json",
        True,
        True,
    ),
    ("`app.dmg` is in ~/Downloads.", "~/Downloads/app.dmg", False, False),
    ("`built.md` is written to Downloads.", "~/Downloads/built.md", True, False),
    ("built.md is written to downloads", "~/Downloads/built.md", True, False),
    ("I created `inside.txt`.", "inside.txt", True, True),
    ("Saved `report.md` to Downloads.", "~/Downloads/report.md", True, False),
    ("Saved the snapshot as `x.json` in your workspace.", "x.json", True, False),
]


@pytest.mark.parametrize("sentence,target,made,bare", READ)
def test_a_file_named_alone_is_read(sentence, target, made, bare):
    files = [x for x in c.read_claims(sentence) if x.kind == c.FILE]
    assert [(x.target, x.made, x.bare, x.firm) for x in files] == [(target, made, bare, True)]


# The wide scan's hits that claim nothing: plans, checks, descriptions, the
# server's own notes and a table of what a file holds.
NOT_CLAIMS = [
    "I’ll review your website and published posts, then save the updated Word file in Downloads.",
    "I’ll update only the opening profile and current AWS section, flag any conflicting details"
    " separately, and save a v2 copy in Downloads.",
    "I’ll find the three largest regular files by scanning the workspace read-only, including"
    " hidden files and dependency directories, without following directory symlinks.",
    "I will find the three largest files in the workspace.",
    "I will find the three smallest workspace files and report their sizes.",
    "I’ll update the visual and companion file to cover only the two tools, then save new versions"
    " in Downloads.",
    "| `.gitlab/ci/ingest.gitlab-ci.yml` | Template, MR validation and scheduled drift job copied"
    " across.",
    "| `.gitlab/ci/warehouse.gitlab-ci.yml` | Template, six jobs, dependencies, deployment gates and"
    " settings copied across.",
    "Step 1 found the core sources: generated SQLAlchemy models under `db-models/db_models`, bronze"
    " PostgreSQL DDL in `warehouse/ddl/bronze_tables_merged.sql`, and ingestion code under"
    " `warehouse/dagster/videos/ingestion`.",
    "However, the extraction_results.json was already generated.",
    "Now let me analyze the extraction_results.json more deeply to find the table mappings:",
    "I’ll derive the table inventory and keys from the actual `~/gitlab` repositories, then save a"
    " concise Markdown handoff in `~/gitlab/docs`.",
    "I’ll update the app to match, use a navy, cobalt and teal palette, and save the new materials"
    " in a separate folder in Downloads.",
    "I’m checking Downloads to distinguish saved work from what I only promised.",
    "*Not saved: `/Users/me/project/regional_opportunities.xlsx` does not exist.*",
    "Desktop and mobile checks pass, and the saved snapshot’s hash is unchanged.",
    "I’ll check for partial output, then retry with permission to write both the repository and"
    " scratch folder.",
    "I’ll check the final desktop and mobile views, remove test residue and save the release"
    " evidence before committing.",
    "*Not saved: `planner/data/snapshots/snapshot-20261022T154215Z.json` was not written in this"
    " turn.*",
    "- `data/refresh-status.json` and `data/snapshots/snapshot-20261022T192352Z.json`: timings,"
    " counts and saved-data comparisons.",
    "Desktop and mobile checks passed: the new timing explanation is readable, the failure message"
    " is explicit, and the unchanged **1,218-row** saved view still loads.",
    "Slack’s documented DM links require a Slack workspace ID and user ID, neither of which is in"
    " the snapshot.",
    "I’ll make Slack use a saved workspace/member-ID mapping, with a setup prompt where that mapping"
    " is missing, so it never opens the wrong person.",
    "I’ll add the desktop persistence test recommended by the portability review, ensuring saved"
    " settings survive relaunch without inheriting development paths.",
    "Not saved: `references/release_provenance.md` does not exist.*",
]

# What the new reading must not take for a claim either.
NOT_CLAIMS += [
    "Dana saved `notes.md` yesterday.",
    "I'll save `notes.md` to Downloads.",
    "I could not create `inside.txt`.",
    "Did you save `notes.md`?",
    "Saved the rows from `input.csv`.",
    "Run `echo hi > inside.txt` to create it.",
    "The nightly job saved `metrics.json`.",
    "Open `report.md` in your editor.",
    "`app.dmg` is ready.",
]


@pytest.mark.parametrize("sentence", NOT_CLAIMS)
def test_a_sentence_that_claims_nothing_stays_unread(sentence):
    assert c.read_claims(sentence) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "Saved new versions in **`/Users/me/Downloads/`**, covering **two tools only**:",
        "I created **two separate workshop documents** and saved them in `~/Downloads/Workshop/`:",
    ],
)
def test_a_folder_lead_line_still_claims_only_its_folder(sentence):
    assert [x.kind for x in c.read_claims(sentence)] == [c.FOLDER]


# ── how it is checked ────────────────────────────────────────────────────


def _bare(name: str) -> c.Claim:
    (claim,) = [x for x in c.read_claims(f"I created `{name}`.") if x.kind == c.FILE]
    return claim


def _turn(*calls) -> list[dict]:
    """A prompt, then one tool round per (name, input, result)."""
    rows = [{"role": "user", "content": "refresh the data"}]
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


def _ev(messages, workspace) -> Evidence:
    return Evidence.of(messages, workspace=str(workspace), started_at=time.time() - 60)


def test_a_bare_name_holds_where_a_call_wrote_it(tmp_path):
    out = tmp_path / "data" / "notes.md"
    out.parent.mkdir()
    out.write_text("x")
    turn = _turn(
        ("ws_create_file", {"path": "data/notes.md", "content": "x"}, "Wrote data/notes.md")
    )
    verdict = check(_bare("notes.md"), _ev(turn, tmp_path))
    assert verdict.ok and verdict.receipt["target"] == str(out)


def test_a_bare_name_holds_where_a_call_result_names_its_path(tmp_path):
    # The refresh ran a script, which named the file it wrote in its result.
    snap = tmp_path / "planner" / "data" / "snapshots" / "snapshot-20261021T192122Z.json"
    snap.parent.mkdir(parents=True)
    snap.write_text("{}")
    turn = _turn(("run_python", {"code": "refresh()"}, f'{{"file": "{snap}", "bytes": 2}}'))
    assert check(_bare(snap.name), _ev(turn, tmp_path)).ok


def test_a_bare_name_holds_in_a_folder_the_turn_listed(tmp_path):
    # The result names the file alone; the turn listed its folder.
    snap = tmp_path / "planner" / "data" / "snapshots" / "snapshot-20261022T081552Z.json"
    snap.parent.mkdir(parents=True)
    snap.write_text("{}")
    turn = _turn(
        ("run_python", {"code": "refresh()"}, f'{{"file": "{snap.name}"}}'),
        ("ws_list_directory", {"path": "planner/data/snapshots"}, snap.name),
    )
    assert check(_bare(snap.name), _ev(turn, tmp_path)).ok


def test_a_bare_name_nothing_wrote_does_not_hold(tmp_path):
    claim = _bare("inside.txt")
    verdict = check(claim, _ev(_turn(("ws_run_command", {"command": "ls"}, "README.md")), tmp_path))
    assert not verdict.ok
    assert not_done(claim, verdict) == (
        "Not saved: no `inside.txt` was found where this turn wrote or looked."
    )


def test_a_bare_name_on_disk_from_before_the_turn_does_not_hold(tmp_path):
    old = tmp_path / "inside.txt"
    old.write_text("hi")
    long_ago = time.time() - 86400
    os.utime(old, (long_ago, long_ago))
    verdict = check(
        _bare("inside.txt"), _ev(_turn(("ws_run_command", {"command": "ls"}, "")), tmp_path)
    )
    assert not verdict.ok and "was not written in this turn" in verdict.note


def test_a_command_that_wrote_the_name_makes_it_hold(tmp_path):
    (tmp_path / "inside.txt").write_text("hi\n")
    turn = _turn(("ws_run_command", {"command": "echo hi > inside.txt"}, "(exit code 0)"))
    assert check(_bare("inside.txt"), _ev(turn, tmp_path)).ok


def test_a_named_place_is_checked_as_a_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    (claim,) = c.read_claims("`app.dmg` is in ~/Downloads.")
    ev = _ev(_turn(), tmp_path)
    assert not check(claim, ev).ok
    (tmp_path / "Downloads" / "app.dmg").write_bytes(b"dmg")
    assert check(claim, ev).ok


def test_the_stream_guard_holds_a_false_bare_name(tmp_path):
    import asyncio
    import json

    from server.chat.claim_guard import ClaimGuard
    from server.chat.engine.events import RoundResult, TextDelta

    async def events():
        yield TextDelta(text="The refresh saved `snapshot-x.json` successfully. Totals are up.")
        yield RoundResult(stop_reason="end_turn", content=[])

    guard = ClaimGuard(workspace=str(tmp_path), started_at=time.time() - 5)

    async def go():
        return [ev async for ev in guard.wrap(events(), [{"role": "user", "content": "refresh"}])]

    shown = "".join(e.text for e in asyncio.run(go()) if isinstance(e, TextDelta))
    assert shown == "Totals are up."
    note = "".join(json.loads(f[6:]).get("text", "") for f in guard.finish())
    assert "Not saved: no `snapshot-x.json` was found" in note
