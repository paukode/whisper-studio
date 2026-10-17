"""The user's global instructions: ~/.whisper/WHISPER.md and ~/.whisper/rules/global/.

Both load whole into the cached static prefix (the ``user_rules`` prompt
section). Files are read as they are (markdown, no comment stripping: a
heading in a rules file is content), capped per file, and cached on the set
of (path, mtime) pairs so an edit applies on the next request.
"""

import os
import time

from server.infrastructure.paths import user_root
from server.prompts import rules as R


def _reset():
    R._cache["key"] = None
    R._cache["text"] = ""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_paths_live_under_the_user_root():
    root = user_root()
    assert R.global_profile_path() == os.path.join(root, "WHISPER.md")
    assert R.global_rules_dir() == os.path.join(root, "rules", "global")


def test_nothing_on_disk_is_no_rules():
    _reset()
    assert R.load_global_instructions() == ""
    assert R.rules_block() == ""
    assert R.append_rules("BASE") == "BASE"


def test_profile_and_rules_load_with_their_labels():
    _reset()
    _write(R.global_profile_path(), "I am a data engineer.\n")
    _write(os.path.join(R.global_rules_dir(), "rules.md"), "# Global rules\n\n- No emoji.\n")
    _write(os.path.join(R.global_rules_dir(), "writing.md"), "- Short sentences.\n")

    text = R.load_global_instructions()
    assert "I am a data engineer." in text
    assert "[~/.whisper/WHISPER.md, about the user and how they work]" in text
    # Markdown is content: the heading survives, and rules.md comes first.
    assert "# Global rules" in text
    assert text.index("No emoji.") < text.index("Short sentences.")
    assert "[rules/global/writing.md, global rules]" in text

    block = R.rules_block()
    assert block.startswith("\n\n## The user's instructions")
    assert R.append_rules("BASE").startswith("BASE\n\n## The user's instructions")


def test_profile_is_not_loaded_twice_as_a_rules_file():
    """A WHISPER.md dropped into rules/global/ is ignored there: the profile
    has its own slot and loading it twice bills the same text twice."""
    _reset()
    _write(os.path.join(R.global_rules_dir(), "WHISPER.md"), "stray profile")
    _write(os.path.join(R.global_rules_dir(), "rules.md"), "- A rule.")
    text = R.load_global_instructions()
    assert "stray profile" not in text
    assert "A rule." in text


def test_edits_apply_without_a_restart():
    _reset()
    path = os.path.join(R.global_rules_dir(), "rules.md")
    _write(path, "- First.")
    assert "First." in R.load_global_instructions()
    _write(path, "- Second.")
    # A same-second rewrite shares the mtime; push it forward explicitly.
    later = time.time() + 5
    os.utime(path, (later, later))
    text = R.load_global_instructions()
    assert "Second." in text and "First." not in text


def test_an_oversized_file_is_capped():
    _reset()
    _write(os.path.join(R.global_rules_dir(), "rules.md"), "x" * (R.MAX_FILE_CHARS * 3))
    text = R.load_global_instructions()
    assert len(text) < R.MAX_FILE_CHARS + 500
    assert "truncated at" in text
