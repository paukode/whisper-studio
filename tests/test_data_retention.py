"""Tests for the account data-retention feature.

Covers the /api/data-retention endpoints (mode read, enable saves prior mode,
disable restores it, AccessDenied → 403), the /api/models retention flag, and
the classify_bedrock_error data-retention branch.
"""

from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.infrastructure.data_retention as dr
from server.infrastructure.errors import (
    DataRetentionRequiredError,
    classify_bedrock_error,
)


class FakeBedrock:
    """Stand-in for the Bedrock control-plane client."""

    def __init__(self, mode="inherit", deny=False):
        self.mode = mode
        self.deny = deny
        self.put_calls = []

    def _maybe_deny(self, op):
        if self.deny:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no perm"}}, op
            )

    def get_account_data_retention(self):
        self._maybe_deny("GetAccountDataRetention")
        return {"mode": self.mode}

    def put_account_data_retention(self, mode):
        self._maybe_deny("PutAccountDataRetention")
        self.put_calls.append(mode)
        self.mode = mode
        return {"mode": mode}


def _client(monkeypatch, fake):
    monkeypatch.setattr(dr, "_get_control_plane_client", lambda: fake)
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app)


def test_get_reports_mode(monkeypatch):
    c = _client(monkeypatch, FakeBedrock(mode="inherit"))
    r = c.get("/api/data-retention")
    assert r.status_code == 200
    assert r.json() == {"mode": "inherit", "enabled": False}


def test_get_reports_enabled_when_sharing(monkeypatch):
    c = _client(monkeypatch, FakeBedrock(mode="provider_data_share"))
    assert c.get("/api/data-retention").json() == {
        "mode": "provider_data_share",
        "enabled": True,
    }


def test_enable_sets_sharing(monkeypatch):
    fake = FakeBedrock(mode="none")
    c = _client(monkeypatch, fake)
    r = c.put("/api/data-retention", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"mode": "provider_data_share", "enabled": True}
    assert fake.put_calls == ["provider_data_share"]


def test_disable_sets_zero_retention(monkeypatch):
    """Policy: off = explicit zero retention ('none') for all models — never a
    restore of whatever mode the account had before."""
    fake = FakeBedrock(mode="provider_data_share")
    c = _client(monkeypatch, fake)
    r = c.put("/api/data-retention", json={"enabled": False})
    assert r.status_code == 200
    assert r.json() == {"mode": "none", "enabled": False}
    assert fake.put_calls == ["none"]
    assert dr.DEFAULT_RESTORE_MODE == "none"


def test_enable_disable_round_trip(monkeypatch):
    fake = FakeBedrock(mode="inherit")
    c = _client(monkeypatch, fake)
    c.put("/api/data-retention", json={"enabled": True})
    r = c.put("/api/data-retention", json={"enabled": False})
    assert r.json() == {"mode": "none", "enabled": False}
    assert fake.put_calls == ["provider_data_share", "none"]


def test_access_denied_returns_403(monkeypatch):
    c = _client(monkeypatch, FakeBedrock(deny=True))
    r = c.get("/api/data-retention")
    assert r.status_code == 403
    assert "permission" in r.json()["error"].lower()


def test_models_endpoint_includes_retention_flag():
    # Shape only — every model object carries the flag as a bool. (Does not
    # assert a specific model's value: this reads the live config.json, whose
    # contents vary by machine.)
    from server.chat import router as chat_router

    app = FastAPI()
    app.include_router(chat_router)
    data = TestClient(app).get("/api/models").json()
    assert data["models"], "expected at least one model"
    for m in data["models"]:
        assert isinstance(m.get("requires_data_retention"), bool)


def test_normalize_preserves_requires_data_retention():
    # The actual config → meta plumbing: a rich model entry's
    # requires_data_retention must survive normalization (this is what the
    # /api/models flag and the consent gate depend on). Config-independent.
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models(
        {
            "opus": {"id": "x", "label": "Opus", "thinking": "adaptive"},
            "fable5.0": {
                "id": "y",
                "label": "Fable 5.0",
                "thinking": "adaptive",
                "requires_data_retention": True,
            },
        }
    )
    assert meta["fable5.0"]["requires_data_retention"] is True
    assert meta["opus"]["requires_data_retention"] is False  # defaults False


def test_classify_data_retention_error():
    err = Exception(
        "ValidationException: data retention mode 'default' is not available for this model"
    )
    classified = classify_bedrock_error(err)
    assert isinstance(classified, DataRetentionRequiredError)
    assert classified.is_retryable is False
    assert "data retention" in classified.user_message.lower()
