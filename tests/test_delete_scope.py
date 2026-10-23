"""Where a deleting command reaches (server/security/delete_scope.py).

The rm floor asks only for a delete that may touch something outside the
roots a mode may delete in on its own. Operands are resolved the way the
shell starts to: against the working directory, ~ expanded, a glob cut at its
wildcard, symlinks followed. Anything the command line cannot resolve counts
as outside, so the floor fails toward asking.
"""

import os

import pytest

from server.security.delete_scope import deletes_beyond, resolve_operand


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "dist").mkdir(parents=True)
    return str(root)


def test_relative_operands_resolve_against_the_working_directory(ws):
    assert resolve_operand("dist", ws) == os.path.realpath(os.path.join(ws, "dist"))
    assert resolve_operand(".", ws) == os.path.realpath(ws)
    assert resolve_operand("./", ws) == os.path.realpath(ws)
    assert resolve_operand("../x", ws) == os.path.realpath(os.path.join(ws, "..", "x"))
    assert resolve_operand("dist", None) is None


def test_globs_check_the_directory_the_wildcard_sits_in(ws):
    real = os.path.realpath(ws)
    assert resolve_operand("dist/*", ws) == os.path.join(real, "dist")
    assert resolve_operand("dist/*.js", ws) == os.path.join(real, "dist")
    assert resolve_operand("*", ws) == real
    assert resolve_operand("*.pyc", ws) == real
    assert resolve_operand("build-?/x", ws) == real
    assert resolve_operand("{a,b}", ws) == real
    assert resolve_operand("/*", ws) == "/"


def test_tilde_and_absolute_paths_ignore_the_working_directory(ws):
    assert resolve_operand("~/Downloads/x", ws) == os.path.realpath(
        os.path.expanduser("~/Downloads/x")
    )
    assert resolve_operand("/var/tmp/x", ws) == os.path.realpath("/var/tmp/x")
    assert resolve_operand("~nobody-such-user/x", ws) is None


def test_expansions_cannot_be_resolved(ws):
    assert resolve_operand("$BUILD_DIR", ws) is None
    assert resolve_operand("${HOME}/x", ws) is None
    assert resolve_operand("`pwd`/x", ws) is None
    assert resolve_operand("", ws) is None


