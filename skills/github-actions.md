---
name: github_actions
description: Guides the user through installing Claude into a GitHub repository's Actions CI step by step. Checks the gh CLI and its auth scopes, detects the repository from the workspace git remote, asks which repo and auth method to use (Anthropic API key or Claude OAuth token), stores the secret via gh, writes claude.yml and optionally claude-review.yml workflows pinned to anthropics/claude-code-action@v1, pushes them, and finishes with the Claude GitHub App install link. Requires a connected workspace with the gh CLI available. Use when the user wants @claude responding in issues and PRs or automated PR reviews in GitHub Actions. Do not use for debugging failing CI runs or authoring unrelated workflows; handle those directly with ws_run_command and gh.
triggers: github actions, claude github app, github app, actions setup, ci setup, workflow setup, pr review automation, ci/cd
input_schema:
  repo:
    type: string
    required: false
    description: Target repository as owner/repo. Optional; when omitted the skill detects it from the workspace git remote and confirms with the user.
---

Set up Claude in GitHub Actions by following these steps with tools. Reuse prompts
exactly as shown. If a valid `owner/repo` was passed in `repo`, skip the Step 2
question and use it as `REPO` after confirming it exists (Step 3 doubles as that
check).

## Step 1: Check prerequisites

Run these checks sequentially:

1. Check `gh` CLI: `ws_run_command("gh --version")`
   - If it fails: `notify_user` with status=error: "GitHub CLI (gh) is not installed. Install it from https://cli.github.com/ then try again.\n\n- macOS: `brew install gh`\n- Windows: `winget install --id GitHub.cli`\n- Linux: See https://github.com/cli/cli#installation"
   - Stop after notifying.

2. Check auth: `ws_run_command("gh auth status -a")`
   - If it fails: `notify_user` with status=warning: "GitHub CLI is not authenticated. Run `gh auth login` and try again." Stop after notifying.
   - If output is missing `repo` or `workflow` scopes: `notify_user` with status=error: "GitHub CLI is missing required permissions.\n\nRun:\n```\ngh auth refresh -h github.com -s repo,workflow\n```\nThis adds the 'repo' and 'workflow' scopes needed to manage Actions and secrets." Stop after notifying.

3. Detect current repo: `ws_run_command("git remote get-url origin")`
   - Parse `owner/repo` from the output. Handle both forms: strip a
     `https://github.com/` prefix or a `git@github.com:` prefix, and the `.git` suffix.

## Step 2: Ask for repo

Call `ask_user_question`:
- question: "Which GitHub repository should I set up? (format: owner/repo)"
- options: [the detected repo from step 1 (if found), "Other (please specify)"]

Wait for the answer. Use it as `REPO`.

## Step 3: Check existing workflow

Run: `ws_run_command("gh api repos/{REPO}/contents/.github/workflows/claude.yml --jq .sha")`

- If it succeeds (exit code 0): the workflow already exists.
  - Call `ask_user_question`:
    - question: "A Claude workflow already exists in this repo. What should I do?"
    - options: ["Update it (overwrite with latest template)", "Skip workflow (just set up the secret)", "Cancel"]
  - If Cancel: `notify_user` "Setup cancelled." and stop.

## Step 4: Auth method

Call `ask_user_question`:
- question: "How should Claude authenticate in GitHub Actions?"
- options: ["Use my Anthropic API key (ANTHROPIC_API_KEY secret)", "Use OAuth token (CLAUDE_CODE_OAUTH_TOKEN secret)"]

Set `SECRET_NAME` accordingly: API key uses `ANTHROPIC_API_KEY`, OAuth uses
`CLAUDE_CODE_OAUTH_TOKEN`.

## Step 5: Check existing secret

Run: `ws_run_command("gh secret list --app actions --repo {REPO}")`

If `{SECRET_NAME}` already exists:
- Call `ask_user_question`:
  - question: "Secret `{SECRET_NAME}` already exists in this repo. Use it or replace it?"
  - options: ["Use existing secret (skip)", "Replace with a new value"]

If using the existing secret: skip to Step 7.

## Step 6: Collect and store the secret value

Never echo the secret back, never embed it inside a shell command line, and never
write it into any other file: a quote inside the value would break shell quoting, and
command lines leak into the process list.

