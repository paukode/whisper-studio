"""What a call did (server/goals/acts.py) and whether it finished
(server/goals/call_results.py), as a claim is checked against it.

A dry run, a draft, an auto-merge still waiting and a push that deletes a
branch did nothing; a preview is not production and a message to an agent is
no email; a runner ("npx", "python -m") runs the program it names; a call that
still waits or runs has not done it yet, until a later call shows it finished.
"""

import time

import pytest

from server.goals import claims as c
from server.goals.acts import acts_of, writes_of
from server.goals.call_results import succeeded, turn_calls
from server.goals.claims import read_claims
from server.goals.evidence import Evidence, check

W = "ws_run_command"


def _turn(*calls, prompt="go"):
    rows = [{"role": "user", "content": prompt}]
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


def _holds(reply: str, *calls, **kw) -> list[bool]:
    ev = Evidence.of(_turn(*calls), started_at=time.time() - 5, **kw)
    return [check(cl, ev).ok for cl in read_claims(reply)]


def _kinds(name: str, given: dict, result: str = "ok") -> list[tuple[str, str]]:
    return [(a.kind, a.target) for a in acts_of(name, given, "", result)]


# ── what a command did ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "aws s3 cp q3.csv s3://acme/q3.csv --dryrun",
        "npm publish --dry-run",
        "kubectl apply --dry-run=client -f deploy.yaml",
        "rsync -avn dist/ host:/srv/site",
        "git push --dry-run origin main",
        "git push --delete origin fix-x",
        "git push origin :fix-x",
        "make -n deploy",
        "helm upgrade api ./chart --dry-run",
    ],
)
def test_a_dry_run_or_a_deletion_does_nothing(command):
    assert _kinds(W, {"command": command}) == []


def test_an_auto_merge_and_a_draft_release_have_done_nothing_yet():
    waiting = "✓ Pull request acme/app#12 will be automatically merged via squash"
    assert _kinds("github", {"args": ["pr", "merge", "12", "--auto"]}, waiting) == []
    assert _kinds("github", {"args": ["release", "create", "v2.1", "--draft"]}) == []
    assert _kinds("github", {"args": ["release", "create", "v2.1"]}) == [(c.PUBLISH, "v2.1")]


def test_a_push_the_remote_already_had_pushed_nothing():
    assert _kinds("git_push", {"branch": "main"}, "Everything up-to-date") == []
    assert _kinds(W, {"command": "git push origin main"}, "Everything up-to-date") == []


@pytest.mark.parametrize(
    "command,kind",
    [
        ("npx vercel deploy --prod", c.PUBLISH),
        ("npx -y wrangler deploy", c.PUBLISH),
        ("python -m twine upload dist/*", c.PUBLISH),
        ("uv publish", c.PUBLISH),
        ("poetry run twine upload dist/*", c.PUBLISH),
        ("make deploy", c.PUBLISH),
        ("npm run release", c.PUBLISH),
        ("bash scripts/deploy.sh prod", c.PUBLISH),
        ("hub pull-request -m Fix", c.PR),
        ("glab mr create --fill", c.PR),
        ("docker push 123.dkr.ecr.us-east-1.amazonaws.com/app:1.2", c.PUSH),
    ],
)
def test_a_runner_or_a_script_named_for_it_does_the_act(command, kind):
    assert kind in [k for k, _t in _kinds(W, {"command": command})]


def test_a_git_push_option_opens_a_merge_request():
    kinds = [k for k, _t in _kinds(W, {"command": "git push origin fix-x -o merge_request.create"})]
    assert kinds == [c.PUSH, c.PR]


def test_a_move_removes_its_source_and_writes_its_destination():
    for command in ("mv src/old.ts src/new.ts", "git mv src/old.ts src/new.ts"):
        assert (c.REMOVED, "src/old.ts") in _kinds(W, {"command": command})
        assert writes_of(W, {"command": command}) == ["src/new.ts"]


