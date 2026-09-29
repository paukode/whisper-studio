"""How replies say they delivered, and what only sounds like it
(server/goals/claims.py and its claim_text, claim_paths, claim_acts).

Pinned from reviews of the reader: each phrasing a model uses for its own
act is read, with ``made`` False where it says what now is; descriptions of
someone else's work, code, quoted text and drafts claim nothing.
"""

import pytest

from server.goals import claims as c
from server.goals.claims import read_claims


def _read(text: str) -> list[tuple[str, str]]:
    return [(x.kind, x.target) for x in read_claims(text)]


@pytest.mark.parametrize(
    "text,kind,target",
    [
        # A verb joined to a first-person or agentless clause shares it.
        ("I fixed the second bug and pushed the change to `main`.", c.PUSH, "main"),
        ("I ran the tests and pushed the branch.", c.PUSH, ""),
        ("I fixed the typo and merged PR #12.", c.MERGE, "#12"),
        ("I wrote the summary and emailed it to dana@acme.com.", c.MESSAGE, "dana@acme.com"),
        ("I built the site and deployed it to production.", c.PUBLISH, ""),
        ("Then I squashed the commits and force-pushed `fix-x`.", c.PUSH, "fix-x"),
        ("Tagged v2.3.0 and pushed the tag.", c.PUSH, ""),
        # Bare objects after a verb with no subject.
        ("Pushed fix-x to origin.", c.PUSH, "fix-x"),
        ("Pushed feature/login to origin.", c.PUSH, "feature/login"),
        ("Pushed main.", c.PUSH, "main"),
        ("Merged fix-x into main.", c.MERGE, "fix-x"),
        ("Merged it into master.", c.MERGE, ""),
        ("Sent Dana the notes.", c.MESSAGE, "Dana"),
        ("Emailed dana@acme.com the notes.", c.MESSAGE, "dana@acme.com"),
        ("Pushed \N{WHITE HEAVY CHECK MARK}", c.PUSH, ""),
        # A dot inside a version or an address does not end the clause.
        ("Deployed v2.3 to production.", c.PUBLISH, "v2.3"),
        ("Synced ./dist to s3://acme-site.", c.UPLOAD, "s3://acme-site"),
        # Passives of the assistant's own act.
        ("The email has been sent to dana@acme.com.", c.MESSAGE, "dana@acme.com"),
        (
            "The PR has been opened: https://github.com/acme/app/pull/7",
            c.PR,
            "https://github.com/acme/app/pull/7",
        ),
        ("Branch `fix-x` has been pushed.", c.PUSH, "fix-x"),
        ("PR #12 has been merged into main.", c.MERGE, "#12"),
        # Headlines naming what was acted on.
        ("PR #12 merged.", c.MERGE, "#12"),
        ("Squash-merged PR #12.", c.MERGE, "#12"),
        # Other ways of saying it.
        (
            "I've opened https://github.com/acme/app/pull/7 for review.",
            c.PR,
            "https://github.com/acme/app/pull/7",
        ),
        ("Opened #7.", c.PR, "#7"),
        (
            "Filed https://github.com/acme/app/issues/40.",
            c.ISSUE,
            "https://github.com/acme/app/issues/40",
        ),
        ("Commented on PR #12 with the review.", c.MESSAGE, "#12"),
        ("I've let Dana know.", c.MESSAGE, "Dana"),
        ("I've created an artifact with the dashboard.", c.ARTIFACT, ""),
        ("Saved that to memory.", c.MEMORY, ""),
        ("Turned on dark mode.", c.SETTING, ""),
        ("Status: pushed to origin/fix-x", c.PUSH, "origin/fix-x"),
        ("Closed issue #10 and merged PR #12.", c.MERGE, "#12"),
    ],
)
def test_a_claim_is_read_however_the_reply_puts_it(text, kind, target):
    assert (kind, target) in _read(text), _read(text)


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Here's the PR: https://github.com/acme/app/pull/7", c.PR),
        ("Pull request: #7", c.PR),
        ("PR #12 is merged.", c.MERGE),
        ("Your changes are pushed.", c.PUSH),
        ("The fix is now on `main`.", c.PUSH),
        ("Changes are on origin/main now.", c.PUSH),
        ("The site is live at https://acme.vercel.app.", c.PUBLISH),
        ("Your reminder is scheduled for 9am tomorrow.", c.SCHEDULE),
        ("Dark mode is now on.", c.SETTING),
        ("The file is in the bucket now: s3://acme/q3.csv", c.UPLOAD),
        ("The commit is `a1b2c3d`.", c.COMMIT),
        ("`server/old_helper.py` is gone now.", c.REMOVED),
    ],
)
def test_what_now_is_is_read_as_a_claim_that_was_not_made_now(text, kind):
    found = [x for x in read_claims(text) if x.kind == kind]
    assert found and not any(x.made for x in found), read_claims(text)


