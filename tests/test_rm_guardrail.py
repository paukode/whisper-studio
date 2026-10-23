"""User directive: rm (and its close cousins) never deletes anything outside
the connected workspace without a real human clicking Approve — no permission
mode, category override, blanket session approval, custom rule or
trusted-skill exemption may cover it. Inside the workspace (and the app's own
folders) a delete is an ordinary command: bypass means bypass there, and the
other modes apply their usual command rule. Two independent layers:

1. server.security.permissions.resolve_static_decision forces "ask" for any
   command-shaped tool input that deletes beyond the workspace, an absolute
   floor exactly like the existing github-destructive category.
2. server.approval.spec.refuse_if_agent_rm hard-refuses every rm inside the
   executors themselves (_do_command, _do_terminal_run) for the unattended
   path, which skips resolve_static_decision entirely and has no human at all.
"""

import asyncio
import os

import pytest

from server.security.delete_sites import delete_sites, is_rm_command
from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    approval_floor,
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


def test_a_pipe_inside_quotes_is_data_not_a_pipeline():
    # The regression (2026-10-22): a regex alternation such as 'a|b' was split
    # at its quoted |, each half kept an unclosed quote, and that parse failure
    # read as a delete of unknown reach, so a plain search asked in bypass.
    assert not is_rm_command("rg -n 'DB =|PLANNER =' backend/app")
    assert not is_rm_command('grep -E "x|y" f.txt')
    assert not is_rm_command("echo 'a|rm -rf /'")
    assert is_rm_command("rg -l 'a|b' . | xargs rm")
    assert is_rm_command("bash -c 'ls | rm -rf /x'")


def test_only_the_filesystem_root_is_a_hard_block():
    """rm -rf / stays refused outright. Any other absolute path goes to the
    rm floor, which lets the mode decide inside the workspace and the app's
    folders (the session's own scratch cleanup) and asks anywhere else."""
    from server.security.command_validator import validate_command

    for root in ("rm -rf /", "rm -rf /*", "rm -rf / --no-preserve-root", "rm -rf //"):
        assert validate_command(root), root
    assert validate_command("rm -rf /Users/me/.whisper/data/scratch/s1/probe") is None
    assert validate_command("rm -rf /Users/me/project/dist") is None


def test_a_search_with_alternations_hits_no_floor():
    """The command from the field report, verbatim in shape: a search over a
    brace-expanded file list chained to a jq read. It deletes nothing, so no
    floor applies and the mode decides (bypass runs it)."""
    command = (
        "rg -n -A 3 -B 2 'DB =|RESOURCE_REQUESTS =|LOE_TEAM =|ReadInternalWebsites|"
        "generic_search' app/backend/{sources,demand,main}.py app/frontend/src/lib/pipeline.ts; "
        "jq '{topLevelKeys: keys, meta: .meta, sources: .sources}' app/data/snapshot.json"
    )
    assert delete_sites(command) == []
    assert approval_floor("ws_run_command", {"command": command}, "cli") is None


def test_does_not_flag_rm_mentioned_only_in_heredoc_data():
    # The heredoc BODY is literal file content, not a command — "rm -rf /" is
    # text being written to a file here, not executed.
    assert not is_rm_command("cat > note.txt <<'EOF'\nrm -rf /\nEOF")


def test_unparseable_segment_is_treated_as_rm_like():
    # Fail toward requiring approval, not away from it: an unbalanced quote
    # is exactly the kind of thing an obfuscated command produces. Tested at
    # the segment check directly: split_subcommands' own regex treats a lone
    # unmatched quote character as an implicit break, so by the time
    # is_rm_command's segments reach the tokenizer they are individually
    # well-formed; this failure mode only reaches a caller that hands
    # _segment_delete_sites a raw, unsplit segment. The delete's reach is
    # unknown, which the floor reads as outside.
    from server.security.delete_sites import _segment_delete_sites

    sites = _segment_delete_sites("echo 'unterminated", 0)
    assert len(sites) == 1 and sites[0].operands is None