def test_a_symlink_is_read_as_where_it_points(ws, tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    os.symlink(target, os.path.join(ws, "link"))
    assert resolve_operand("link", ws) == os.path.realpath(str(target))
    assert resolve_operand("link/*", ws) == os.path.realpath(str(target))


def test_deletes_inside_the_roots_stay_inside(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    roots = [ws, str(other)]
    for command in (
        "rm -rf dist",
        "rm -rf dist/* build",
        "rm -rf .",
        f"rm -rf {ws}",
        f"rm {other}/old.log",
        "find . -name '*.pyc' -delete",
        "npm run build && rm -rf dist",
        "rm -rf",
        "npm test",
    ):
        assert not deletes_beyond(command, ws, roots), command


def test_deletes_beyond_the_roots_or_of_unknown_reach_ask(ws, tmp_path):
    roots = [ws]
    for command in (
        "rm -rf ../x",
        "rm -rf /",
        f"rm -rf {tmp_path}",
        "rm -rf ~/x",
        "rm -rf $DIR",
        "echo x | xargs rm",
        "find / -delete",
    ):
        assert deletes_beyond(command, ws, roots), command


def test_no_roots_means_every_delete_is_beyond(ws):
    assert deletes_beyond("rm -rf dist", ws, [])
    assert deletes_beyond("rm -rf dist", None, [""])
    assert not deletes_beyond("npm test", None, [])


def test_a_relative_delete_with_no_working_directory_is_beyond(ws):
    assert deletes_beyond("rm -rf dist", None, [ws])
    assert not deletes_beyond(f"rm -rf {ws}/dist", None, [ws])


def test_a_cd_before_the_delete_moves_its_relative_operands(ws, tmp_path):
    roots = [ws]
    assert deletes_beyond("cd ~/Documents && rm -rf notes", ws, roots)
    assert not deletes_beyond("cd sub && rm -rf build", ws, roots)
    assert deletes_beyond('cd "$X" && rm -rf y', ws, roots)
    assert not deletes_beyond(f"cd {ws}/dist; rm *", ws, roots)
    # The glob is read in the directory the cd names, not the workspace.
    other = tmp_path / "other"
    other.mkdir()
    assert deletes_beyond(f"cd {other}; rm *", ws, roots)
    assert deletes_beyond("cd ~/Documents\nrm -rf notes", ws, roots)
    assert deletes_beyond("cd ~/Documents && find . -delete", ws, roots)
    assert not deletes_beyond("cd sub && find . -delete", ws, roots)


def test_a_cd_the_command_line_cannot_follow_leaves_no_working_directory(ws):
    roots = [ws]
    for cd in ('cd "$X"', "cd ${HOME}/x", "cd -", "cd", "cd a b", "cd dist*", "cd -2"):
        assert deletes_beyond(f"{cd} && rm -rf build", ws, roots), cd
    for stack in ("pushd sub", "popd"):
        assert deletes_beyond(f"{stack} && rm -rf build", ws, roots), stack
    # An absolute operand needs no working directory.
    assert not deletes_beyond(f'cd "$X" && rm -rf {ws}/dist', ws, roots)
    assert not deletes_beyond(f"popd; rm -rf {ws}/dist", ws, roots)


def test_only_an_and_chain_makes_the_cd_sure(ws, tmp_path):
    """A cd joined to the delete by && alone surely ran before it. After any
    other operator the delete may run where the command started, so both
    directories are checked."""
    other = tmp_path / "other"
    other.mkdir()
    roots = [ws]
    assert not deletes_beyond(f"cd {ws}/dist && rm *", str(other), roots)
    assert not deletes_beyond(f"cd {ws}/dist && rm *", None, roots)
    assert not deletes_beyond(f"cd {ws}/dist &&\nrm *", str(other), roots)
    for unsure in (
        f"cd {ws}/dist; rm *",
        f"cd {ws}/dist\nrm *",
        f"cd {ws}/dist || rm *",
        f"cd {ws}/dist | rm *",
        f"echo | cd {ws}/dist && rm *",
        f"true || cd {ws}/dist && rm *",
        f"! cd {ws}/dist && rm *",
        f"time cd {ws}/dist && rm *",
        f"(cd {ws}/dist) && rm *",
        f"( true && cd {ws}/dist && true ) && rm *",
        f"cd {ws}/dist & rm *",
    ):
        assert deletes_beyond(unsure, str(other), roots), unsure
    assert not deletes_beyond("cd sub && npm run build && cd .. && rm -rf dist", ws, roots)
    assert not deletes_beyond("cd sub && rm -rf ../dist", ws, roots)
    assert deletes_beyond("cd sub; rm -rf ../dist", ws, roots)


def test_cds_in_scripts_subshells_and_loops(ws):
    roots = [ws]
    assert deletes_beyond("bash -c 'cd ~/Documents && rm -rf notes'", ws, roots)
    # A child shell's cd ends with it; an eval's stays.
    assert not deletes_beyond("bash -c 'cd ~/Documents'; rm -rf notes", ws, roots)
    assert deletes_beyond("eval 'cd ~/Documents'; rm -rf notes", ws, roots)
    assert not deletes_beyond("cd sub && bash -c 'true; rm -rf ../dist'", ws, roots)
    assert deletes_beyond("( cd ~/Documents && rm -rf notes )", ws, roots)
    assert deletes_beyond("{ cd ~/Documents; rm -rf notes; }", ws, roots)
    assert deletes_beyond("if cd ~/Documents; then rm -rf notes; fi", ws, roots)
    # The next pass of a loop runs its delete after the cd that follows it.
    assert deletes_beyond("for i in 1 2; do rm -rf build; cd ~/Documents; done", ws, roots)
    assert not deletes_beyond("for i in 1 2; do rm -rf build; done", ws, roots)


def test_a_cd_through_a_symlink_lands_where_the_link_points(ws, tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    os.symlink(target, os.path.join(ws, "link"))
    roots = [ws]
    assert deletes_beyond("cd link && rm -rf x", ws, roots)
    # rm climbs from the real directory, so .. is the link target's parent.
    assert deletes_beyond("cd link && rm -rf ../dist", ws, roots)
    # cd -P (or a shell in physical mode) reads link/.. off the real path.
    assert deletes_beyond("cd link/.. && rm -rf dist", ws, roots)


def test_a_dotdot_after_a_symlink_climbs_from_where_it_points(ws, tmp_path):
    """The kernel follows the link before it reads the .. after it, so
    link/../x is beside the link's target, not beside the link."""
    target = tmp_path / "shared" / "x"
    target.mkdir(parents=True)
    os.symlink(target, os.path.join(ws, "link"))
    beside = os.path.realpath(str(tmp_path / "shared" / "y"))
    assert resolve_operand("link/../y", ws) == beside
    assert resolve_operand("link/../*", ws) == os.path.dirname(beside)
    roots = [ws]
    assert deletes_beyond("rm -rf link/../y", ws, roots)
    assert deletes_beyond("rm -rf link/..", ws, roots)
    assert deletes_beyond("find link/.. -delete", ws, roots)
    # A .. after a real folder climbs back into the workspace as before.
    assert not deletes_beyond("rm -rf dist/../dist", ws, roots)


def test_deletes_in_case_arms_functions_and_behind_precommands_reach_their_operands(ws):
    roots = [ws]
    for command in (
        "case 1 in 1) rm -rf ~/x ;; esac",
        "case $x in a|b) rm -rf ../x;; esac",
        "f() { rm -rf ~/x; }; f",
        "function f { rm -rf ~/x; }; f",
        "noglob rm -rf ~/x",
        "nocorrect rm -rf ../x",
    ):
        assert deletes_beyond(command, ws, roots), command
    for command in (
        "case $1 in clean) rm -rf dist ;; esac",
        "f() { rm -rf dist; }; f",
        "noglob rm -rf dist/*",
    ):
        assert not deletes_beyond(command, ws, roots), command


def test_a_cd_in_a_function_body_leaves_no_working_directory(ws):
    """The body runs at each call, as often as it is called, so its cd may
    run before any delete in the command, the body's own included."""
    roots = [ws]
    for command in (
        "f() { cd ~/Documents; }; f; rm -rf notes",
        "function f { cd ~/Documents; }; f; rm -rf notes",
        "f() {\n  cd ~/Documents\n}\nf\nrm -rf notes",
        "f() { rm -rf notes; cd ~/Documents; }; f; f",
        "f() { cd ..; }; f; f; rm -rf ws",
    ):
        assert deletes_beyond(command, ws, roots), command
    # An absolute operand needs no working directory.
    assert not deletes_beyond(f"f() {{ cd ~/Documents; }}; f; rm -rf {ws}/dist", ws, roots)


def test_a_wrapper_that_changes_directory_moves_its_relative_operands(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    roots = [ws]
    for command in (
        "env -C ~/Documents rm -rf notes",
        "sudo -D ~/Documents rm -rf notes",
        "sudo -u root -D ~/Documents rm -rf notes",
        "env -C .. rm -rf x",
        "env -C ~/Documents find . -delete",
        "sudo -D ~/Documents sh -c 'rm -rf notes'",
        "cd dist && env -C ../.. rm -rf x",
    ):
        assert deletes_beyond(command, ws, roots), command
    # The wrapper does not run the command when its move fails, so the move
    # is sure: the command's own working directory is not where it deletes.
    assert not deletes_beyond("env -C dist rm -rf old", ws, roots)
    assert not deletes_beyond(f"env -C {ws}/dist rm -rf old", str(other), roots)
    assert not deletes_beyond(f"sudo -D {ws} sh -c 'rm -rf dist'", str(other), roots)
    # A folder the line does not name, or another root, always asks for a
    # relative operand; under another root an absolute one asks too.
    for command in (
        'env -C "$D" rm -rf dist',
        "env -C~/x rm -rf dist",
        "sudo -i rm -rf dist",
        "sudo -R / rm -rf dist",
        f"sudo -R / rm -rf {ws}/dist",
    ):
        assert deletes_beyond(command, ws, roots), command
    assert not deletes_beyond(f"sudo -i rm -rf {ws}/dist", ws, roots)


def test_deletes_hidden_in_env_strings_redirections_loops_and_coprocesses_reach_their_operands(ws):
    roots = [ws]
    for command in (
        "env -S 'rm -rf ~/x'",
        "env -S '-C ~/Documents rm -rf notes'",
        "2>/dev/null rm -rf ~/x",
        "npm test; 2>&1 rm -rf ../x",
        "for i (1 2) rm -rf ~/x",
        "repeat 2 rm -rf ../x",
        "coproc rm -rf ~/x",
    ):
        assert deletes_beyond(command, ws, roots), command
    for command in (
        "env -S 'rm -rf dist'",
        "2>/dev/null rm -rf dist",
        "repeat 2 rm -rf dist/*",
        "for f in rm cp; do echo $f; done",
    ):
        assert not deletes_beyond(command, ws, roots), command


def test_a_trap_delete_runs_after_every_later_cd(ws):
    """A trap's script runs at a signal or at exit, after any cd that
    follows it, so with a cd anywhere in the command its relative operands
    have no working directory the line names."""
    roots = [ws]
    assert deletes_beyond("trap 'rm -rf ~/x' EXIT", ws, roots)
    assert not deletes_beyond("trap 'rm -rf dist' EXIT", ws, roots)
    assert deletes_beyond("trap 'rm -rf dist' EXIT; cd ~/Documents", ws, roots)
    assert deletes_beyond("trap 'cd ~/Documents' EXIT; rm -rf notes", ws, roots)
    assert not deletes_beyond(f"trap 'rm -rf {ws}/dist' EXIT; cd ~/Documents", ws, roots)


def test_a_delete_run_as_another_user_asks_where_the_line_cannot_say(ws):
    roots = [ws]
    for command in (
        "su root -c 'rm -rf ~/x'",
        "su - root -c 'rm -rf dist'",
        "sudo sh -c 'rm -rf ~/x'",
        "sudo sh -c 'cd ~/web && rm -rf dist'",
        "chroot / rm -rf /tmp/x",
        f"chroot / rm -rf {ws}/dist",
    ):
        assert deletes_beyond(command, ws, roots), command
    for command in (
        "su root -c 'rm -rf dist'",
        f"su - root -c 'rm -rf {ws}/dist'",
        "sudo sh -c 'rm -rf dist'",
    ):
        assert not deletes_beyond(command, ws, roots), command
