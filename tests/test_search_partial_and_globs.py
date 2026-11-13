"""ws_grep/ws_glob keep the results rg found when some paths are unreadable,
type + glob combine into one AND filter, and the bundled rg is found next to
the interpreter."""

from __future__ import annotations

import os
import sys

import pytest

from server.search import engine, glob, grep


def _rg_returns(monkeypatch, module, stdout, stderr, code):
    monkeypatch.setattr(module, "get_workspace_path", lambda: "/ws")
    monkeypatch.setattr(module, "rg_raw", lambda args, cwd: (stdout, stderr, code))


def test_grep_keeps_matches_when_a_path_is_unreadable(monkeypatch):
    _rg_returns(
        monkeypatch, grep, "ok/a.txt\n", "rg: ./locked: Permission denied (os error 13)\n", 2
    )
    out = grep.exec_ws_grep({"pattern": "needle"}, "", [])
    assert out.startswith("ok/a.txt")
    assert "could not be read" in out and "./locked" in out


def test_grep_reports_an_error_with_no_output(monkeypatch):
    _rg_returns(monkeypatch, grep, "", "rg: bad regex\n", 2)
    assert grep.exec_ws_grep({"pattern": "("}, "", []).startswith("Search error (exit 2)")


def test_glob_keeps_files_when_a_path_is_unreadable(monkeypatch):
    _rg_returns(monkeypatch, glob, "ok/a.txt\n", "rg: ./locked: Permission denied\n", 2)
    out = glob.exec_ws_glob({"pattern": "*.txt"}, "", [])
    assert out.startswith("ok/a.txt") and "could not be read" in out


def _globs(args):
    return [
        args[i + 1]
        for i, a in enumerate(args)
        if a == "--glob" and not args[i + 1].startswith("!.")
    ]


@pytest.mark.parametrize(
    ("glob_filter", "expected"),
    [
        ("server", ["server/**/*.py", "server/**/*.pyi"]),
        ("server/", ["server/**/*.py", "server/**/*.pyi"]),
        ("server/**", ["server/**/*.py", "server/**/*.pyi"]),
    ],
)
def test_type_and_folder_glob_combine(monkeypatch, glob_filter, expected):
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: ["*.py", "*.pyi"])
    args = grep._build_grep_args("x", glob_filter, "files_with_matches", file_type="py")
    assert _globs(args) == expected
    assert "--type" not in args


@pytest.mark.parametrize("glob_filter", ["*.test.ts", "src/**/*.{ts,tsx}", "src/*.ts"])
def test_a_file_glob_of_the_type_is_kept_as_it_is(monkeypatch, glob_filter):
    # The field failure: type "ts" with glob "server/**/*.ts" was refused as
    # "already names files", and the model had to send the call again.
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: ["*.ts"])
    args = grep._build_grep_args("x", glob_filter, "content", file_type="ts")
    assert _globs(args) == [glob_filter]
    assert "--type" not in args


def test_a_file_glob_of_another_type_is_refused(monkeypatch):
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: ["*.py", "*.pyi"])
    with pytest.raises(ValueError, match="does not include"):
        grep._build_grep_args("x", "src/**/*.tsx", "content", file_type="py")


def test_a_name_glob_takes_the_extensions_of_the_type(monkeypatch):
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: ["*.py", "*.pyi"])
    args = grep._build_grep_args("x", "tests/test_*", "content", file_type="py")
    assert _globs(args) == ["tests/test_*.py", "tests/test_*.pyi"]


def test_an_extension_given_as_the_type_is_searched_as_that_extension(monkeypatch):
    # "tsx" is not a ripgrep type (its "ts" type holds *.tsx).
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: [])
    alone = grep._build_grep_args("x", None, "content", file_type="tsx")
    assert _globs(alone) == ["*.tsx"] and "--type" not in alone
    with_folder = grep._build_grep_args("x", "src", "content", file_type="tsx")
    assert _globs(with_folder) == ["src/**/*.tsx"]


def test_a_known_type_alone_goes_to_ripgrep_as_a_type(monkeypatch):
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: ["*.py"])
    args = grep._build_grep_args("x", None, "content", file_type="py")
    assert args[args.index("--type") + 1] == "py" and not _globs(args)


def test_type_with_only_an_exclusion_keeps_the_type(monkeypatch):
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: ["*.py"])
    args = grep._build_grep_args("x", "!vendor", "content", file_type="py")
    assert "!vendor" in args and args[args.index("--type") + 1] == "py"


def test_unknown_type_with_a_glob_is_an_error(monkeypatch):
    monkeypatch.setattr(grep, "get_workspace_path", lambda: "/ws")
    monkeypatch.setattr(grep, "_resolve_type_globs", lambda t: [])
    out = grep.exec_ws_grep({"pattern": "x", "type": "no.pe", "glob": "src/**"}, "", [])
    assert out.startswith("Error: unknown file type 'no.pe'")


def test_rg_is_found_next_to_the_interpreter(monkeypatch, tmp_path):
    rg = tmp_path / "rg"
    rg.write_text("#!/bin/sh\n")
    rg.chmod(0o755)
    monkeypatch.delenv("WHISPER_RG_PATH", raising=False)
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python3"))
    engine.get_rg_path.cache_clear()
    try:
        assert engine.get_rg_path() == os.fspath(rg)
    finally:
        engine.get_rg_path.cache_clear()