def test_git_rm_cached_takes_a_file_out_of_git_only(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secrets.env").write_text("k=v")
    call = (W, {"command": "git rm --cached config/secrets.env"}, "rm 'config/secrets.env'")
    ev = Evidence.of(_turn(call), workspace=str(tmp_path), started_at=time.time() - 5)
    (untracked,) = read_claims("Removed `config/secrets.env` from git.")
    assert check(untracked, ev).ok
    (deleted,) = read_claims("Deleted `config/secrets.env`.")
    assert not check(deleted, ev).ok


def test_a_command_writes_where_it_ran_not_anywhere_by_that_name(tmp_path):
    old = tmp_path / "old" / "report.pdf"
    old.parent.mkdir()
    old.write_text("last week")
    call = (W, {"command": "cd out && pandoc notes.md -o report.pdf"}, "(exit code 0)")
    assert writes_of(*call[:2]) == ["out/report.pdf"]
    ev = Evidence.of(_turn(call), workspace=str(tmp_path / "ws"), started_at=time.time() + 60)
    (claim,) = read_claims(f"Saved the report to {old}.")
    assert not check(claim, ev).ok


def test_a_heredoc_body_is_data_not_commands():
    command = "cat > notes.md <<'EOF'\nthen run git push origin main\nEOF"
    assert _kinds(W, {"command": command}) == []


def test_a_webhook_post_is_a_chat_message():
    command = 'curl -X POST --data \'{"text":"done"}\' https://hooks.slack.com/services/T/B/X'
    assert _holds("Posted the update to #ops.", (W, {"command": command}, "ok")) == [True]
    assert _holds("Emailed the update to dana@acme.com.", (W, {"command": command}, "ok")) == [
        False
    ]


# ── where it went ────────────────────────────────────────────────────────


def test_a_preview_deploy_is_not_production():
    preview = (W, {"command": "vercel deploy"}, "Preview: https://acme-git-fix-acme.vercel.app")
    assert _holds("Deployed the app to production.", preview) == [False]
    assert _holds("Deployed a preview of the app.", preview) == [True]
    staging = (W, {"command": "fly deploy --app acme-staging"}, "v12 deployed successfully")
    assert _holds("Deployed the app to production.", staging) == [False]
    prod = (W, {"command": "vercel deploy --prod"}, "Production: https://acme.vercel.app")
    assert _holds("Deployed the app to production.", prod) == [True]


def test_a_site_deploy_publishes_no_package_and_testpypi_is_not_pypi():
    preview = (W, {"command": "vercel deploy --prod"}, "Production: https://acme.vercel.app")
    assert _holds("Published the package to npm.", preview) == [False]
    test_pypi = (W, {"command": "twine upload --repository testpypi dist/*"}, "View at ...")
    assert _holds("Published the package to PyPI.", test_pypi) == [False]


def test_a_message_to_an_agent_or_a_pr_comment_is_no_email():
    agent = ("send_message", {"to_agent_id": "a1", "content": "go"}, '{"sent": true}')
    assert _holds("Emailed the report to the client.", agent) == [False]
    assert _holds("I sent the summary to the team on Slack.", agent) == [False]
    comment = ("github", {"args": ["pr", "comment", "12", "--body", "LGTM"]}, "https://x/pull/12")
    assert _holds("Emailed the release notes to the stakeholders.", comment) == [False]
    assert _holds("Commented on PR #12 with the review.", comment) == [True]


def test_a_pull_request_is_the_one_the_result_names():
    made = (
        "git_push_pr",
        {"title": "Fix crash from #12", "body": "Closes #12"},
        "Pushed to origin/fix-crash and created PR:\n\nhttps://github.com/acme/app/pull/45",
    )
    assert _holds("Opened PR #12 to fix the crash.", made) == [False]
    assert _holds("Opened PR #45 to fix the crash.", made) == [True]


def test_an_mcp_merge_and_post_name_their_targets():
    merge = ("mcp__github__merge_pull_request", {"owner": "a", "repo": "b", "pullNumber": 12}, "{}")
    assert _holds("Merged PR #12.", merge) == [True]
    assert _holds("Merged PR #13.", merge) == [False]
    post = (
        "mcp__slack__slack_post_message",
        {"channel_id": "C07ABCD12", "text": "Deploy done"},
        '{"ok": true, "channel": "C07ABCD12", "ts": "1727.1"}',
    )
    assert _holds("Posted the update in #general.", post) == [True]


def test_an_image_push_is_no_git_push_and_names_its_registry():
    image = (W, {"command": "docker push 123.dkr.ecr.us-east-1.amazonaws.com/app:1.2"}, "digest")
    assert _holds("Pushed the image to ECR.", image) == [True]
    assert _holds("Pushed the fix to `main`.", image) == [False]
    assert _holds("Pushed the image to Docker Hub.", image) == [False]


# ── whether it finished ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,result",
    [
        ("terminal_send", "wait_reason: stdin_read\nwaiting for input\n---\nUsername:"),
        ("terminal_send", "wait_reason: timeout\nstill printing\n---\nWriting objects: 40%"),
        ("terminal_run", "[User approved] ok.\n\nTIMED OUT after 120s.\nexit_code: None\n---\n"),
        (
            "ws_run_command",
            "[User approved] " + "x" * 300 + "\n\n[Background Task Started] task_id=t1",
        ),
        ("send_message", "Agent a1 is not running; message not delivered"),
        ("mcp__gmail__send_email", "Warning: 1 recipient rejected: bob@x.com"),
        ("mcp__github__create_pull_request", '{"message": "Not Found", "documentation_url": "x"}'),
        ("github_api_write", '{"message": "Resource not accessible", "status": "403"}'),
        ("github_api_write", "HTTP 422: Validation Failed"),
        ("ws_run_command", "npm ERR! code E403\nnpm ERR! 403 Forbidden - PUT https://registry"),
        ("ws_run_command", "unauthorized: authentication required"),
    ],
)
def test_a_call_that_waits_runs_on_or_failed_in_words_did_not_do_it(name, result):
    assert not succeeded(name, result)