Call `ask_user_question`:
- question: "How do you want to set the {SECRET_NAME} secret?"
- options:
  - "I'll set it myself in my terminal (most secure)"
  - "I'll paste it here"

**If they set it themselves**: `notify_user` with the exact command to run in their own
terminal, then continue to Step 7 once they confirm:
```
gh secret set {SECRET_NAME} --app actions --repo {REPO}
```
(gh prompts for the value on stdin; nothing is stored in this chat.)
For OAuth, first run `claude setup-token` to obtain the token.

**If they paste it**: collect it with `ask_user_question` ("Paste the value:",
options: ["Other (please specify)"]). Then store it via stdin, never inline:
1. `ws_create_file` a temp file `.claude_secret_tmp` containing only the value
2. `ws_run_command("gh secret set {SECRET_NAME} --app actions --repo {REPO} < .claude_secret_tmp")`
3. `ws_delete_file` the temp file immediately, regardless of success

If the gh command fails: `notify_user` status=error with the error message and stop
(after deleting the temp file).

## Step 7: Create workflow file(s)

If not skipping the workflow, ask which to install:
- Call `ask_user_question`:
  - question: "Which workflows should I install?"
  - options: ["Both: claude.yml (general) + claude-review.yml (PR reviews)", "Just claude.yml (general)", "Just claude-review.yml (PR reviews)"]

Create the selected file(s) under `.github/workflows/` with `ws_create_file`.

### claude.yml content:
```yaml
name: Claude

on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]
  issues:
    types: [opened, assigned]
  pull_request_review:
    types: [submitted]

jobs:
  claude:
    if: |
      (github.event_name == 'issue_comment' && contains(github.event.comment.body, '@claude')) ||
      (github.event_name == 'pull_request_review_comment' && contains(github.event.comment.body, '@claude')) ||
      (github.event_name == 'pull_request_review' && contains(github.event.review.body, '@claude')) ||
      (github.event_name == 'issues' && (contains(github.event.issue.body, '@claude') || contains(github.event.issue.title, '@claude')))
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
      issues: write
      id-token: write
      actions: read # Required for Claude to read CI results on PRs
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
        with:
          fetch-depth: 1

      - name: Run Claude
        id: claude
        uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

### claude-review.yml content:
```yaml
name: Claude PR Review

on:
  pull_request:
    types: [opened, synchronize, ready_for_review, reopened]

jobs:
  claude-review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      id-token: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
        with:
          fetch-depth: 1

      - name: Claude PR Review
        uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt: |
            Review this pull request. Provide concise, actionable feedback on:
            - Code correctness and potential bugs
            - Security issues
            - Performance concerns
            - Code style consistency

            Be constructive and specific. Skip trivial nit-picks.
```

If OAuth was chosen in Step 4, change the `with:` line in each workflow to
`claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}` (both the input name
and the secret name change; do not pass an OAuth token as anthropic_api_key).

## Step 8: Push the workflow to GitHub

The workflow does not exist on GitHub until it is committed and pushed.

- Run `ws_run_command("git remote get-url origin")` again and confirm it matches
  `REPO`.
  - If it matches: ask the user to confirm, then commit and push:
    `ws_run_command("git add .github/workflows && git commit -m 'Add Claude GitHub Actions workflow' && git push")`
  - If it does not match: the local workspace is a different repo. Do not commit here.
    Create the file(s) directly on GitHub instead, e.g.:
    `ws_run_command("gh api -X PUT repos/{REPO}/contents/.github/workflows/claude.yml -f message='Add Claude workflow' -f content=$(base64 < .github/workflows/claude.yml | tr -d '\\n')")`
    or tell the user to add the file to that repo manually, and include it in the
    final summary.

## Step 9: Install GitHub App and summarize

`notify_user` status=success:
"Almost done! One last step: install the Claude GitHub App on your repository.

1. Go to: https://github.com/apps/claude
2. Click **Install** and select `{REPO}`
3. Grant access to the repository

Once installed, mention **@claude** in any issue or PR comment to trigger it."

Then a final summary listing exactly what happened: secret configured (or reused),
workflow file(s) created, whether they were pushed (and if not, what the user still
needs to do), and the app install link.
