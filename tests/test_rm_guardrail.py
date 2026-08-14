"""User directive: rm (and its close cousins) must never run without a real
human clicking Approve — no permission mode, category override, blanket
session approval, custom rule, trusted-skill exemption, or unattended-agent
auto-approve may cover it. Two independent layers:

1. server.security.permissions.resolve_static_decision forces "ask" for any
   command-shaped tool input containing rm, an absolute floor exactly like
   the existing github-destructive category.
2. server.approval.spec.refuse_if_agent_rm hard-refuses it inside the
   executors themselves (_do_command, _do_terminal_run) for the unattended
   path, which skips resolve_static_decision entirely.
"""

import asyncio

from server.security.command_validator import is_rm_command
from server.security.permissions import (
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    resolve_static_decision,
)

# ── is_rm_command: the detector itself ──────────────────────────────────────


def test_detects_plain_and_flagged_rm():
    assert is_rm_command("rm foo.txt")
    assert is_rm_command("rm -rf ./build")
    assert is_rm_command("rm --no-preserve-root -rf /")
    assert is_rm_command("/bin/rm -f x")


def test_detects_wrapped_rm():
    assert is_rm_command("sudo rm -rf /tmp/x")
    assert is_rm_command("env FOO=bar rm x")
    assert is_rm_command("nice -19 rm x")


def test_detects_rm_chained_or_piped():
    assert is_rm_command("echo hi && rm foo.txt")
    assert is_rm_command("echo hi; rm foo.txt")
    assert is_rm_command("true || rm foo.txt")
    assert is_rm_command("echo foo.txt | xargs rm")


def test_detects_rm_on_its_own_line_with_no_separator():
    # A multi-line terminal_run/workflow script has no ;/&&/|| between lines.
    assert is_rm_command("echo building\nrm -rf dist\necho done")


def test_detects_close_cousins():
    assert is_rm_command("rmdir empty_dir")
    assert is_rm_command("unlink foo")
    assert is_rm_command("shred -u secret.txt")


def test_detects_find_delete():
    assert is_rm_command("find . -name '*.tmp' -delete")
    assert is_rm_command("find . -name '*.o' -exec rm {} \\;")


def test_does_not_flag_unrelated_commands():
    assert not is_rm_command("ls -la | grep foo")
    assert not is_rm_command("git commit -m 'fix rm bug'")
    assert not is_rm_command("npm run build")
    assert not is_rm_command("mv old.txt new.txt")
    assert not is_rm_command("echo 'never rm anything'")
    assert not is_rm_command("grep -rn 'term' file.py")
    assert not is_rm_command("confirm --yes")


def test_does_not_flag_rm_mentioned_only_in_heredoc_data():
    # The heredoc BODY is literal file content, not a command — "rm -rf /" is
    # text being written to a file here, not executed.
    assert not is_rm_command("cat > note.txt <<'EOF'\nrm -rf /\nEOF")


def test_unparseable_segment_is_treated_as_rm_like():
    # Fail toward requiring approval, not away from it — an unbalanced quote
    # is exactly the kind of thing an obfuscated command produces. Tested at
    # the tokenizer directly: split_subcommands' own regex treats a lone
    # unmatched quote character as an implicit break, so by the time
    # is_rm_command's segments reach the tokenizer they are individually
    # well-formed — this failure mode only reaches a caller that hands
    # _leading_command_token a raw, unsplit segment.
    from server.security.command_validator import _leading_command_token

    assert _leading_command_token("echo 'unterminated") is None


# ── resolve_static_decision: the absolute-ask floor ─────────────────────────


def _decide(command, mode=MODE_DEFAULT, approvals=None, auto_allow_trusted=False):
    return resolve_static_decision(
        "ws_run_command",
        {"command": command},
        "cli",
        approvals or {},
        mode,
        auto_allow_trusted,
    )


def test_rm_always_asks_regardless_of_mode_or_approval():
    assert _decide("rm -rf ./build") == "ask"
    assert _decide("rm -rf ./build", mode=MODE_BYPASS) == "ask"
    assert _decide("rm -rf ./build", approvals={"cli": "allow"}) == "ask"
    assert _decide("rm -rf ./build", mode=MODE_BYPASS, approvals={"cli": "allow"}) == "ask"
    assert _decide("rm -rf ./build", auto_allow_trusted=True) == "ask"


def test_rm_asks_even_under_dontask(monkeypatch):
    # dontAsk normally auto-DENIES write-class tools silently; rm must still
    # surface a real prompt rather than a silent, unexplained refusal.
    assert _decide("rm -rf ./build", mode=MODE_DONT_ASK) == "ask"


def test_non_rm_commands_are_unaffected_by_the_guardrail():
    # bypassPermissions still works normally for anything that isn't rm.
    assert _decide("npm run build", mode=MODE_BYPASS) == "allow"
    assert _decide("npm run build", approvals={"cli": "allow"}) == "allow"


def test_rm_guardrail_only_looks_at_a_real_command_field():
    # A tool with no "command" input (e.g. git_add_commit sharing the "cli"
    # category) must not be spuriously affected by this check.
    decision = resolve_static_decision(
        "git_add_commit", {"message": "rm the old config"}, "cli", {"cli": "allow"}, MODE_DEFAULT
    )
    assert decision == "allow"  # "rm" only appears in a commit MESSAGE, not a command


# ── refuse_if_agent_rm: the unattended-agent hard refusal ───────────────────


def test_refuse_if_agent_rm_blocks_unattended_rm():
    from server.approval.spec import refuse_if_agent_rm

    outcome = refuse_if_agent_rm({"__agent__": True, "command": "rm -rf /tmp/x"})
    assert outcome is not None
    assert outcome.ok is False


def test_refuse_if_agent_rm_allows_unattended_non_rm():
    from server.approval.spec import refuse_if_agent_rm

    assert refuse_if_agent_rm({"__agent__": True, "command": "npm run build"}) is None


def test_refuse_if_agent_rm_allows_interactive_rm():
    # Not agent-originated — a real human is present, so this check has
    # nothing to refuse; the ordinary approval card handles it.
    from server.approval.spec import refuse_if_agent_rm

    assert refuse_if_agent_rm({"command": "rm -rf /tmp/x"}) is None


def test_do_command_refuses_unattended_rm():
    from server.approval.bootstrap import _do_command

    outcome = asyncio.run(_do_command({"__agent__": True, "command": "rm -rf /tmp/x"}))
    assert outcome.ok is False
    assert "unattended" in (outcome.error or "").lower()


def test_do_terminal_run_refuses_unattended_rm():
    from server.approval.bootstrap import _do_terminal_run

    outcome = asyncio.run(_do_terminal_run({"__agent__": True, "command": "rm -rf /tmp/x"}))
    assert outcome.ok is False
    assert "unattended" in (outcome.error or "").lower()
