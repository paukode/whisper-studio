"""Tests for the egress-policy wiring into server/sandbox.py.

The single most important test here is
`test_default_permissive_matches_pre_egress_policy_behavior`: this feature
ships default-off, so proving the default tier changes NOTHING about
existing behavior (no proxy started, no env injected, profile byte-for-byte
identical) is the regression gate that matters most.
"""

import server.sandbox as sandbox
from server.security import egress_policy


def _permissive_policy():
    return {"tier": "permissive", "custom_allowed_domains": [], "methods_only_safe": False}


def _curated_policy(domains=None):
    return {
        "tier": "curated",
        "custom_allowed_domains": domains or [],
        "methods_only_safe": False,
    }


# ── default (permissive) behavior is unchanged ──────────────────────────────


def test_permissive_injects_no_proxy_env_vars(monkeypatch):
    monkeypatch.setattr(egress_policy, "get_active_policy", _permissive_policy)

    def _fail_if_called():
        raise AssertionError("ensure_proxy_running must not be called under permissive policy")

    monkeypatch.setattr(egress_policy, "ensure_proxy_running", lambda: _fail_if_called())

    env = sandbox._merged_env(None)
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert key not in env


def test_permissive_macos_profile_has_no_network_rules(monkeypatch):
    monkeypatch.setattr(egress_policy, "get_active_policy", _permissive_policy)
    assert sandbox._macos_network_restriction_rules() == []


def test_permissive_bwrap_args_are_empty(monkeypatch):
    monkeypatch.setattr(egress_policy, "get_active_policy", _permissive_policy)
    assert sandbox._bwrap_network_restriction_args() == []


def test_default_permissive_matches_pre_egress_policy_behavior(monkeypatch):
    """The regression gate: with no permissions.json override (i.e. real
    defaults), _generate_macos_profile's output must be byte-for-byte
    identical to what it produced before this feature existed — proven here
    by asserting no network rules get appended and the merged env carries no
    proxy vars, for a real (unmocked) default policy read."""
    # Force a clean "nothing saved" read rather than depending on whatever
    # happens to be in this machine's real permissions.json.
    monkeypatch.setattr("server.security.permissions.load_permissions", lambda: {})

    profile = sandbox._generate_macos_profile("/tmp/some-workspace")
    assert "network" not in profile.lower()
    assert "443" not in profile
    assert profile.endswith("\n")
    assert not profile.endswith("\n\n")  # no stray blank line from an empty appended block

    env = sandbox._merged_env(None)
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert key not in env


# ── non-permissive tiers actually change behavior ───────────────────────────


def test_merged_env_injects_proxy_vars_when_non_permissive(monkeypatch):
    monkeypatch.setattr(egress_policy, "get_active_policy", lambda: _curated_policy())
    monkeypatch.setattr(egress_policy, "ensure_proxy_running", lambda: ("127.0.0.1", 54321))

    env = sandbox._merged_env(None)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:54321"
    assert env["https_proxy"] == "http://127.0.0.1:54321"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:54321"
    assert env["http_proxy"] == "http://127.0.0.1:54321"


def test_env_extra_overrides_proxy_vars(monkeypatch):
    # _merged_env applies env_extra last, so an explicit caller override wins.
    monkeypatch.setattr(egress_policy, "get_active_policy", lambda: _curated_policy())
    monkeypatch.setattr(egress_policy, "ensure_proxy_running", lambda: ("127.0.0.1", 54321))

    env = sandbox._merged_env({"HTTPS_PROXY": "http://custom:9"})
    assert env["HTTPS_PROXY"] == "http://custom:9"


def test_credential_denylist_still_enforced_alongside_proxy_injection(monkeypatch):
    monkeypatch.setattr(egress_policy, "get_active_policy", lambda: _curated_policy())
    monkeypatch.setattr(egress_policy, "ensure_proxy_running", lambda: ("127.0.0.1", 54321))
    monkeypatch.setenv("GH_TOKEN", "should-never-appear")

    env = sandbox._merged_env(None)
    assert "GH_TOKEN" not in env
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:54321"


def test_macos_profile_appends_network_deny_rules_when_curated(monkeypatch):
    monkeypatch.setattr(egress_policy, "get_active_policy", lambda: _curated_policy())
    rules = sandbox._macos_network_restriction_rules()
    assert any("443" in r and "deny" in r for r in rules)
    assert any("80" in r and "deny" in r for r in rules)

    profile = sandbox._generate_macos_profile("/tmp/some-workspace")
    assert '(deny network-outbound (remote ip "*:443"))' in profile
    assert '(deny network-outbound (remote ip "*:80"))' in profile


def test_macos_profile_network_rules_come_after_the_deny_paths_block(monkeypatch):
    monkeypatch.setattr(egress_policy, "get_active_policy", lambda: _curated_policy())
    profile = sandbox._generate_macos_profile("/tmp/some-workspace")
    assert profile.index("allow default") < profile.index('remote ip "*:443"')


def test_bwrap_args_include_unshare_net_when_restrictive(monkeypatch):
    monkeypatch.setattr(
        egress_policy,
        "get_active_policy",
        lambda: {"tier": "restrictive", "custom_allowed_domains": [], "methods_only_safe": False},
    )
    assert sandbox._bwrap_network_restriction_args() == ["--unshare-net"]


def test_run_sandboxed_no_sandbox_fallback_still_gets_proxy_env(monkeypatch, tmp_path):
    """Even the "no OS sandbox available" fallback path in run_sandboxed goes
    through _merged_env, so it should still see proxy vars under a
    non-permissive policy — sanity-checking the "no new plumbing needed at
    the call site" wiring end to end for that branch."""
    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: False)
    monkeypatch.setattr(sandbox, "_is_bwrap_available", lambda: False)
    monkeypatch.setattr(egress_policy, "get_active_policy", lambda: _curated_policy())
    monkeypatch.setattr(egress_policy, "ensure_proxy_running", lambda: ("127.0.0.1", 54321))

    result = sandbox.run_sandboxed("echo $HTTPS_PROXY", cwd=str(tmp_path), timeout=10)
    assert result.stdout.strip() == "http://127.0.0.1:54321"


def test_run_sandboxed_default_permissive_end_to_end(tmp_path):
    """No monkeypatching of the policy here at all — exercises the REAL
    default read path (permissions.json likely doesn't even have a
    network_policy key on a fresh install) to prove a plain run_sandboxed
    call behaves exactly as it did before this feature existed."""
    result = sandbox.run_sandboxed("echo hello-world", cwd=str(tmp_path), timeout=10)
    assert result.returncode == 0
    assert "hello-world" in result.stdout


# ── proxy singleton lifecycle ────────────────────────────────────────────────


def test_ensure_proxy_running_is_idempotent():
    egress_policy._reset_proxy_for_tests()
    try:
        addr1 = egress_policy.ensure_proxy_running()
        addr2 = egress_policy.ensure_proxy_running()
        assert addr1 == addr2
    finally:
        egress_policy._reset_proxy_for_tests()


def test_proxy_binds_to_loopback_ephemeral_port():
    egress_policy._reset_proxy_for_tests()
    try:
        host, port = egress_policy.ensure_proxy_running()
        assert host == "127.0.0.1"
        assert port not in (80, 443)
        assert port > 0
    finally:
        egress_policy._reset_proxy_for_tests()
