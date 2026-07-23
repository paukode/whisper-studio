"""data_root() is the single owner of the app-data location.

Modules used to derive DATA_DIR from their own ``__file__``, which moves with
whatever checkout/worktree the server runs from — data written by a worktree
instance evaporated with the worktree. These pin the resolution order and
guard against new self-derived data dirs creeping back in.
"""

import os
import pathlib
import re

from server.infrastructure.paths import data_root, repo_root

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent / "server"

# A join that names the "data" dir literally — only paths.py may do this.
_DATA_JOIN = re.compile(r'os\.path\.join\([^)]*"data"')


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    assert data_root() == str(tmp_path)


def test_default_is_repo_data(monkeypatch):
    monkeypatch.delenv("WHISPER_DATA_DIR", raising=False)
    monkeypatch.setattr("server.infrastructure.config.get", lambda key, default=None: default)
    assert data_root() == os.path.join(repo_root(), "data")


def test_config_data_dir_used_when_no_env(monkeypatch, tmp_path):
    monkeypatch.delenv("WHISPER_DATA_DIR", raising=False)
    monkeypatch.setattr(
        "server.infrastructure.config.get",
        lambda key, default=None: str(tmp_path) if key == "data_dir" else default,
    )
    assert data_root() == str(tmp_path)


def test_no_module_derives_its_own_data_dir():
    # Every app-data path must resolve through data_root(); a literal
    # join(..., "data") anywhere else reintroduces the moves-with-the-checkout
    # bug that made cache files unfindable.
    offenders = []
    for path in SERVER_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name == "paths.py" and path.parent.name == "infrastructure":
            continue
        if _DATA_JOIN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SERVER_DIR)))
    assert offenders == [], (
        f"derive app-data paths via data_root(), not join(..'data'): {offenders}"
    )