def test_a_statement_that_runs_nothing_is_not_a_delete():
    # The regression: a shell script's bare assignment (`backend_port=8000;`)
    # named no command, and "names no command" was read as a delete, so a
    # launch config that never deleted anything hit the rm floor in bypass.
    assert not is_rm_command("backend_port=8000")
    assert not is_rm_command("x=1; echo hi")
    assert not is_rm_command("npm run dev & pid=$!; wait $pid")
    assert not is_rm_command("while true; do\n  npm test\ndone")
    assert not is_rm_command("FOO=bar npm run dev")
    assert not is_rm_command("bash -c 'set -eu; port=8000; npm run dev -- --port $port'")


def test_delete_sites_report_what_each_delete_names():
    assert [(s.verb, s.operands) for s in delete_sites("rm -rf build dist/*")] == [
        ("rm", ["build", "dist/*"])
    ]
    assert delete_sites("rm -- -dashed")[0].operands == ["-dashed"]
    assert delete_sites("sudo -u root rm -rf /var/x")[0].operands == ["/var/x"]
    assert delete_sites("find . -name '*.pyc' -delete")[0].operands == ["."]
    assert delete_sites("find -L /etc /var -type f -exec rm {} \\;")[0].operands == [
        "/etc",
        "/var",
    ]
    assert delete_sites("find -delete")[0].operands == ["."]
    assert delete_sites("sh -c 'npm run build && rm -rf dist'")[0].operands == ["dist"]
    assert delete_sites("echo a | xargs rm")[0].operands is None
    assert delete_sites("echo a | xargs -I {} rm {}")[0].operands is None
    assert delete_sites("npm run build; rm -rf dist; rm old.log") == [
        delete_sites("rm -rf dist")[0],
        delete_sites("rm old.log")[0],
    ]


def test_delete_sites_carry_the_cds_before_them():
    from server.security.delete_sites import ChDir

    assert delete_sites("cd web && rm -rf dist")[0].chdirs == (ChDir("web", sure=True),)
    assert delete_sites("cd web; rm -rf dist")[0].chdirs == (ChDir("web"),)
    assert delete_sites("cd -P -- web 2>/dev/null && rm x")[0].chdirs == (ChDir("web", True),)
    assert delete_sites("popd && rm x")[0].chdirs == (ChDir(None, True),)
    assert delete_sites("rm -rf dist; cd web")[0].chdirs == ()


def test_detects_rm_inside_a_shell_script_argument():
    # The script after -c is a command line of its own; `sh -c "rm -rf build"`
    # used to read as the command `sh`.
    assert is_rm_command("sh -c 'rm -rf build'")
    assert is_rm_command('bash -c "rm -rf build && npm run dev"')
    assert is_rm_command("bash -lc 'npm run build; rm -rf dist'")
    assert is_rm_command("zsh -o pipefail -c 'rm x'")
    assert is_rm_command("/bin/sh -e -c 'rm x'")
    assert is_rm_command("sh -c \"bash -c 'rm -rf x'\"")
    # The argv a launch config joins with plain spaces keeps the verb visible.
    assert is_rm_command("sh -c rm -rf build")


def test_detects_rm_behind_more_wrappers():
    assert is_rm_command("nohup rm -rf x")
    assert is_rm_command("time rm x")
    assert is_rm_command("timeout 10 rm x")
    assert is_rm_command("sudo -u root rm -rf x")
    assert is_rm_command("echo a | xargs -I {} rm {}")
    assert is_rm_command("nohup sh -c 'rm -rf x'")
    assert is_rm_command("eval 'rm -rf x'")


def test_detects_rm_after_a_reserved_word():
    assert is_rm_command("sh -c 'if true; then rm -rf dist; fi'")
    assert is_rm_command("if rm x; then echo gone; fi")
    assert is_rm_command("(rm -rf build)")
    assert is_rm_command("( rm -rf build )")


def _first_operands(command):
    sites = delete_sites(command)
    return sites[0].operands if sites else None


def test_detects_rm_in_a_case_arm():
    # The arm's pattern (`1)`, `a|b)`, `(a)`) sits where the command word
    # would be, and `case` itself was read as the command.
    for command in (
        "case 1 in 1) rm -rf ~/x ;; esac",
        "case $x in a|b) rm -rf ~/x;; esac",
        "case $x in (a) rm -rf ~/x;; esac",
        "case $x in\n  *) rm -rf ~/x ;;\nesac",
        "sh -c 'case $1 in clean) rm -rf ~/x;; esac'",
    ):
        assert _first_operands(command) == ["~/x"], command
    assert not is_rm_command("case $x in a) echo hi ;; *) npm test ;; esac")


