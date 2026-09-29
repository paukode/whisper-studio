"""What a reply claims it delivered, beyond a saved document.

server/goals/claims.py reads every delivery a reply reports: other files,
folders and removals, and the acts that leave nothing on this disk (a push,
a commit, a pull request, a merge, an issue, an upload, a message, a deploy,
a schedule, a link, a memory, a setting). Each is the assistant's own
statement; a plan, an offer, a question, a negation, a third party's act or
a description of work claims nothing. The document rules themselves are
pinned in tests/test_gate_claim_reading.py.
"""

import pytest

from server.goals import claims as c
from server.goals.claims import read_claims


def _read(text: str) -> list[tuple[str, str]]:
    return [(cl.kind, cl.target) for cl in read_claims(text)]


@pytest.mark.parametrize(
    ("text", "claimed"),
    [
        ("I've pushed the fix to `origin/main`.", [(c.PUSH, "origin/main")]),
        ("Pushed to main.", [(c.PUSH, "main")]),
        ("All changes are committed and pushed.", [(c.COMMIT, ""), (c.PUSH, "")]),
        ("Committed, then pushed to `main`.", [(c.COMMIT, ""), (c.PUSH, "main")]),
        (
            "Opened PR #42: https://github.com/acme/app/pull/42",
            [(c.PR, "https://github.com/acme/app/pull/42")],
        ),
        ("I merged the PR into main.", [(c.MERGE, "")]),
        ("Filed an issue for the flaky test (#77).", [(c.ISSUE, "#77")]),
        (
            "Uploaded the export to s3://acme-data/exports/q3.csv.",
            [(c.UPLOAD, "s3://acme-data/exports/q3.csv")],
        ),
        ("Sent the summary to Dana.", [(c.MESSAGE, "Dana")]),
        ("I emailed the report to bob@acme.com.", [(c.MESSAGE, "bob@acme.com")]),
        ("Posted the update in #general.", [(c.MESSAGE, "#general")]),
        ("Deployed the app to production.", [(c.PUBLISH, "")]),
        ("Published v2.11.2 to PyPI.", [(c.PUBLISH, "v2.11.2")]),
        ("I scheduled a daily task to check the build.", [(c.SCHEDULE, "")]),
        ("Saved that to your memory.", [(c.MEMORY, "")]),
        ("I turned on the dark mode setting.", [(c.SETTING, "")]),
        ("I updated `server/app.py` to retry on 503.", [(c.FILE, "server/app.py")]),
        ("Removed `old/helper.py`.", [(c.REMOVED, "old/helper.py")]),
        ("server/app.py was updated to log the region.", [(c.FILE, "server/app.py")]),
        ("Saved the charts to ~/Downloads/charts/", [(c.FOLDER, "~/Downloads/charts/")]),
        ("I saved the report to ~/Downloads.", [(c.FOLDER, "~/Downloads")]),
        (
            "I created a gist at https://gist.github.com/acme/abc123.",
            [(c.LINK, "https://gist.github.com/acme/abc123")],
        ),
        ("- Pushed `fix-x` to origin\n- Opened PR #12", [(c.PUSH, "fix-x"), (c.PR, "#12")]),
        ("The branch was pushed to `release`.", [(c.PUSH, "release")]),
        ("Saved ~/new.md and removed ~/old.md.", [(c.FILE, "~/new.md"), (c.REMOVED, "~/old.md")]),
    ],
)
def test_the_assistants_own_statement_is_a_claim(text, claimed):
    assert _read(text) == claimed


@pytest.mark.parametrize(
    "text",
    [
        "CI pushed a new build.",
        "Dana merged it yesterday.",
        "The PR was merged by Dana.",
        "Dana sent the summary to the team.",
        "I merged the two CSV files into one table.",
        "I sent a request to the API and got a 200.",
        "Want me to push the branch?",
        "I can open a PR if you like.",
        "I didn't push anything.",
        "Next, push the branch and open a PR.",
        "I released the lock before exiting.",
        "The meeting is scheduled for 3pm.",
        "The handler lives in server/app.py and reads the config.",
        "I read server/app.py and found the bug.",
        "The API docs are available at https://docs.aws.amazon.com/bedrock/.",
        "The endpoint /api/v1/users.json returns the list.",
        "```\ngit push origin main\nSaved to ~/Downloads/a.docx\n```",
    ],
)
def test_a_plan_a_description_or_someone_elses_act_claims_nothing(text):
    assert _read(text) == []


