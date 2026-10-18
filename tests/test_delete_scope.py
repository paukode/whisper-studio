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
