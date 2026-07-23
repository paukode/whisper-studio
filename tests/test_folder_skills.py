"""Tests for folder-based skills: discovery, injection, asset serving, the git
importer's copy/validation logic, and trusted-skill auto-approval gating."""

import os
import textwrap

import pytest

from server import folder_skills, skills, skills_import, skills_routes


def _make_folder_skill(root, name="hello-folder", *, desc_block=True, with_script=True):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(d, "references"), exist_ok=True)
    desc = (
        "description: |\n  Line one.\n  Line two.\n" if desc_block else "description: one liner\n"
    )
    fm = f'---\nname: {name}\n{desc}allowed-tools:\n  - "python3 scripts/"\nlicense: MIT\n---\n'
    body = "Run `python3 scripts/hello.py` or ${CLAUDE_SKILL_DIR}/scripts/hello.py.\nSee references/notes.md.\n"
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(fm + body)
    if with_script:
        with open(os.path.join(d, "scripts", "hello.py"), "w") as f:
            f.write("print('hi')\n")
    with open(os.path.join(d, "references", "notes.md"), "w") as f:
        f.write("# notes\nshould not become a skill\n")
    return d


# ── discovery + parsing ──────────────────────────────────────────────────────


def test_folder_skill_discovered_alongside_md(tmp_path):
    root = str(tmp_path)
    _make_folder_skill(root)
    with open(os.path.join(root, "simple.md"), "w") as f:
        f.write("---\nname: simple_skill\ndescription: plain\n---\nbody\n")
    loaded = skills.load_skills(root)
    assert "hello_folder" in loaded
    assert "simple_skill" in loaded
    hf = loaded["hello_folder"]
    assert hf["is_folder"] and hf["has_scripts"]
    # a folder skill's references/*.md must NOT become its own skill
    assert "notes" not in loaded


def test_multiline_block_scalar_description(tmp_path):
    _make_folder_skill(str(tmp_path), desc_block=True)
    loaded = skills.load_skills(str(tmp_path))
    desc = loaded["hello_folder"]["description"]
    assert "Line one." in desc and "Line two." in desc
    assert desc.strip() != "|"


def test_frontmatter_flat_fallback_handles_block_scalar():
    fm, body = folder_skills.parse_frontmatter(
        "---\nname: x\ndescription: |\n  a\n  b\n---\nbody\n"
    )
    assert fm["name"] == "x"
    assert "a" in fm["description"] and "b" in fm["description"]


# ── injection ────────────────────────────────────────────────────────────────


def test_injection_resolves_paths_and_preamble(tmp_path, monkeypatch):
    root = str(tmp_path)
    d = _make_folder_skill(root)
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    skills.SKILLS = skills.load_skills(root)
    out = skills._tool_run_skill("hello_folder", {"input": "go", "__session_id__": "s1"})
    real = os.path.realpath(d)
    assert f"SKILL DIRECTORY: {real}" in out
    assert f"{real}/scripts/hello.py" in out  # ${CLAUDE_SKILL_DIR} + bare ref resolved
    assert f"{real}/references/notes.md" in out
    assert "__session_id__" not in out  # internal key stripped from USER REQUEST


def test_rewrite_body_leaves_escaping_refs_untouched(tmp_path):
    d = _make_folder_skill(str(tmp_path))
    real = os.path.realpath(d)
    body = "bad scripts/../../etc/passwd here"
    out = folder_skills.rewrite_body(body, real)
    # An escaping ref is not rewritten at all: the skill dir is never prepended,
    # so the body is returned unchanged.
    assert out == body
    assert real not in out


# ── asset serving guards ─────────────────────────────────────────────────────


def test_asset_listing_and_traversal_guard(tmp_path, monkeypatch):
    root = str(tmp_path)
    _make_folder_skill(root)
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    skills.SKILLS = skills.load_skills(root)
    sd = skills_routes._folder_skill_dir("hello_folder")
    assert sd is not None
    paths = [f["path"] for f in skills_routes._list_asset_files(sd)]
    assert "SKILL.md" in paths and "scripts/hello.py" in paths
    assert skills_routes._safe_asset_file(sd, "scripts/hello.py") is not None
    assert skills_routes._safe_asset_file(sd, "../../../../etc/passwd") is None
    assert skills_routes._safe_asset_file(sd, "/etc/passwd") is None