def test_removing_a_line_from_a_file_edits_it():
    assert _read("Removed the stale import from `server/app.py`.") == [(c.FILE, "server/app.py")]


def test_a_path_with_spaces_written_bare_is_not_read_as_two_paths():
    assert _read("Saved it to ~/My Folder/notes/a.py.") == []


def test_a_claim_carries_the_offsets_of_its_clause():
    text = "Here is the summary. I pushed the fix to `main`. Let me know."
    (claim,) = read_claims(text)
    assert text[claim.start : claim.end] == claim.clause
    assert "pushed the fix" in claim.clause and "summary" not in claim.clause


def test_where_a_file_is_is_not_that_it_was_made():
    (made,) = read_claims("I saved the report to ~/Downloads/q3.docx.")
    (there,) = read_claims("The report is at ~/Downloads/q3.docx.")
    assert made.made and not there.made
    (state,) = read_claims("The report is saved at ~/Downloads/q3.docx.")
    assert not state.made


def test_a_claim_that_points_back_is_marked_earlier():
    (claim,) = read_claims("I saved the report to ~/Downloads/q3.docx earlier.")
    assert claim.earlier
    (now,) = read_claims("I saved the report to ~/Downloads/q3.docx.")
    assert not now.earlier


def test_a_list_item_is_read_with_its_lead_line():
    text = "I did the following:\n- pushed `fix-x`\n- emailed the notes to Dana"
    assert _read(text) == [(c.PUSH, "fix-x"), (c.MESSAGE, "Dana")]


def test_a_code_file_in_a_table_of_changes_is_claimed_where_the_row_says_what_was_done():
    changed = "| File | Change |\n| --- | --- |\n| `server/app.py` | updated the retry |"
    listed = "| File | Purpose |\n| --- | --- |\n| `server/app.py` | serves the API |"
    assert _read(changed) == [(c.FILE, "server/app.py")]
    assert _read(listed) == []


@pytest.mark.parametrize(
    "text",
    [
        # A search result's citation points at a source.
        "See [bronze-explained.html:425-451](#wsfile=bronze-explained.html&L=425-451).",
        "It is described in [notes.md:3-9](#wsfile=%2FUsers%2Fme%2Fnotes.md&L=3-9).",
        # A folder named in a plan or an instruction.
        "Build a linked HTML report in `~/gitlab/docs`.",
        "Then save a concise Markdown handoff in `~/gitlab/docs`.",
        # The name of a thing, not an act.
        "- Scheduled Python checks between MySQL sources and Snowflake.",
        "Merged PRs: #12, #13",
        "Generated docs: https://x.dev/docs",
        # Where the work came from.
        "Added verified missing content from `Downloads/dbmodels.md`:",
    ],
)
def test_a_citation_a_planned_folder_or_a_named_thing_claims_nothing(text):
    # Read in real replies: each once came out as a claim.
    assert _read(text) == []


@pytest.mark.parametrize(
    ("text", "claimed"),
    [
        ("Emailed Dana the notes.", [(c.MESSAGE, "Dana")]),
        ("I messaged @ops about it.", [(c.MESSAGE, "@ops")]),
        (
            "Restored all nine files in `/Users/me/docs/images/src/`.",
            [(c.FOLDER, "/Users/me/docs/images/src/")],
        ),
        (
            "Done: [report](#wsfile=~/Downloads/report.docx&open=os) is ready.",
            [(c.FILE, "~/Downloads/report.docx")],
        ),
    ],
)
def test_a_recipient_a_restored_folder_and_a_file_link_are_claims(text, claimed):
    assert _read(text) == claimed
