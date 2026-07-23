"""Tests for the user-editable global prompt rules (PROMPT_RULES.md)."""

import importlib

import server.prompts.rules as R


def _reload_with(monkeypatch, tmp_path, contents: str | None):
    """Point the loader at a temp rules file (or none) and clear its cache."""
    if contents is None:
        path = tmp_path / "missing.md"
    else:
        path = tmp_path / "PROMPT_RULES.md"
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(R, "RULES_PATH", str(path))
    monkeypatch.setattr(R, "_cache", {"text": "", "mtime": -1.0})
    return R


def test_strips_comments_and_blanks(monkeypatch, tmp_path):
    r = _reload_with(
        monkeypatch, tmp_path, "# a note\n#\nRule one.\n\n  Rule two.  \n# trailing note\n"
    )
    assert r.load_prompt_rules() == "Rule one.\nRule two."


def test_missing_file_is_no_rules(monkeypatch, tmp_path):
    r = _reload_with(monkeypatch, tmp_path, None)
    assert r.load_prompt_rules() == ""
    assert r.rules_block() == ""
    assert r.append_rules("BASE") == "BASE"


def test_empty_file_is_no_rules(monkeypatch, tmp_path):
    r = _reload_with(monkeypatch, tmp_path, "# only comments\n#\n")
    assert r.load_prompt_rules() == ""
    assert r.append_rules("BASE") == "BASE"


def test_rules_block_and_append(monkeypatch, tmp_path):
    r = _reload_with(monkeypatch, tmp_path, "No emojis.\n")
    block = r.rules_block()
    assert "Output rules" in block and "No emojis." in block
    out = r.append_rules("BASE PROMPT")
    assert out.startswith("BASE PROMPT")
    assert "No emojis." in out
