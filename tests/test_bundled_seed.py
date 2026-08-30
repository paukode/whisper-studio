"""First-launch seeding of the bundled speech models, and the local-first
default. build_app.sh stages the language-ID and speaker-encoder snapshots
into <backend>/models-bundled/; server startup copies any missing one into
the live models root, so a fresh Mac app install transcribes with language
and speaker detection without downloading anything."""

import os

import server.main as main_mod
from server.infrastructure import paths as infra_paths


def _mk_bundled(base, name: str) -> str:
    d = base / "models-bundled" / name
    d.mkdir(parents=True)
    (d / "hyperparams.yaml").write_text("ok")
    return str(d)


def test_seed_copies_missing_and_skips_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "BASE_DIR", str(tmp_path))
    models = tmp_path / "models"
    monkeypatch.setattr(infra_paths, "models_root", lambda: str(models))
    _mk_bundled(tmp_path, "lang-id-voxlingua107-ecapa")
    _mk_bundled(tmp_path, "spkrec-ecapa-voxceleb")
    # One of the two already exists with user content — it must be left alone.
    existing = models / "spkrec-ecapa-voxceleb"
    existing.mkdir(parents=True)
    (existing / "user.txt").write_text("keep me")

    main_mod._seed_bundled_models()

    assert (models / "lang-id-voxlingua107-ecapa" / "hyperparams.yaml").exists()
    assert (existing / "user.txt").read_text() == "keep me"
    assert not (existing / "hyperparams.yaml").exists()  # not overwritten


def test_seed_noop_without_bundle_dir(tmp_path, monkeypatch):
    """Source checkouts have no models-bundled dir (setup.sh downloads the
    same two models instead) — startup must not create anything."""
    monkeypatch.setattr(main_mod, "BASE_DIR", str(tmp_path))
    models = tmp_path / "models"
    monkeypatch.setattr(infra_paths, "models_root", lambda: str(models))
    main_mod._seed_bundled_models()
    assert not models.exists() or not os.listdir(models)


def test_fresh_installs_default_to_local_mode():
    from server.infrastructure.config import FIRST_RUN_USER_CONFIG

    assert FIRST_RUN_USER_CONFIG["model_mode"] == "local"
