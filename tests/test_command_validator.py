"""Allow / deny matrix for the shell command validator.

These are the cases the security review flagged as bypass-prone — keep them
green so a regression here can't ship silently.
"""

import pytest

from server.security.command_validator import (
    check_gh_command,
    check_sed_inplace,
    check_subcommand_cap,
    strip_heredoc_bodies,
    validate_command,
)


def test_blocks_sed_dash_i_with_space():
    assert check_sed_inplace("sed -i s/foo/bar/ file.txt") is not None


def test_blocks_sed_dash_i_dot_bak():
    assert check_sed_inplace("sed -i.bak s/foo/bar/ file.txt") is not None


def test_blocks_sed_dash_i_quoted_bak():
    assert check_sed_inplace("sed -i'.bak' file.txt") is not None


def test_blocks_long_form_in_place():
    assert check_sed_inplace("sed --in-place s/foo/bar/ file.txt") is not None


def test_blocks_long_form_inplace_no_dash():
    assert check_sed_inplace("sed --inplace s/foo/bar/ file.txt") is not None


def test_blocks_dash_i_after_other_flags():
    assert check_sed_inplace("sed -e foo -i bar") is not None


def test_blocks_dash_i_followed_by_semicolon():
    assert check_sed_inplace("sed -i;ls") is not None


def test_allows_sed_dash_e():
    assert check_sed_inplace('sed -e "s/foo/bar/" file') is None


def test_allows_sed_dash_n():
    assert check_sed_inplace("sed -n /pattern/p file.txt") is None


def test_allows_sed_dash_E():
    assert check_sed_inplace("sed -E s/a/b/ file") is None


def test_allows_unrelated_command_containing_sed_token():
    assert check_sed_inplace("echo sed") is None


# ── Heredoc bodies are literal data, not command chains ──────────────
# Regression: writing a markdown file with a table (full of `|` pipes) via
# `cat > f <<'EOF' … EOF` used to trip the chained-command cap, because the
# guard counted the table's pipes as pipelines.


def _md_heredoc(rows: int) -> str:
    body = "\n".join(f"| item {i} | value {i} | note {i} |" for i in range(rows))
    return "cat > review.md <<'EOF'\n# Review\n\n| A | B | C |\n|---|---|---|\n" + body + "\nEOF"


def test_heredoc_markdown_table_not_blocked():
    cmd = _md_heredoc(40)  # ~170 pipes in the body — far past the cap
    assert cmd.count("|") > 100
    assert validate_command(cmd) is None


def test_strip_heredoc_removes_body_but_keeps_opener():
    stripped = strip_heredoc_bodies(_md_heredoc(40))
    assert "cat > review.md" in stripped
    assert "item 0" not in stripped
    assert "| A | B | C |" not in stripped


def test_real_chained_pipeline_still_capped():
    cmd = "echo x" + " | cat" * 60
    assert check_subcommand_cap(cmd) is not None
    assert validate_command(cmd) is not None


def test_command_after_heredoc_still_validated():
    cmd = "cat > f <<'EOF'\nhello\nEOF\nrm -rf /"
    assert validate_command(cmd) is not None


# ── `gh` block: the advice has to match what is actually in the tool pool ──
#
# github / github_api / github_api_write only enter the catalog when a
# workspace is connected AND it is a git repo (server/chat/tool_pool.py). They
# are deferred, so with that gate closed tool_search cannot reach them either.
# Naming them anyway sent a real session into ~13 shell retries before it gave
# up and handed the user a link to open the PR by hand.


@pytest.fixture
def git_workspace(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    return tmp_path


def test_gh_is_blocked_either_way(git_workspace):
    assert check_gh_command("gh pr create --base main") is not None


def test_gh_block_names_the_tools_when_they_are_reachable(git_workspace):
    msg = check_gh_command("gh pr create --base main")
    assert "github_api_write" in msg
    assert "connect" not in msg.lower()


def test_gh_block_says_connect_a_workspace_when_the_tools_are_absent(monkeypatch):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    msg = check_gh_command("gh pr create --base main")
    assert "not in this session's tool pool" in msg
    assert "connect the repository as the workspace" in msg


def test_gh_block_says_connect_when_the_workspace_is_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    msg = check_gh_command("gh pr create --base main")
    assert "not in this session's tool pool" in msg


def test_gh_block_survives_a_broken_workspace_lookup(monkeypatch):
    """Advice quality must never turn a validator call into an exception."""

    def _boom():
        raise RuntimeError("workspace state unreadable")

    monkeypatch.setattr("server.workspace.state.get_workspace_path", _boom)
    assert check_gh_command("gh pr view 1") is not None


def test_non_gh_commands_are_untouched(git_workspace):
    assert check_gh_command("git status") is None
    assert check_gh_command("ghost --help") is None