# ── importer logic (no network) ──────────────────────────────────────────────


def test_tree_grouping():
    paths = [
        "README.md",
        "eng/arch/SKILL.md",
        "eng/arch/scripts/a.py",
        "eng/arch/scripts/b.py",
        "mkt/aeo/SKILL.md",
    ]
    dirs = skills_import._skill_dirs_from_tree(paths)
    assert set(dirs) == {"eng/arch", "mkt/aeo"}
    assert "eng/arch/scripts/a.py" in dirs["eng/arch"]


def test_import_one_copies_skips_symlinks_and_dedupes(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    src = checkout / "eng" / "arch"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: arch\ndescription: d\n---\nbody\n")
    (src / "scripts" / "a.py").write_text("print(1)\n")
    os.symlink("/etc/passwd", str(src / "evil"))

    dest_root = tmp_path / "skills"
    dest_root.mkdir()
    monkeypatch.setattr(skills_import, "SKILLS_DIR", str(dest_root))

    r1 = skills_import._import_one(str(checkout), "eng/arch", overwrite=False)
    assert r1["status"] == "imported"
    imported = dest_root / "arch"
    assert (imported / "SKILL.md").is_file()
    assert (imported / "scripts" / "a.py").is_file()
    assert not (imported / "evil").exists()  # symlink skipped

    r2 = skills_import._import_one(str(checkout), "eng/arch", overwrite=False)
    assert r2["status"] == "conflict"
    r3 = skills_import._import_one(str(checkout), "eng/arch", overwrite=True)
    assert r3["status"] == "imported"
    assert any(p.name.startswith("arch.bak-") for p in dest_root.iterdir())


def test_import_refuses_reserved_name(tmp_path, monkeypatch):
    checkout = tmp_path / "co"
    src = checkout / "terminal_run"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: terminal_run\ndescription: d\n---\nb\n")
    monkeypatch.setattr(skills_import, "SKILLS_DIR", str(tmp_path / "sk"))
    (tmp_path / "sk").mkdir()
    r = skills_import._import_one(str(checkout), "terminal_run", overwrite=False)
    assert r["status"] == "error" and "reserved" in r["reason"]


def test_import_rejects_bad_url():
    with pytest.raises(skills_import.SkillImportError):
        skills_import.import_skills("file:///etc", ["x"], overwrite=False)


def test_fetch_descriptions_degrades_gracefully(tmp_path):
    # Not a git repo → sparse-checkout fails → returns {} without raising.
    assert skills_import._fetch_descriptions(str(tmp_path), ["eng/arch"]) == {}


def test_description_parsed_from_skill_md():
    fm, _ = folder_skills.parse_frontmatter(
        "---\nname: x\ndescription: When adding feature flags. Triggers on 'add a flag'.\n---\nbody\n"
    )
    assert "feature flags" in fm["description"]


# ── trusted-skill auto-approval gating ───────────────────────────────────────


def test_command_runs_trusted_skill(tmp_path, monkeypatch):
    root = str(tmp_path)
    _make_folder_skill(root, name="agenthub")
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    monkeypatch.setattr(skills, "SKILLS_CONFIG_PATH", os.path.join(root, "skills_config.json"))
    skills.SKILLS = skills.load_skills(root)
    sd = skills.SKILLS["agenthub"]["skill_dir"]
    cmd = f"python3 {sd}/scripts/hello.py"

    assert skills.command_runs_trusted_skill(cmd) is False  # untrusted
    skills.save_skills_config({"disabled": [], "trusted": ["agenthub"]})
    assert skills.command_runs_trusted_skill(cmd) is True
    assert skills.command_runs_trusted_skill("python3 /etc/evil.py") is False