def test_detects_rm_in_a_function_body():
    for command in (
        "f() { rm -rf ~/x; }; f",
        "f () { rm -rf ~/x; }; f",
        "function f { rm -rf ~/x; }; f",
        "function f() { rm -rf ~/x; }; f",
        "f() rm -rf ~/x",  # zsh: a simple command as the body
        "f g () { rm -rf ~/x; }",  # zsh: one body for two names
    ):
        assert _first_operands(command) == ["~/x"], command
    assert "~/x" in _first_operands("f() ( rm -rf ~/x ); f")
    assert not is_rm_command("f() { npm test; }; f")
    assert not is_rm_command("git commit -m 'call f() once'")


def test_detects_rm_behind_zsh_precommand_modifiers():
    for command in (
        "noglob rm -rf ~/x",
        "nocorrect rm -rf ~/x",
        "builtin rm -rf ~/x",  # zsh/files makes rm a builtin
        "noglob sudo rm -rf ~/x",
        "if true; then noglob rm -rf ~/x; fi",
    ):
        assert _first_operands(command) == ["~/x"], command
    assert not is_rm_command("noglob git log --oneline")


def test_a_cd_in_a_function_body_or_a_case_arm_is_tracked():
    from server.security.delete_sites import ChDir

    # A function body runs at each call, as often as it is called: with a cd
    # in it no delete in the command has a working directory the line names.
    for command in (
        "f() { cd web; }; f; rm -rf dist",
        "function f { cd web; }; f; rm -rf dist",
        "f() {\n  cd web\n}\nf\nrm -rf dist",
        "f() { rm -rf dist; cd web; }; f; f",
    ):
        assert delete_sites(command)[0].chdirs == (ChDir(None),), command
    assert delete_sites("f() { rm -rf dist; }; f")[0].chdirs == ()
    # A case arm may not run, so its cd is not sure.
    assert delete_sites("case $x in a) cd web ;; esac; rm -rf dist")[0].chdirs == (ChDir("web"),)
    assert delete_sites("noglob cd web && rm -rf dist")[0].chdirs == (ChDir("web", sure=True),)


def test_a_wrapper_that_changes_directory_moves_its_delete():
    from server.security.delete_sites import ChDir

    documents = (ChDir("~/Documents", sure=True),)
    for command in (
        "env -C ~/Documents rm -rf notes",
        "env -i -C ~/Documents FOO=1 rm -rf notes",
        "env -iC ~/Documents rm -rf notes",
        "env --chdir ~/Documents rm -rf notes",
        "sudo -D ~/Documents rm -rf notes",
        "sudo -u root -nD ~/Documents rm -rf notes",
        "sudo 2>/dev/null -D ~/Documents rm -rf notes",
        "sudo --chdir ~/Documents rm -rf notes",
        "sudo --chd ~/Documents rm -rf notes",  # getopt_long takes a unique prefix
        "nohup sudo -D ~/Documents rm -rf notes",
        "sudo -D ~/Documents bash -c 'rm -rf notes'",
        "env -C ~/Documents find . -delete",
    ):
        assert delete_sites(command)[0].chdirs == documents, command
    assert delete_sites("sudo --chdir=/tmp rm x")[0].chdirs == (ChDir("/tmp", sure=True),)
    # A user called sudo is an option value, not a second sudo moving again.
    assert delete_sites("sudo -u sudo -D web rm x")[0].chdirs == (ChDir("web", sure=True),)
    assert delete_sites("cd web && env -C out rm x")[0].chdirs == (
        ChDir("web", sure=True),
        ChDir("out", sure=True),
    )
    # A folder the line does not name: a value written onto its option is
    # not tilde-expanded, a value can be missing, sudo -i moves to the target
    # user's home, and an option the wrapper does not have (or a long name
    # two of its options start with) cannot be read past.
    unknown = (ChDir(None, sure=True),)
    for command in (
        "env -C~/Documents rm -rf notes",
        "sudo --chdir=~/Documents rm -rf notes",
        "sudo -i rm -rf notes",
        "sudo --login rm -rf notes",
        "sudo -X -D /tmp rm -rf notes",
        "sudo --ch /tmp rm -rf notes",
    ):
        assert delete_sites(command)[0].chdirs == unknown, command
    # Under another root no operand reads as written.
    assert delete_sites("sudo -R /mnt rm -rf /x")[0].operands is None
    assert delete_sites("sudo --chroot=/mnt sh -c 'rm -rf /x'")[0].operands is None
    # Option values and words that only look like a wrapper move nothing.
    for command in (
        "sudo -u env rm -rf notes",
        "env -u C rm x",
        "sudo -p -D rm x",
        "nice env rm x",
    ):
        assert delete_sites(command)[0].chdirs == (), command


