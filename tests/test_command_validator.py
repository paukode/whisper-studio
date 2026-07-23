"""Allow / deny matrix for the shell command validator.

These are the cases the security review flagged as bypass-prone — keep them
green so a regression here can't ship silently.
"""

from server.security.command_validator import (
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
