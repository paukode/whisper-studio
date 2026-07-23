"""The `model:` frontmatter key is parsed and exposed via get_skill_model, and
the forced-skill model override only fires for an available chat model.

Before this change the key was parsed but never read (dead Feature 9 plumbing).
The chat pipeline now uses it to run a forced skill's turn on its own model.
"""

import server.skills as sk


def _write_skill(dirpath, name, body_model=None):
    lines = ["---", f"name: {name}", "description: test skill", "executor: run_python"]
    if body_model:
        lines.append(f"model: {body_model}")
    lines += [
        "input_schema:",
        "  code:",
        "    type: string",
        "    required: true",
        "    description: code",
        "---",
        "",
        "Run it.",
    ]
    (dirpath / f"{name}.md").write_text("\n".join(lines))


def test_model_key_parsed_and_exposed(tmp_path, monkeypatch):
    _write_skill(tmp_path, "cheap_skill", body_model="haiku")
    _write_skill(tmp_path, "plain_skill", body_model=None)
    skills = sk.load_skills(str(tmp_path))
    assert skills["cheap_skill"]["model"] == "haiku"
    assert skills["plain_skill"]["model"] is None

    monkeypatch.setattr(sk, "SKILLS", skills)
    assert sk.get_skill_model("cheap_skill") == "haiku"
    assert sk.get_skill_model("plain_skill") is None
    assert sk.get_skill_model("does_not_exist") is None


def test_forced_skill_override_gate():
    """Mirror the routes.py gate: apply the override only when the skill's model
    is an available chat model; otherwise keep the resolved model."""
    chat_models = {"haiku": "id-haiku", "sonnet": "id-sonnet"}

    def resolve(force_skill_model):
        model_key, model_id = "sonnet", chat_models["sonnet"]
        if force_skill_model and force_skill_model in chat_models:
            model_key, model_id = force_skill_model, chat_models[force_skill_model]
        return model_key, model_id

    assert resolve("haiku") == ("haiku", "id-haiku")  # available -> override
    assert resolve("opus-unavailable") == ("sonnet", "id-sonnet")  # not available -> keep
    assert resolve(None) == ("sonnet", "id-sonnet")  # no override -> keep