def test_shell_scripts_reads_a_shell_anywhere_in_an_argv():
    from server.security.delete_sites import shell_scripts

    assert shell_scripts(["sh", "-c", "rm -rf build && npm run dev"]) == [
        "rm -rf build && npm run dev"
    ]
    assert shell_scripts(["nohup", "bash", "-lc", "npm run dev"]) == ["npm run dev"]
    assert shell_scripts(["bash", "build.sh"]) == []
    assert shell_scripts(["npm", "run", "dev"]) == []


def test_shells_and_wrappers_that_do_not_delete_pass():
    assert not is_rm_command("sh -c 'npm run build && npm start'")
    assert not is_rm_command("bash -lc 'npm test'")
    assert not is_rm_command("bash build.sh")
    assert not is_rm_command("nohup npm run dev")
    assert not is_rm_command("time python manage.py runserver")
    assert not is_rm_command("timeout 30 curl -s http://localhost:3000")


# ── resolve_static_decision: the floor for a delete beyond the workspace ────


def _decide(command, mode=MODE_DEFAULT, approvals=None, auto_allow_trusted=False, **extra):
    return resolve_static_decision(
        "ws_run_command",
        {"command": command, **extra},
        "cli",
        approvals or {},
        mode,
        auto_allow_trusted,
    )


ALL_MODES = (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_DONT_ASK, MODE_BYPASS)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    (ws / "dist").mkdir(parents=True)
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": MODE_DEFAULT, "rules": [], "category_modes": {}},
    )
    return ws


def test_a_delete_inside_the_workspace_is_an_ordinary_command(workspace):
    # Bypass means bypass inside the workspace, and the other modes apply
    # their usual command rule: a session "Yes, all commands" covers it.
    for command in (
        "rm -rf ./build",
        "rm -rf dist/*",
        "rm old.log",
        "find . -name '*.pyc' -delete",
        "cd web && npm run build && cd .. && rm -rf dist",
    ):
        assert approval_floor("ws_run_command", {"command": command}, "cli") is None
        assert _decide(command, mode=MODE_BYPASS) == "allow"
        assert _decide(command, mode=MODE_DEFAULT) == "ask"
        assert _decide(command, approvals={"cli": "allow"}) == "allow"
        assert _decide(command, mode=MODE_DONT_ASK) == "deny"
        assert _decide(command, auto_allow_trusted=True) == "allow"


