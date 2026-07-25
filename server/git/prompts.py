"""
Git prompts — inject git context into system prompts.

Generates git status blocks for system prompts and specialized
context for commit and PR workflows.
"""

from server.git.core import (
    _run_git,
    find_git_root,
    get_branch,
    get_default_branch,
    get_file_status,
    get_is_clean,
    get_remote_url,
)


def build_git_status_prompt(workspace_path: str) -> str:
    """Generate git context block for system prompt.

    Includes current branch, clean/dirty status, and recent commits.
    Returns empty string if not a git repo.
    """
    git_root = find_git_root(workspace_path)
    if not git_root:
        return ""

    branch = get_branch(workspace_path)
    default = get_default_branch(workspace_path)
    clean = get_is_clean(workspace_path)

    lines = [
        "# Git Status",
        f"Current branch: {branch}",
        f"Main branch (for PRs): {default}",
    ]

    if clean:
        lines.append("Status: (clean)")
    else:
        status = get_file_status(workspace_path)
        tracked = len(status["tracked"])
        untracked = len(status["untracked"])
        parts = []
        if tracked:
            parts.append(f"{tracked} files modified")
        if untracked:
            parts.append(f"{untracked} untracked")
        lines.append(f"Status: {', '.join(parts)}")

    # Recent commits
    try:
        result = _run_git(["log", "-5", "--oneline"], cwd=git_root, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            lines.append("Recent commits:")
            for commit_line in result.stdout.strip().split("\n"):
                lines.append(f"  {commit_line}")
    except Exception:
        pass

    return "\n".join(lines) + "\n"


def build_git_instructions_prompt() -> str:
    """Static git workflow instructions for the system prompt.

    Always included when git tools are in the tool pool so the LLM knows the
    expected commit message style, PR body format, and safety rules. Static —
    no dynamic git state — so it is registered on the cached WORKSPACE layer,
    NOT alongside build_git_status_prompt on the dynamic layer.

    Scope rule: per-tool usage guidance belongs in that tool's ``description``,
    where it loads with the tool instead of on every turn. Only cross-tool
    workflow (which tool to reach for, message/PR shape, repo-wide safety) earns
    a place here. "How to clone" lived here as a near-verbatim copy of
    git_clone's own description; the branch-swap worktree requirement lived here
    while git_checkout / git_merge / git_stash said nothing about it.
    """
    return """# Git Commit Instructions

When creating a git commit using git_add_commit:

1. First use git_status and git_diff to review all changes.
2. Use git_log to see recent commit messages and follow the repository's style.
3. Draft a concise commit message that focuses on the "why" rather than the "what".
4. Summarize the nature of the changes (new feature, enhancement, bug fix, refactor, test, docs).
5. Prefer staging specific files over staging all changes.
6. NEVER commit files that likely contain secrets (.env, credentials.json, etc.).

# Branch Deletion Rules
- "delete all other branches", "clean up local branches", "delete other branches" → use git_branch_list to list all local branches, then call git_delete_branch for every branch except the current one. Complete ALL deletions: do not stop after switching branches.
- Always switch to the target branch first, then delete the others one by one.
- Use force=true only when the user explicitly says "force delete" or "delete unmerged".

# Git Safety Protocol
- NEVER update git config
- NEVER skip hooks (--no-verify, --no-gpg-sign)
- ALWAYS create NEW commits (never --amend unless explicitly requested)
- Warn on secret files (.env, credentials.json, credentials, .aws, .ssh, etc.)
- Never use -i (interactive) flag

# Working Tree Safety

Whisper's dev server watches the workspace tree, so any git operation that
rewrites files across it can trigger a reload that ends the chat session. The
tools whose descriptions carry this warning (git_checkout, git_merge, git_stash
pop/apply) must be confirmed with the user before you call them. Never reach for
`git worktree add`, checkout, merge, rebase, or `reset --hard` through
ws_run_command or any shell: those bypass both the warning and the approval card.

# Pull Request Rules

## When to push / create a PR
- "write a PR message", "draft a PR", "write PR description" → ONLY write the message as text. Do NOT push or create the PR.
- "create a PR", "open a PR", "submit a PR", "push and create PR" → push to remote and create the PR.
- "commit and write a PR message" → commit the changes, then write the PR message as text only. Do NOT push or create the PR.
- Never push to remote or create a PR unless the user explicitly asks to create/open/submit one.

## GitHub beyond creating a PR (issues, PRs, runs, releases, API)
- Use the `github` tool for any GitHub CLI subcommand: pass gh argv without the leading "gh", e.g. `["pr","close","2"]`, `["issue","list"]`, `["run","view","123"]`. Reads run immediately; mutations show the exact command for approval and are verified.
- Use `github_api` (read) / `github_api_write` (mutate) for raw REST/GraphQL that no gh verb covers.
- To CLOSE a PR without merging, call `github ["pr","close",<number>]`. To check a PR's state, `github ["pr","view",<number>]` or `github_api`.
- NEVER manage GitHub by shelling out to `gh`/`git`/`curl` through a command tool — that path is not authenticated and fails silently, which has caused false "closed the PR" claims. Only the github / github_api / github_api_write tools reach the authenticated GitHub path.
- Some actions are NEVER permitted from the assistant, no matter what the user says — deleting or transferring a repository, changing repository visibility (private/public), and adding or removing repository collaborators or teams. The tools hard-block these; if asked, explain that the user must do it themselves on GitHub. Do not try to work around the block.
- NEVER tell the user a PR or issue is closed, merged, or open unless a github/github_api result THIS TURN confirmed that exact state. If a tool reports it could not complete or verify a mutation, say so plainly — do not claim success.

## PR message format (when drafting or creating)

Title: short, under 70 chars
Body:
## Summary
<1-3 bullet points>

## Files changed
<bullet points on files created/deleted/modified>

## Test plan
<bulleted checklist>

Important:
- Always analyze ALL commits on the branch (not just the latest) when drafting the PR description.
- If on the default branch, create a new branch first.
"""


def build_commit_prompt(workspace_path: str) -> str:
    """Generate full context for commit operations.

    Includes git status, diff HEAD, branch name, recent 10 commits,
    and safety protocol rules.
    """
    git_root = find_git_root(workspace_path)
    if not git_root:
        return ""

    parts = []

    # Status
    try:
        result = _run_git(["status"], cwd=git_root, timeout=5)
        if result.returncode == 0:
            parts.append(f"# Git Status\n{result.stdout.strip()}")
    except Exception:
        pass

    # Diff HEAD (truncated)
    try:
        result = _run_git(["--no-optional-locks", "diff", "HEAD"], cwd=git_root, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            diff = result.stdout
            if len(diff) > 20000:
                diff = diff[:20000] + "\n\n... (diff truncated)"
            parts.append(f"# Git Diff HEAD\n{diff.strip()}")
    except Exception:
        pass

    # Branch
    branch = get_branch(workspace_path)
    parts.append(f"Current branch: {branch}")

    # Recent commits
    try:
        result = _run_git(["log", "-10", "--oneline"], cwd=git_root, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            parts.append(f"# Recent Commits\n{result.stdout.strip()}")
    except Exception:
        pass

    # Safety protocol
    parts.append("""# Git Safety Protocol
- NEVER update git config
- NEVER skip hooks (--no-verify, --no-gpg-sign)
- ALWAYS create NEW commits (never --amend unless explicitly requested)
- Warn on secret files (.env, credentials.json, credentials, .aws, .ssh, etc.)
- Never use -i (interactive) flag
- Use HEREDOC syntax for multi-line commit messages

# Commit Message Writing Style
- Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen.
- Never use dashes to create lines or separators in the commit message.""")

    return "\n\n".join(parts) + "\n"


def build_pr_prompt(workspace_path: str) -> str:
    """Generate full context for PR creation.

    Extends commit prompt with default branch, remote URL, and PR template.
    """
    base = build_commit_prompt(workspace_path)
    if not base:
        return ""

    default = get_default_branch(workspace_path)
    remote = get_remote_url(workspace_path)

    extra = [
        f"Default branch: {default}",
        f"Remote URL: {remote or 'unknown'}",
        """# PR Template
Title: short, under 70 chars
Body:
## Summary
<1-3 bullet points>

## Files changed
<bullet points on file created/deleted/modified>

## Test plan
<bulleted checklist>

# PR Writing Style
- Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen.""",
    ]

    return base + "\n" + "\n\n".join(extra) + "\n"
