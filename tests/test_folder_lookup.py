"""resolve_folder: a spoken or typed folder reference becomes one real directory
when unambiguous, or a short candidate list for the user to confirm."""

import os

from server.workspace.folder_lookup import normalize, resolve_folder, spoken_to_name


def _mk(tmp_path, *names):
    for n in names:
        (tmp_path / n).mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def test_spoken_punctuation_and_normalization():
    assert spoken_to_name("m l dash o p s") == "m l-o p s"
    assert spoken_to_name("my_repo underscore two dot zero") == "my_repo_two.zero"
    assert normalize("m l dash o p s") == "mlops"
    assert normalize("ML-OPS") == "mlops" == normalize("ml ops")


def test_exact_path_wins(tmp_path):
    root = _mk(tmp_path, "ml-ops")
    assert resolve_folder(os.path.join(root, "ml-ops"), roots=[]) == (
        os.path.realpath(os.path.join(root, "ml-ops")),
        [],
    )


def test_name_lookup_is_case_and_punctuation_insensitive(tmp_path):
    root = _mk(tmp_path, "ml-ops", "other", "notes")
    want = os.path.realpath(os.path.join(root, "ml-ops"))
    assert resolve_folder("ML-OPS", roots=[root]) == (want, [])
    assert resolve_folder("the ml ops folder in my documents", roots=[root]) == (want, [])
    # Speech recognition spelling it out letter by letter.
    assert resolve_folder("m l dash o p s", roots=[root]) == (want, [])
    # A wrong parent path still resolves by name through the roots.
    assert resolve_folder("~/Nowhere/ml-ops", roots=[root]) == (want, [])


def test_close_matches_are_offered_not_guessed(tmp_path):
    root = _mk(tmp_path, "ml-ops", "ml-ops-archive", "ml-reports")
    path, candidates = resolve_folder("ml ops arch", roots=[root])
    assert path is None
    assert os.path.realpath(os.path.join(root, "ml-ops-archive")) in candidates
    # Typo with a single clearly closest folder opens it.
    _mk(tmp_path, "ledger-toolkit")
    path, candidates = resolve_folder("ledger-tolkit", roots=[root])
    assert path == os.path.realpath(os.path.join(root, "ledger-toolkit")) and candidates == []


def test_nothing_close_returns_no_candidates(tmp_path):
    root = _mk(tmp_path, "alpha", "beta")
    assert resolve_folder("zzzz-quux", roots=[root]) == (None, [])
    assert resolve_folder("", roots=[root]) == (None, [])


def test_recent_workspace_name_match(tmp_path):
    root = _mk(tmp_path, "deep/nested/ml-ops")
    rec = os.path.join(root, "deep", "nested", "ml-ops")
    assert resolve_folder("ml-ops", roots=[], recents=[rec]) == (os.path.realpath(rec), [])


def test_a_shared_distinctive_word_makes_a_candidate(tmp_path):
    """Speech recognition kept one word: "flight-etl" for orders-etl still
    shares "etl", so the *-etl folders are offered instead of nothing."""
    root = _mk(tmp_path, "orders-etl", "invoices-etl", "notes")
    path, candidates = resolve_folder("flight-etl", roots=[root])
    assert path is None
    assert {os.path.basename(c) for c in candidates} == {"orders-etl", "invoices-etl"}
    # Generic words alone do not relate folders.
    _mk(tmp_path, "test-data")
    assert resolve_folder("data test", roots=[root]) == (None, [])


def test_browse_names_lists_what_exists_where_the_name_was_looked_for(tmp_path):
    from server.workspace.folder_lookup import browse_names

    root = _mk(tmp_path, "orders-etl", "notes", ".hidden")
    (tmp_path / "file.txt").write_text("x")
    assert browse_names("flight-etl", roots=[root]) == ["notes", "orders-etl"]
    assert browse_names("x", roots=[root], limit=1) == ["notes"]
