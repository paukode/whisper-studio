"""Live preview is always on: the probe, the refusal, and the catalog.

setup.sh installs Playwright and Chromium, and the packaged .app ships them in
its bundle, so nothing installs at run time. The probe reads the exact builds
the pinned playwright launches. When they are missing, every preview tool
refuses with the fix (run setup.sh again, or install the app again), and the
tools stay in the catalog either way.
"""

import asyncio

from server.preview import capability

# The conftest fixture _preview_browser_present makes the probe pass for every
# test; the refusal tests below patch it to fail.


def _make_bundled_browsers(root):
    """Create the exact versioned dirs + INSTALLATION_COMPLETE markers the
    pinned playwright expects, the way `playwright install chromium` (and
    therefore the bundle) lays them out."""
    required = capability._required_browser_dirs()
    assert required, "playwright registry should be readable in the venv"
    assert len(required) == 2, "expected full chromium + headless shell"
    for d in required:
        (root / d).mkdir(parents=True)
        (root / d / "INSTALLATION_COMPLETE").write_text("")
    return required


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------


def test_registry_names_both_chromium_builds():
    """headless=True launches the headless shell; the probe must require it
    alongside full chromium, at exact revisions from playwright's registry."""
    required = capability._required_browser_dirs()
    names = sorted(d.rsplit("-", 1)[0] for d in required)
    assert names == ["chromium", "chromium_headless_shell"]
    assert all(d.rsplit("-", 1)[1].isdigit() for d in required)


def test_chromium_installed_with_exact_revisions(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    _make_bundled_browsers(tmp_path)
    assert capability.is_chromium_installed() is True


def test_stale_revision_does_not_count(tmp_path, monkeypatch):
    """A cache left by an older playwright must not report ready — the pinned
    playwright would fail at launch time with 'Executable doesn't exist'."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    stale = tmp_path / "chromium-1"
    stale.mkdir(parents=True)
    (stale / "INSTALLATION_COMPLETE").write_text("")
    assert capability.is_chromium_installed() is False


def test_missing_headless_shell_not_ready(tmp_path, monkeypatch):
    """Full chromium alone is not enough: headless launches execute the
    chromium-headless-shell build on this playwright version."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    required = _make_bundled_browsers(tmp_path)
    shell = next(d for d in required if "headless_shell" in d)
    (tmp_path / shell / "INSTALLATION_COMPLETE").unlink()
    assert capability.is_chromium_installed() is False


def test_an_unreadable_registry_is_not_installed(tmp_path, monkeypatch):
    """Any chromium* marker used to count, so a stale older revision reported
    ready and then failed at launch."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(capability, "_required_browser_dirs", lambda: ())
    marker_dir = tmp_path / "chromium-9999"
    marker_dir.mkdir(parents=True)
    (marker_dir / "INSTALLATION_COMPLETE").write_text("")
    assert capability.is_chromium_installed() is False


def test_is_packaged_install_follows_whisper_home(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    assert capability.is_packaged_install() is False
    monkeypatch.setenv("WHISPER_HOME", "/tmp/somewhere")
    assert capability.is_packaged_install() is True


# ---------------------------------------------------------------------------
# Refusal and catalog
# ---------------------------------------------------------------------------


def test_a_source_install_without_the_browser_says_run_setup(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    monkeypatch.setattr(capability, "preview_capability_ok", lambda: False)
    message = capability.missing_browser_message()
    assert "setup.sh" in message


def test_a_packaged_app_without_the_browser_says_install_again(monkeypatch):
    monkeypatch.setenv("WHISPER_HOME", "/tmp/somewhere")
    monkeypatch.setattr(capability, "preview_capability_ok", lambda: False)
    message = capability.missing_browser_message()
    assert "Install Whisper Studio again" in message
    assert "setup.sh" not in message


def test_a_working_install_has_no_refusal():
    assert capability.missing_browser_message() is None


def test_every_preview_tool_refuses_with_the_fix_when_the_browser_is_missing(monkeypatch):
    """Read-only and approval-gated tools alike answer with the refusal; no
    approval card is raised for a preview that cannot start."""
    from server.preview.router import execute_preview_tool
    from server.preview.tools import PREVIEW_TOOL_NAMES

    monkeypatch.delenv("WHISPER_HOME", raising=False)
    monkeypatch.setattr(capability, "preview_capability_ok", lambda: False)
    expected = capability.missing_browser_message()
    for name in sorted(PREVIEW_TOOL_NAMES):
        out = asyncio.run(execute_preview_tool(name, {"__session_id__": "c1"}))
        assert out == expected, name


def test_preview_tools_stay_in_the_catalog_without_the_browser(monkeypatch):
    """The catalog does not follow the install, so it stays byte-stable."""
    from server.chat.tool_pool import assemble_full_catalog
    from server.preview.tools import PREVIEW_TOOL_NAMES

    with_browser = [t["name"] for t in assemble_full_catalog()]
    monkeypatch.setattr(capability, "preview_capability_ok", lambda: False)
    without_browser = [t["name"] for t in assemble_full_catalog()]
    assert with_browser == without_browser
    assert PREVIEW_TOOL_NAMES <= set(with_browser)
