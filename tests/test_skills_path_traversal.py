"""Skill create/update must reject names that escape the skills directory.

Before the fix, ``name`` was joined straight onto SKILLS_DIR, so a name like
``../../../../tmp/evil`` wrote a file anywhere on disk (path traversal → RCE
via dropping into cron/startup dirs)."""

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.skills import router as skills_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(skills_router)
    return TestClient(app)


def test_create_skill_rejects_path_traversal():
    client = _client()
    escape_target = "/tmp/whisper_evil_skill_test.md"
    if os.path.exists(escape_target):
        os.remove(escape_target)

    r = client.post(
        "/api/skills",
        json={
            "name": "../../../../../../tmp/whisper_evil_skill_test",
            "content": "pwned",
        },
    )

    assert r.status_code == 400
    assert not os.path.exists(escape_target), "traversal wrote outside SKILLS_DIR"


def test_create_skill_rejects_separators():
    client = _client()
    for bad in ["foo/bar", "a\\b", ".."]:
        r = client.post("/api/skills", json={"name": bad, "content": "x"})
        assert r.status_code == 400, f"{bad!r} should be rejected"
