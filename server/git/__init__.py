"""
Git integration module for Whisper.

Provides git discovery, state queries, filesystem-based reads,
config parsing, gitignore management, diff system, security,
repository detection, and operation tracking.
"""

from server.git.core import (
    find_canonical_git_root,
    find_git_root,
    get_branch,
    get_changed_files,
    get_default_branch,
    get_file_status,
    get_git_exe,
    get_head,
    get_is_clean,
    get_is_git,
    get_remote_url,
    get_repo_remote_hash,
    has_unpushed_commits,
    is_bare_git_repo,
    normalize_git_remote_url,
    stash_to_clean_state,
)
from server.git.diff import (
    DiffFileStats,
    DiffHunk,
    GitDiffResult,
    ToolUseDiff,
    fetch_git_diff,
    fetch_git_diff_hunks,
    fetch_single_file_git_diff,
    is_transient_git_state,
    parse_git_diff,
    parse_shortstat,
)
from server.git.filesystem import (
    get_common_dir,
    get_worktree_count,
    is_safe_ref_name,
    is_shallow_clone,
    is_valid_git_sha,
    read_git_head,
    read_worktree_head_sha,
    resolve_git_dir,
    resolve_ref,
)
from server.git.prompts import (
    build_commit_prompt,
    build_git_instructions_prompt,
    build_git_status_prompt,
    build_pr_prompt,
)
from server.git.repository import (
    ParsedRepository,
    detect_current_repository,
    detect_current_repository_with_host,
    looks_like_real_hostname,
    parse_git_remote,
)
from server.git.router import router as git_router
from server.git.security import (
    check_bare_repo,
    contains_secret_files,
    get_destructive_command_warning,
    is_read_only_git_command,
    validate_git_command,
    validate_worktree,
)
from server.git.tracking import (
    DetectedCommit,
    DetectedPR,
    DetectedPush,
    GitOperationResult,
    detect_git_operation,
    get_session_operations,
    get_session_pr_links,
    link_session_to_pr,
    track_git_operation,
    track_git_operations,
)
