"""A search stopped at its time limit says so.

rg prints nothing when it is stopped, so a field session read a bare
"Search error (exit -15): " after a grep over a parent folder that held
large snapshot files, and had nothing to change.
"""

from __future__ import annotations

import sys

from server.search import engine


def test_a_stopped_search_says_it_timed_out_and_how_to_narrow_it(monkeypatch, tmp_path):
    # A stand-in for rg that never finishes and stops quietly on SIGTERM.
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(engine, "get_rg_path", lambda: sys.executable)
    monkeypatch.setattr(engine, "_SIGKILL_DELAY", 5)
    stdout, stderr, code = engine.rg_raw([str(slow)], str(tmp_path), timeout=1)
    assert code != 0 and stdout == ""
    assert stderr.startswith("the search timed out after 1 s and was stopped")
    assert "glob inside the project" in stderr
