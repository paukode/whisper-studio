"""The `companion` feature flag must default to OFF so a fresh install
doesn't burn a Claude call after every assistant turn. This test pins
the default and ensures the registry contains the flag with the right
metadata — if anyone re-flips the default to True without updating the
docs/tests, this fails loudly.
"""

from server.infrastructure.feature_flags import (
    get_all_flags,
    get_flag,
    is_enabled,
)


def test_companion_flag_is_registered():
    flag = get_flag("companion")
    assert flag is not None, "companion flag should be registered"
    assert flag.category == "ui"


def test_companion_flag_default_is_off():
    flag = get_flag("companion")
    assert flag is not None
    assert flag.default is False, (
        "companion default must be False — otherwise every fresh install "
        "auto-fires Claude calls after each assistant turn without consent."
    )


def test_companion_appears_in_full_registry():
    flags = get_all_flags()
    assert "companion" in flags


def test_is_enabled_resolves_to_default_when_unset(monkeypatch):
    """When config.json has no `feature_flags.companion` override, the
    resolver should fall back to the registered default (False).

    Note: monkeypatching ``chdir`` is NOT enough — ``CONFIG_PATH`` is
    a module-level constant computed from the file's location, so it
    points at the project's real config.json regardless of cwd. The
    old version of this test made that mistake and silently passed
    only on machines where companion was unset. We instead stub
    ``load_config`` directly to return a clean dict.
    """
    from server.infrastructure import config as cfg

    monkeypatch.setattr(cfg, "load_config", lambda *_args, **_kwargs: {})
    expected = get_flag("companion").default
    assert is_enabled("companion") == expected
