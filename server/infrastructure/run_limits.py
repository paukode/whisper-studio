"""The round limit and time limit every run takes from Settings.

One control, the way ``bedrock_region`` is one control for every model call:
Settings > Costs > Budget, config keys ``round_limit`` and
``time_limit_minutes`` (defaults in server/infrastructure/config.py DEFAULTS).
Every run reads the round limit: chat turns, voice, every agent type, cron and
headless runs. The time limit applies where nobody is watching: agents, cron
and unattended headless runs. Both are read when a run starts, so a change in
Settings applies to the next run.

The exceptions, chosen on 2026-10-04:

- chat and voice have no time limit: the user is there, and Stop is the brake;
- a coordinator gets twice the time limit, since it waits on the agents it
  starts (AgentConfig.time_limit_factor);
- the background memory jobs (extraction, dream, session summary) keep their
  own small round caps (AgentConfig.internal), and two single steps inside a
  turn keep theirs: the learning review (server/memory/review_fork.py) and the
  wake answer (server/agents/wake.py).
"""


def _positive_int(value, key: str) -> int:
    """``value`` as a positive whole number; anything else (a hand-edited
    string, zero, a negative, a boolean) reads as the shipped default."""
    from server.infrastructure.config import DEFAULTS

    if not isinstance(value, bool):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            return number
    return int(DEFAULTS[key])


def round_limit() -> int:
    """How many model rounds any run may take."""
    from server.infrastructure.config import load_config

    return _positive_int(load_config().get("round_limit"), "round_limit")


def time_limit_seconds() -> float:
    """The wall-clock budget, in seconds, of a run nobody is watching."""
    from server.infrastructure.config import load_config

    minutes = _positive_int(load_config().get("time_limit_minutes"), "time_limit_minutes")
    return 60.0 * minutes
