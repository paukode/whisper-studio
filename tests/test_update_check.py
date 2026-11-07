"""GET /api/update-check: the Update notice shows only when the running
version is a release before the newest GitHub release, and any GitHub failure
means no notice. GitHub is faked with an httpx MockTransport at the client the
module builds, so nothing leaves the machine."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import update_check

RELEASE_PAGE = "https://github.com/paukode/whisper-studio/releases/tag/"
DOWNLOAD = "https://github.com/paukode/whisper-studio/releases/download/"


def _release(tag: str, **extra):
    body = {
        "tag_name": tag,
        "html_url": RELEASE_PAGE + tag,
        "published_at": "2026-10-29T12:00:00Z",
        "draft": False,
        "prerelease": False,
        "body": "notes",
        "assets": [
            {
                "name": f"WhisperStudio-{tag[1:]}.dmg",
                "browser_download_url": f"{DOWNLOAD}{tag}/WhisperStudio-{tag[1:]}.dmg",
            }
        ],
    }
    body.update(extra)
    return body


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub. Set ``state['response']`` to a dict (200 JSON), an int
    (that status), or an exception instance (raised by the transport)."""
    state: dict = {"response": _release("v2.15.2"), "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        resp = state["response"]
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, int):
            return httpx.Response(resp, json={"message": "nope"})
        return httpx.Response(200, json=resp)

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    update_check.reset_cache()
    yield state
    update_check.reset_cache()


def _check(monkeypatch, current: str, packaged: bool = False, fresh: bool = False) -> dict:
    monkeypatch.setattr(update_check, "running_version", lambda: current)
    if packaged:
        monkeypatch.setenv("WHISPER_HOME", "/tmp/whisper-home-test")
    else:
        monkeypatch.delenv("WHISPER_HOME", raising=False)
    app = FastAPI()
    app.include_router(update_check.router)
    resp = TestClient(app).get("/api/update-check", params={"fresh": "true"} if fresh else None)
    assert resp.status_code == 200
    return resp.json()


def test_older_app_sees_the_newer_release(github, monkeypatch):
    github["response"] = _release("v2.16.0")
    data = _check(monkeypatch, "2.15.2", packaged=True)
    assert data["update_available"] is True
    assert data["current"] == "2.15.2"
    assert data["latest"]["version"] == "2.16.0"
    assert data["latest"]["tag"] == "v2.16.0"
    assert data["latest"]["url"] == RELEASE_PAGE + "v2.16.0"
    assert data["latest"]["download_url"] == DOWNLOAD + "v2.16.0/WhisperStudio-2.16.0.dmg"
    assert data["latest"]["published_at"] == "2026-10-29T12:00:00Z"
    assert data["install"] == "app"
    assert data["update_commands"] == []


def test_same_version_sees_no_update(github, monkeypatch):
    github["response"] = _release("v2.15.2")
    data = _check(monkeypatch, "2.15.2", packaged=True)
    assert data["update_available"] is False
    assert data["latest"]["version"] == "2.15.2"


def test_newer_than_the_release_sees_no_update(github, monkeypatch):
    github["response"] = _release("v2.15.2")
    assert _check(monkeypatch, "2.16.0")["update_available"] is False


def test_dev_build_ahead_of_the_newest_tag_is_not_older(github, monkeypatch):
    github["response"] = _release("v2.15.2")
    data = _check(monkeypatch, "2.15.2-453-g69dcdaf4")
    assert data["update_available"] is False
    assert data["install"] == "source"


def test_dev_build_from_an_earlier_tag_is_older_and_gets_the_commands(github, monkeypatch):
    github["response"] = _release("v2.15.2")
    data = _check(monkeypatch, "2.15.0-12-gabc1234")
    assert data["update_available"] is True
    assert data["install"] == "source"
    assert data["update_commands"] == ["git pull", "bash setup.sh"]


