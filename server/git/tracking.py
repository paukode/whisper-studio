"""
Git operation tracking — detect and track git operations from command output.

Parses git/gh command + stdout to detect commits, pushes, cherry-picks,
merges, rebases, and PR operations. Logs detected operations per session.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DetectedCommit:
    sha: str
    kind: str  # "committed", "amended", "cherry-picked"


@dataclass
class DetectedPush:
    branch: str


@dataclass
class DetectedPR:
    number: int
    url: str | None
    action: str  # "created", "edited", "merged", "commented", "closed", "ready"


@dataclass
class GitOperationResult:
    commit: DetectedCommit | None = None
    push: DetectedPush | None = None
    pr: DetectedPR | None = None


# --- Detection patterns ---

# Commit SHA: [branch abc1234] or [branch (root-commit) abc1234]
_COMMIT_SHA_PATTERN = re.compile(r"\[[\w./-]+(?:\s+\(root-commit\))?\s+([0-9a-f]+)\]")

# Push branch: lines like " * [new branch]  main -> origin/main"
# or "   abc1234..def5678  main -> origin/main"
_PUSH_BRANCH_PATTERN = re.compile(
    r"^\s*[+\-*!= ]?\s*(?:\[new branch\]|\S+\.\.+\S+)\s+\S+\s*->\s*(\S+)",
    re.MULTILINE,
)

# PR number: "Pull request #42" or "pull request owner/repo#42"
_PR_NUMBER_PATTERN = re.compile(r"[Pp]ull request (?:\S+#)?#?(\d+)")

# PR URL: https://github.com/owner/repo/pull/123
_PR_URL_PATTERN = re.compile(r"(https://github\.com/[\w.-]+/[\w.-]+/pull/\d+)")

# gh pr subcommands
_GH_PR_ACTIONS = {
    "create": "created",
    "edit": "edited",
    "merge": "merged",
    "comment": "commented",
    "close": "closed",
    "ready": "ready",
}

# glab mr create
_GLAB_MR_PATTERN = re.compile(r"glab\s+mr\s+create")


# --- Session operation log ---

_session_operations: dict[str, list[GitOperationResult]] = {}
_session_pr_links: dict[str, list[str]] = {}


def detect_git_operation(command: str, output: str) -> GitOperationResult:
    """Parse command + stdout/stderr to detect git operations.

    Works for both bash and PowerShell git/gh commands.
    Returns GitOperationResult with detected operations (any field may be None).
    """
    result = GitOperationResult()

    # --- Commit detection ---
    if "git commit" in command or "git cherry-pick" in command:
        sha_match = _COMMIT_SHA_PATTERN.search(output)
        if sha_match:
            sha = sha_match.group(1)
            if "--amend" in command:
                kind = "amended"
            elif "cherry-pick" in command:
                kind = "cherry-picked"
            else:
                kind = "committed"
            result.commit = DetectedCommit(sha=sha, kind=kind)

    # --- Push detection ---
    if "git push" in command:
        push_match = _PUSH_BRANCH_PATTERN.search(output)
        if push_match:
            result.push = DetectedPush(branch=push_match.group(1))

    # --- PR detection (gh pr) ---
    gh_pr_match = re.search(r"gh\s+pr\s+(\w+)", command)
    if gh_pr_match:
        subcommand = gh_pr_match.group(1)
        action = _GH_PR_ACTIONS.get(subcommand)
        if action:
            pr_num = None
            pr_url = None

            # Try to extract PR number
            num_match = _PR_NUMBER_PATTERN.search(output)
            if num_match:
                pr_num = int(num_match.group(1))

            # Try to extract PR URL
            url_match = _PR_URL_PATTERN.search(output)
            if url_match:
                pr_url = url_match.group(1)
                # Extract number from URL if we didn't get it above
                if pr_num is None:
                    url_num_match = re.search(r"/pull/(\d+)", pr_url)
                    if url_num_match:
                        pr_num = int(url_num_match.group(1))

            if pr_num is not None:
                result.pr = DetectedPR(number=pr_num, url=pr_url, action=action)

    # --- PR detection (glab mr) ---
    if _GLAB_MR_PATTERN.search(command):
        url_match = _PR_URL_PATTERN.search(output)
        num_match = re.search(r"!(\d+)", output)
        if num_match:
            result.pr = DetectedPR(
                number=int(num_match.group(1)),
                url=url_match.group(1) if url_match else None,
                action="created",
            )

    return result


def track_git_operation(operation: GitOperationResult, session_id: str) -> None:
    """Log a detected git operation for a session.

    Stores operations in memory for the session lifetime.
    Auto-links sessions to PRs when PR operations are detected.
    """
    if not session_id:
        return

    if session_id not in _session_operations:
        _session_operations[session_id] = []
    _session_operations[session_id].append(operation)

    # Auto-link session to PR
    if operation.pr and operation.pr.url:
        link_session_to_pr(session_id, operation.pr.url)

    # Log for debugging
    if operation.commit:
        logger.info(
            f"Git operation: {operation.commit.kind} {operation.commit.sha} (session {session_id})"
        )
    if operation.push:
        logger.info(f"Git operation: push to {operation.push.branch} (session {session_id})")
    if operation.pr:
        logger.info(
            f"Git operation: PR #{operation.pr.number} {operation.pr.action} "
            f"{operation.pr.url or ''} (session {session_id})"
        )


def track_git_operations(command: str, output: str, session_id: str) -> GitOperationResult:
    """Detect and track git operations from command output.

    Convenience function combining detect + track.
    Returns the detected operation result.
    """
    operation = detect_git_operation(command, output)
    if operation.commit or operation.push or operation.pr:
        track_git_operation(operation, session_id)
    return operation


def link_session_to_pr(session_id: str, pr_url: str) -> None:
    """Link a session to a PR URL for tracking."""
    if session_id not in _session_pr_links:
        _session_pr_links[session_id] = []
    if pr_url not in _session_pr_links[session_id]:
        _session_pr_links[session_id].append(pr_url)


def get_session_operations(session_id: str) -> list[GitOperationResult]:
    """Get all tracked operations for a session."""
    return _session_operations.get(session_id, [])


def get_session_pr_links(session_id: str) -> list[str]:
    """Get all PR URLs linked to a session."""
    return _session_pr_links.get(session_id, [])
