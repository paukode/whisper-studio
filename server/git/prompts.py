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
    where it loads with the tool instead of on every turn. Only three things earn
    a place here: intent disambiguation where guessing wrong is not reversible,
    the shape of output the user expects, and constraints that span tools so no
    single description can hold them.

    What that rule removed, and why it is safe to have removed it:
    - "How to clone" was a near-verbatim copy of git_clone's own description.
    - The safety protocol (never update config, never skip hooks, never --amend,
      never -i, warn on secret files) guarded against flags these tools do not
      expose, and git_add_commit already says it never amends and rejects secret
      files. The one real hole was the shell, which the shell rule below covers.
    - The GitHub bullets restated the github / github_api / github_api_write
      descriptions, which already carry the argv shape, the read-vs-mutate split,
      the hard-blocked actions, and the "never claim a state a tool did not
      confirm" rule.
    - Step-by-step commit and branch-deletion procedures were multi-step
      completion the model does unprompted, and git_delete_branch's own
      description carries the one real ordering constraint (be on another branch).
    """
    return """# Commits

Match the repository's existing style — read recent messages with git_log before
writing one. Say why the change was made rather than restating the diff, and
stage the files you actually touched rather than everything.

# Pull requests

Read the request literally here, because pushing cannot be undone from this side:
- "write a PR message", "draft a PR", "write PR description" → produce the text
  only. Do not push. Do not open a PR.
- "commit and write a PR message" → commit, then produce the text only.
- "create a PR", "open a PR", "submit a PR", "push and create PR" → push the
  branch and open it.
Never push or open a PR the user did not ask for. When drafting, read every
commit on the branch rather than only the newest. Branch first if the current
branch is the default one.

Title: short, under 70 chars
Body:
## Summary
<1-3 bullet points>

## Files changed
<bullet points on files created/deleted/modified>

## Test plan
<bulleted checklist>

# Constraints that span tools

Whisper's dev server watches the workspace tree, so any git operation that
rewrites files across it can trigger a reload that ends the chat session. The
tools whose descriptions carry that warning (git_checkout, git_merge, git_stash
pop/apply) must be confirmed with the user before you call them.

Do git and GitHub work through the git_* and github tools, never by shelling out
through ws_run_command, terminal_run, or any other command tool. The shell path
skips the approval cards, skips the verification the github tools perform, and is
not authenticated for GitHub — which is how it has produced confident but false
"I closed the PR" claims. That includes `git worktree add`, checkout, merge,
rebase, and `reset --hard`.
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