@pytest.mark.parametrize(
    "response",
    [httpx.ConnectError("offline"), httpx.ReadTimeout("slow"), 403, 500, ["not", "a", "dict"]],
)
def test_github_failure_means_no_notice(github, monkeypatch, response):
    github["response"] = response
    data = _check(monkeypatch, "2.0.0")
    assert data["latest"] is None
    assert data["update_available"] is False


def test_release_page_outside_this_repo_is_ignored(github, monkeypatch):
    github["response"] = _release("v9.0.0", html_url="https://example.com/evil")
    data = _check(monkeypatch, "2.0.0")
    assert data["latest"] is None
    assert data["update_available"] is False


@pytest.mark.parametrize(
    "assets",
    [
        [],
        None,
        [{"name": "notes.txt", "browser_download_url": DOWNLOAD + "v9.0.0/notes.txt"}],
        [{"name": "WhisperStudio-9.0.0.dmg", "browser_download_url": "https://example.com/x.dmg"}],
        ["not an asset"],
    ],
)
def test_no_dmg_of_this_repo_leaves_only_the_release_page(github, monkeypatch, assets):
    github["response"] = _release("v9.0.0", assets=assets)
    data = _check(monkeypatch, "2.15.2", packaged=True)
    assert data["update_available"] is True
    assert data["latest"]["download_url"] == ""
    assert data["latest"]["url"] == RELEASE_PAGE + "v9.0.0"


def test_draft_or_prerelease_is_ignored(github, monkeypatch):
    github["response"] = _release("v9.0.0", prerelease=True)
    assert _check(monkeypatch, "2.0.0")["update_available"] is False


def test_unknown_running_version_sees_no_update(github, monkeypatch):
    github["response"] = _release("v9.0.0")
    assert _check(monkeypatch, "030b61ff")["update_available"] is False


def test_release_is_cached_between_checks(github, monkeypatch):
    github["response"] = _release("v2.16.0")
    _check(monkeypatch, "2.15.2")
    github["response"] = _release("v2.17.0")
    data = _check(monkeypatch, "2.15.2")
    assert len(github["requests"]) == 1
    assert data["latest"]["version"] == "2.16.0"


def test_a_fresh_check_skips_the_cache_and_refills_it(github, monkeypatch):
    github["response"] = _release("v2.16.0")
    _check(monkeypatch, "2.16.0")
    github["response"] = _release("v2.16.1")
    data = _check(monkeypatch, "2.16.0", fresh=True)
    assert len(github["requests"]) == 2
    assert data["update_available"] is True
    # The fresh answer is what the next ordinary check reuses.
    assert _check(monkeypatch, "2.16.0")["latest"]["version"] == "2.16.1"
    assert len(github["requests"]) == 2


def test_request_sends_no_user_data(github, monkeypatch):
    _check(monkeypatch, "2.15.2")
    (req,) = github["requests"]
    assert str(req.url) == update_check.RELEASES_URL
    assert req.method == "GET"
    assert req.content == b""
    assert "authorization" not in req.headers
    assert "cookie" not in req.headers
    assert "2.15.2" not in req.headers.get("user-agent", "")


def test_four_part_tags_compare_with_three_part_versions():
    assert update_check.is_older("2.11.2", "2.11.2.1")
    assert not update_check.is_older("2.11.2.1", "2.11.2")
    assert not update_check.is_older("2.11.2.0", "2.11.2")
    assert update_check.is_older("2.6.0-sonic", "2.7.0")


def test_bundle_version_comes_from_the_app_info_plist(tmp_path, monkeypatch):
    import plistlib

    contents = tmp_path / "Contents"
    backend = contents / "Resources" / "backend"
    backend.mkdir(parents=True)
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": "2.15.2"}, fh)
    monkeypatch.setattr(update_check, "repo_root", lambda: str(backend))
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "home"))
    update_check.reset_cache()
    try:
        assert update_check.running_version() == "2.15.2"
    finally:
        update_check.reset_cache()


def test_source_version_comes_from_git_describe():
    update_check.reset_cache()
    try:
        # A shallow clone without tags still names the commit (--always).
        assert update_check._git_describe_version()
    finally:
        update_check.reset_cache()
