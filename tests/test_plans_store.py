"""Tests for the plan-document store (server/plans/store.py)."""

from server.plans import store


def test_roundtrip_and_session_scoped_list(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PLANS_DIR", str(tmp_path))
    saved = store.write_plan("sess-abcdef12", "My Test Plan", "# Plan\n\nbody")
    assert saved["id"] == "my-test-plan-sessabcd"
    assert store.read_plan(saved["id"]).startswith("# Plan")
    ids = [p["id"] for p in store.list_plans("sess-abcdef12")]
    assert saved["id"] in ids
    # A different session must not see this plan.
    assert store.list_plans("other-99999999") == []


def test_path_traversal_and_bad_ids_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PLANS_DIR", str(tmp_path))
    assert store.read_plan("../../etc/passwd") is None
    assert store.read_plan("/etc/passwd") is None
    assert store.read_plan("has spaces") is None
    assert store.read_plan("missing-plan-xxxx") is None


def test_plan_id_is_slug_plus_session_tag():
    assert store.plan_id_for("abc12345xyz", "My Cool Plan!!") == "my-cool-plan-abc12345"
    # Empty title still yields a valid id.
    assert store.plan_id_for("s", "") == "plan-s"
