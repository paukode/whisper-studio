"""The context doctor: per-section cost plus the findings nothing else catches."""

from server.context_report import (
    build_context_report,
    find_duplication,
    find_missing_tool_refs,
)

_TOOL = {
    "name": "terminal_run",
    "description": (
        "Runs a one-shot non-interactive shell command and returns the exit code. "
        "Default timeout 30s, max 300s; on timeout partial output is returned."
    ),
}


def test_duplication_is_found_when_a_section_restates_a_tool_description():
    """The exact P2 smell: prompt prose repeating what the schema already says."""
    sections = {
        "terminal_run_guidance": (
            "You have a terminal_run tool. Default timeout 30s, max 300s; on timeout "
            "partial output is returned. Prefer it over asking the user."
        )
    }
    findings = find_duplication(sections, [_TOOL])
    assert findings, "an eight-word-plus restatement must be reported"
    assert findings[0]["section"] == "terminal_run_guidance"
    assert findings[0]["tool"] == "terminal_run"
    assert "timeout" in findings[0]["phrase"]


def test_unrelated_prose_is_not_reported():
    sections = {"identity": "You are a concise assistant that answers questions well."}
    assert find_duplication(sections, [_TOOL]) == []


def test_incidental_short_phrases_do_not_register():
    """Shared wording below the window length is normal English, not duplication."""
    sections = {"x": "Runs a one-shot command differently, with other words entirely."}
    assert find_duplication(sections, [_TOOL]) == []


def test_adjacent_matches_merge_into_one_finding():
    sections = {"x": _TOOL["description"]}
    findings = find_duplication(sections, [_TOOL])
    assert len(findings) == 1
    assert findings[0]["words"] > 20


def test_missing_tool_refs_are_reported():
    """The enter_worktree failure: instructions for a tool with no schema."""
    sections = {"git": "Call the enter_worktree tool, then use git_status to check."}
    missing = find_missing_tool_refs(sections, {"git_status"})
    assert missing == [{"section": "git", "tool": "enter_worktree"}]


def test_present_tool_refs_are_not_reported():
    sections = {"git": "Use git_status and ws_read_file."}
    assert find_missing_tool_refs(sections, {"git_status", "ws_read_file"}) == []


def test_paths_are_not_mistaken_for_tool_names():
    """The prompt interpolates the real workspace path, and directories have
    underscores. `/home/me/my_project` is not a dead tool reference."""
    sections = {"workspace": "A workspace is connected at '/home/me/my_project/sub_dir'."}
    assert find_missing_tool_refs(sections, set()) == []


# ── The live report ──────────────────────────────────────────────────────────


def _report(monkeypatch, ws_path):
    from server.skills import init_skills

    init_skills()
    for target in (
        "server.workspace.get_workspace_path",
        "server.chat.tool_pool.get_workspace_path",
        "server.workspace.tools.get_workspace_path",
    ):
        monkeypatch.setattr(target, lambda: ws_path)
    return build_context_report(ws_path=ws_path)


def test_report_shape_and_cached_split(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = _report(monkeypatch, str(repo))

    assert report["sections"], "the prompt is never empty"
    assert report["totals"]["chars"] == sum(s["chars"] for s in report["sections"])
    assert report["totals"]["cached_pct"] > 0
    assert report["tools"]["advertised"] > 0
    assert report["tools"]["deferred"] > 0

    # Every section is classified, and the layer boundary is what decides it.
    for section in report["sections"]:
        assert section["cached"] == (section["layer"] <= 30)


def test_live_prompt_has_no_duplication_or_dead_tool_refs(tmp_path, monkeypatch):
    """A regression gate on the shipped prompt, not just on the detectors.

    If someone reintroduces guidance that a tool description already carries, or
    points the prompt at a tool that does not exist, this fails.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = _report(monkeypatch, str(repo))

    assert report["duplication"] == [], (
        f"prompt duplicates tool descriptions: {report['duplication']}"
    )
    assert report["missing_tool_refs"] == [], (
        f"prompt names absent tools: {report['missing_tool_refs']}"
    )


def test_endpoint_serves_the_report():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.doctor import router as doctor_router

    app = FastAPI()
    app.include_router(doctor_router)
    body = TestClient(app).get("/api/doctor/context").json()

    assert "error" not in body, body.get("error")
    assert "totals" in body and "sections" in body
    assert body["totals"]["chars"] > 0