def test_a_background_command_counts_once_a_later_call_shows_it_finished():
    started = (
        W,
        {"command": "vercel deploy --prod"},
        "[Background Task Started] task_id=t1\nCommand: vercel deploy --prod",
    )
    assert _holds("Deployed the app to production.", started) == [False]
    ev = Evidence.of(_turn(started), started_at=time.time() - 5)
    (claim,) = read_claims("Deployed the app to production.")
    verdict = check(claim, ev)
    assert verdict.pending and "still running" in verdict.note
    done = (
        "task_status",
        {"task_id": "t1"},
        '{"task_id": "t1", "status": "completed", "exit_code": 0}',
    )
    assert _holds("Deployed the app to production.", started, done) == [True]
    output = ("task_output", {"task_id": "t1"}, "[shell task t1 \N{EM DASH} failed, exit 1]\nError")
    assert _holds("Deployed the app to production.", started, output) == [False]
    (call,) = [x for x in turn_calls(_turn(started, output)) if x.name == W]
    assert not call.ok and not call.pending


def test_a_claim_that_names_no_channel_is_not_met_by_an_agent_message():
    agent = ("send_message", {"to_agent_id": "a1", "content": "summary"}, '{"sent": true}')
    assert _holds("Sent the summary to Dana.", agent) == [False]
    assert _holds("Sent the summary.", agent) == [False]
    assert _holds("Sent the summary to the reviewer agent.", agent) == [True]


def test_a_package_claim_is_not_met_by_a_site_deploy():
    site = (W, {"command": "vercel deploy --prod"}, "Production: https://acme.vercel.app")
    assert _holds("Published the package.", site) == [False]


def test_a_git_push_claim_is_not_met_by_an_image_push():
    image = (W, {"command": "docker push acme/app:1.2"}, "1.2: digest: sha256:ab")
    assert _holds("Pushed the fix.", image) == [False]


def test_a_pull_request_body_names_no_pull_request_this_call_opened():
    made = (
        "git_push_pr",
        {"title": "Fix", "body": "Follow-up to https://github.com/acme/app/pull/12"},
        "Pushed to origin/fix and created PR",
    )
    assert _holds("Opened PR #12.", made) == [False]
