"""Agents must inherit the session-selected model, not hardcode per-type ones.

This guards against re-introducing things like `explore -> haiku` or
`general -> opus4.7` in AGENT_TYPES. The session model is threaded into every
spawn path as `model_id_override`; each AgentConfig's `model` must stay None.
"""

from server.agents.config import AGENT_TYPES


def test_no_agent_type_hardcodes_a_model():
    offenders = {name: cfg.model for name, cfg in AGENT_TYPES.items() if cfg.model is not None}
    assert not offenders, (
        "Agent types must not hardcode a model — they inherit the session model "
        f"via model_id_override. Offenders: {offenders}"
    )
