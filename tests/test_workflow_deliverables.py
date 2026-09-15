"""A workflow run is 'completed' only if every file its script lists under
``deliverables`` exists and is non-empty. Before this the status was set the
moment the script returned, and the model told the user a report was saved
that the script's own text said it could not find."""

from server.workflows.manager import verify_deliverables


def _done(result):
    return {"status": "done", "result": result, "error": ""}


def test_present_non_empty_deliverables_keep_the_run_completed(tmp_path):
    f = tmp_path / "report.docx"
    f.write_bytes(b"content")
    out = verify_deliverables(_done({"deliverables": [str(f)]}), None)
    assert out["status"] == "done" and out["error"] == ""


def test_missing_deliverable_fails_the_run_and_names_it(tmp_path):
    out = verify_deliverables(_done({"deliverables": [str(tmp_path / "nope.docx")]}), None)
    assert out["status"] == "failed"
    assert "deliverable missing or empty" in out["error"]
    assert "nope.docx" in out["error"]


def test_empty_file_counts_as_missing(tmp_path):
    f = tmp_path / "empty.pdf"
    f.write_bytes(b"")
    out = verify_deliverables(_done({"deliverables": [str(f)]}), None)
    assert out["status"] == "failed" and "empty.pdf" in out["error"]


def test_relative_paths_resolve_against_the_workspace_and_dict_items_work(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.md").write_text("x")
    result = {"deliverables": ["out/a.md", {"path": "out/a.md"}]}
    assert verify_deliverables(_done(result), str(tmp_path))["status"] == "done"
    assert verify_deliverables(_done(result), None)["status"] == "failed"


def test_runs_without_a_deliverables_list_or_not_completed_are_untouched(tmp_path):
    assert verify_deliverables(_done({"fixed": 3}), None)["status"] == "done"
    assert verify_deliverables(_done("plain text"), None)["status"] == "done"
    failed = {"status": "failed", "result": {"deliverables": ["/nope"]}, "error": "boom"}
    assert verify_deliverables(failed, None) == failed