@pytest.mark.parametrize(
    "text",
    [
        # Someone else did it.
        "The PR updated `server/app.py` to split the loader.",
        "Commit a1b2c3d changed `server/app.py` to retry on 503.",
        "Upstream changed `src/api/client.ts` in v2.0.",
        "Someone deleted `docs/setup.md` in the last merge.",
        "The script created `out/report.csv` the last time it ran.",
        "The file was saved to `out/report.csv` by the cron job.",
        "Your branch pushed 3 commits to `main` yesterday.",
        "The CI job pushed the image to ECR.",
        # A dated description.
        "The commit pushed to `main` at 10:02 broke the login page.",
        # What happens when something else does.
        "When CI passes, the image is pushed to ECR and deployed to staging.",
        "After merge, the docs site is published to GitHub Pages.",
        "On failure, an alert is sent to #oncall.",
        "If the deploy fails, an email is sent to sre@acme.com.",
        # Code.
        "I scheduled the cleanup task with `asyncio.create_task` so it runs off the request path.",
        "I set the retry setting to 3 in `server/config.py`.",
        "I updated the notification preferences handler to skip muted users.",
        "I kept the parsed index in memory to avoid a second read.",
        "I merged the two helpers into `main.py`.",
        # Not an act.
        "Let me know.",
        "The meeting is scheduled for 3pm.",
    ],
)
def test_a_description_of_other_work_claims_nothing_of_the_assistants(text):
    assert not [x for x in read_claims(text) if x.firm], read_claims(text)


def test_a_lead_line_about_someone_elses_work_makes_its_items_descriptions():
    items = read_claims("Summary of your changes:\n- Updated `server/app.py` to retry on 503")
    assert items and not any(x.firm for x in items)


def test_a_first_person_lead_makes_its_items_claims():
    for text in (
        "I updated these files:\n\n- `server/app.py`: added retry\n- `tests/test_app.py`: added tests",
        "I updated these files:\n\n| File | Change |\n| --- | --- |\n| `server/app.py` | retry |",
    ):
        assert (c.FILE, "server/app.py") in _read(text), text
        assert all(x.firm and x.made for x in read_claims(text)), text


def test_a_path_on_its_own_line_under_a_lead_is_an_item():
    assert _read("Saved the report to:\n`/Users/me/q3.docx`") == [(c.FILE, "/Users/me/q3.docx")]


def test_a_blank_line_between_a_lead_and_its_list_keeps_the_lead():
    text = "I saved these files:\n\n- `/Users/nobody/out/a.md`\n- `/Users/nobody/out/b.md`\n"
    assert _read(text) == [(c.FILE, "/Users/nobody/out/a.md"), (c.FILE, "/Users/nobody/out/b.md")]


@pytest.mark.parametrize(
    "text",
    [
        "Here's a draft you can send:\n\n> Hi team,\n> I've also merged PR #12 into main.",
        "Suggested commit message:\n\nPushed the retry helper to the loader and deployed it.",
        "Here's your standup update:\n\n- Yesterday: merged PR #41 into main",
        "Steps:\n1. ```bash\n   git push origin main\n   ```",
    ],
)
def test_quoted_text_a_draft_and_code_are_not_the_assistants_claims(text):
    assert read_claims(text) == []


def test_a_draft_ends_where_its_block_or_its_paragraph_does():
    example = "Example:\n````md\n```\ncode\n```\n````\nI pushed the fix to `main`.\n"
    assert _read(example) == [(c.PUSH, "main")]
    draft = (
        "Here's a draft you can send:\n\n> Hi team, the fix is out.\n\nI pushed the fix to `main`."
    )
    assert _read(draft) == [(c.PUSH, "main")]


@pytest.mark.parametrize(
    "text",
    [
        "```npm test``` runs the suite.\nI pushed the fix to `main`.",
        "Steps:\n- ```bash\n  git status\n  ```\nI pushed the fix to `main`.",
        "Run:\n```bash\necho one\n\necho two\n```\nI pushed the fix to `main`.",
    ],
)
def test_a_claim_after_code_is_read(text):
    assert (c.PUSH, "main") in _read(text)


def test_a_title_or_an_abbreviation_ends_no_sentence():
    (claim,) = read_claims("I emailed the report to Dr. Smith this morning.")
    assert claim.target == "Smith" and "this morning" in claim.clause
    assert [x.kind for x in read_claims("I pushed to `main`, etc. and more.")] == [c.PUSH]


def test_only_the_verb_a_path_has_is_its_own():
    text = "In this diff, `server/app.py` was updated and `docs/setup.md` was removed."
    assert [(x.kind, x.target) for x in read_claims(text)] == [
        (c.FILE, "server/app.py"),
        (c.REMOVED, "docs/setup.md"),
    ]


def test_a_quoted_line_outside_a_draft_is_not_the_assistants():
    assert read_claims("The user wrote:\n> I've merged PR #12 into main.") == []


def test_a_headline_noun_that_did_the_act_is_not_the_assistant():
    for text in ("Your branch pushed 3 commits to `main`.", "The PR merged the fix into main."):
        assert not [x for x in read_claims(text) if x.firm], text


def test_an_act_listed_under_someone_elses_work_is_a_description():
    acts = read_claims("Summary of your changes:\n- Pushed the fix to `main`")
    assert acts and not any(x.firm for x in acts)
