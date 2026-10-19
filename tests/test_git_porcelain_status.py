"""`git status --porcelain` parsing keeps the index column of every entry.

Stripping the whole output removed the leading space of the first entry, so
" M first.txt" (changed in the worktree only) read as "M " and the Git panel
listed it under Staged.
"""

import subprocess

import server.git.core as git_core
from server.git.core import STATUS_PORCELAIN_ARGS, parse_porcelain_status


def _fake_git(stdout: str):
    def run(args, cwd, **_kwargs):
        assert args == STATUS_PORCELAIN_ARGS
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def test_first_entry_keeps_its_blank_index_column():
    entries = parse_porcelain_status(" M first.txt\0 M second.txt\0M  staged.txt\0")
    assert entries == [(" M", "first.txt"), (" M", "second.txt"), ("M ", "staged.txt")]


def test_paths_with_spaces_are_unquoted():
    entries = parse_porcelain_status(" M sp ace.txt\0?? new dir/\0")
    assert entries == [(" M", "sp ace.txt"), ("??", "new dir/")]


def test_rename_reports_the_new_path_and_skips_the_source():
    entries = parse_porcelain_status("R  new.txt\0old.txt\0 M after.txt\0")
    assert entries == [("R ", "new.txt"), (" M", "after.txt")]


def test_empty_output_has_no_entries():
    assert parse_porcelain_status("") == []


def test_get_file_status_reads_the_first_unstaged_entry(monkeypatch):
    monkeypatch.setattr(git_core, "_run_git", _fake_git(" M first.txt\0?? sp ace.txt\0"))
    assert git_core.get_file_status("/repo") == {
        "tracked": ["first.txt"],
        "untracked": ["sp ace.txt"],
    }
