"""One malformed skill file must not brick the whole skill loader.

Regression: load_skills / _find_skill_file did ``content.index("---", 3)``
with no guard. A skill .md that opens with ``---`` but never closes the
frontmatter raised ValueError, aborting the loader. Because init_skills runs
in the FastAPI lifespan, a single bad file bricked server boot.
"""

from server import skills


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def test_malformed_file_is_skipped_valid_ones_still_load(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()

    # Valid skill with closed frontmatter.
    _write(
        d / "good.md",
        "---\nname: good_skill\ndescription: a fine skill\ntriggers: hello, hi\n---\n\nBody text.\n",
    )
    # Malformed: opens with `---` but never closes it.
    _write(
        d / "bad.md",
        "---\nname: bad_skill\ndescription: never closes\n\nBody with no closing fence.\n",
    )
    # Second valid skill, to prove the loader keeps going past the bad one
    # regardless of alphabetical ordering.
    _write(
        d / "zeta.md",
        "---\nname: zeta_skill\ndescription: another fine skill\n---\n\nZeta body.\n",
    )

    loaded = skills.load_skills(str(d))  # must not raise

    assert "good_skill" in loaded
    assert "zeta_skill" in loaded
    # The malformed file is skipped entirely.
    assert "bad_skill" not in loaded


def test_only_malformed_file_yields_empty_without_raising(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    _write(d / "bad.md", "---\nname: lonely_bad\nno closing fence here\n")

    loaded = skills.load_skills(str(d))  # must not raise
    assert loaded == {}


def test_find_skill_file_tolerates_malformed(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    _write(
        d / "good.md",
        "---\nname: findable\ndescription: ok\n---\n\nbody\n",
    )
    _write(d / "bad.md", "---\nname: broken\nunterminated\n")

    monkeypatch.setattr(skills, "SKILLS_DIR", str(d))
    # Should locate the good file and not raise on the malformed one.
    assert skills._find_skill_file("findable") == "good.md"
    # An unterminated-frontmatter file is never returned as a match.
    assert skills._find_skill_file("broken") is None