def test_a_delete_outside_the_workspace_always_asks(workspace, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outside = (
        f"rm -rf {elsewhere}",
        "rm -rf ../elsewhere",
        "rm -rf dist/../../elsewhere",
        "rm -rf ~/Downloads/build",
        "rm -rf /",
        "rm -rf /*",
        "rm -rf $BUILD_DIR",
        "echo build | xargs rm -rf",
        "find / -name '*.tmp' -delete",
        "npm run build && rm -rf ~/old",
        "( rm -rf ~/old )",
        "cd ~/Documents && rm -rf notes",
        'cd "$OUT" && rm -rf build',
        "case 1 in 1) rm -rf ~/old ;; esac",
        "f() { rm -rf ~/old; }; f",
        "function f { cd ~/Documents; }; f; rm -rf notes",
        "noglob rm -rf ~/old",
        "env -C ~/Documents rm -rf notes",
        "sudo -D ~/Documents rm -rf notes",
    )
    for command in outside:
        assert approval_floor("ws_run_command", {"command": command}, "cli") == "rm", command
        for mode in ALL_MODES:
            assert _decide(command, mode=mode) == "ask", (command, mode)
        assert _decide(command, approvals={"cli": "allow"}) == "ask", command
        assert _decide(command, mode=MODE_BYPASS, approvals={"cli": "allow"}) == "ask"
        assert _decide(command, auto_allow_trusted=True) == "ask"


def test_a_symlink_out_of_the_workspace_reads_as_outside(workspace, tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    os.symlink(target, workspace / "link")
    assert approval_floor("ws_run_command", {"command": "rm -rf link/"}, "cli") == "rm"
    assert approval_floor("ws_run_command", {"command": "rm -rf link/*"}, "cli") == "rm"
    # The kernel climbs .. from where the link points, not from the link.
    assert approval_floor("ws_run_command", {"command": "rm -rf link/../x"}, "cli") == "rm"


def test_the_working_directory_decides_where_a_relative_delete_lands(workspace, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert (
        approval_floor("ws_run_command", {"command": "rm -rf build", "cwd": str(elsewhere)}, "cli")
        == "rm"
    )
    assert (
        approval_floor("ws_run_command", {"command": "rm -rf build", "cwd": "web"}, "cli") is None
    )
    assert approval_floor("terminal_run", {"command": "rm -rf build", "cwd": "~"}, "cli") == "rm"


def test_the_card_the_gate_stamped_decides_its_own_workspace(workspace, tmp_path):
    # A workspace-bound card carries the root it was raised in; the floor
    # reads that root, not whatever is connected when the decision is made.
    other = tmp_path / "other"
    other.mkdir()
    payload = {"command": "rm -rf build", "workspace_root": str(other)}
    assert approval_floor("ws_run_command", payload, "cli") is None
    payload = {"command": f"rm -rf {workspace / 'dist'}", "workspace_root": str(other)}
    assert approval_floor("ws_run_command", payload, "cli") == "rm"


def test_with_no_workspace_every_delete_asks(monkeypatch):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    assert _decide("rm -rf ./build") == "ask"
    assert _decide("rm -rf ./build", mode=MODE_BYPASS) == "ask"
    assert _decide("rm -rf ./build", approvals={"cli": "allow"}) == "ask"
    assert _decide("rm -rf ./build", mode=MODE_BYPASS, approvals={"cli": "allow"}) == "ask"
    assert _decide("rm -rf ./build", auto_allow_trusted=True) == "ask"
    # dontAsk normally auto-DENIES write-class tools silently; a delete the
    # floor catches must still surface a real prompt rather than a silent,
    # unexplained refusal.
    assert _decide("rm -rf ./build", mode=MODE_DONT_ASK) == "ask"


def test_the_apps_own_folders_count_as_inside(workspace):
    # The user's rule: bypass may delete under ~/.whisper too; the other
    # modes keep their delete rule, which for a command is the cli rule.
    from server.infrastructure.paths import data_root

    scratch = os.path.join(data_root(), "scratch", "sess", "probe.py")
    assert approval_floor("ws_run_command", {"command": f"rm {scratch}"}, "cli") is None
    assert _decide(f"rm {scratch}", mode=MODE_BYPASS) == "allow"
    assert _decide(f"rm {scratch}", mode=MODE_DEFAULT) == "ask"


def test_non_rm_commands_are_unaffected_by_the_guardrail(workspace):
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


def test_refuse_if_agent_rm_blocks_rm_wherever_the_command_hides_it():
    from server.approval.spec import refuse_if_agent_rm

    for command in (
        "case 1 in 1) rm -rf x ;; esac",
        "f() { rm -rf x; }; f",
        "function f { rm -rf x; }; f",
        "noglob rm -rf x",
        "nocorrect rm -rf x",
        "env -C /tmp rm -rf x",
        "sudo -D /tmp rm -rf x",
    ):
        outcome = refuse_if_agent_rm({"__agent__": True, "command": command})
        assert outcome is not None and outcome.ok is False, command


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
