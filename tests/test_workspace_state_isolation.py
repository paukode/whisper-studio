"""The suite must never write to the real workspace state.

A dev backend launched with the packaged app's WHISPER_HOME shares
``<data_root()>/workspace_config.json`` with the INSTALLED app. A test that
connected a workspace and then disconnected wrote ``{"path": null}`` there and
silently disconnected the user's running app: its frontend kept rendering the
cached tree, while every turn assembled the tool pool with ws_connected=False
and dropped the git and GitHub tools from the catalog. The model was then left
retrying `gh` through the shell until it gave up.

conftest's ``_isolate_workspace_state`` redirects those files per test, the
same way ``_isolate_app_databases`` handles sessions.db. These pin it.
"""

import json
import os

from server.infrastructure.paths import data_root
from server.workspace import state
from server.workspace.routes import recent


def _real(filename: str) -> str:
    return os.path.join(data_root(), filename)


def test_workspace_state_paths_are_redirected_away_from_the_real_data_root():
    for attr, filename in (
        ("WORKSPACE_CONFIG_PATH", "workspace_config.json"),
        ("RECENT_WORKSPACES_PATH", "recent_workspaces.json"),
        ("SAVE_LOCATIONS_PATH", "save_locations.json"),
    ):
        live = getattr(state, attr)
        assert live != _real(filename)
        assert not live.startswith(data_root()), f"{attr} still points inside the real app home"


def test_the_route_module_copy_is_redirected_too():
    """server/workspace/routes/recent.py imports the constant by value, so the
    source module alone is not enough."""
    assert recent.RECENT_WORKSPACES_PATH == state.RECENT_WORKSPACES_PATH


def test_connect_then_disconnect_leaves_the_real_config_untouched(tmp_path):
    before = None
    real_config = _real("workspace_config.json")
    if os.path.exists(real_config):
        with open(real_config) as f:
            before = f.read()

    ws = tmp_path / "repo"
    ws.mkdir()
    state.connect_workspace(str(ws))
    assert state.get_workspace_path() == os.path.realpath(str(ws))

    # What /api/workspace/disconnect does to the config.
    config = state.load_workspace_config()
    config["path"] = None
    state.save_workspace_config(config)
    assert state.get_workspace_path() is None

    with open(state.WORKSPACE_CONFIG_PATH) as f:
        assert json.load(f)["path"] is None, "the isolated copy took the write"

    after = None
    if os.path.exists(real_config):
        with open(real_config) as f:
            after = f.read()
    assert after == before, "the suite wrote to the real workspace config"


def test_recents_do_not_leak_tmp_paths_into_the_real_list(tmp_path):
    real_recents = _real("recent_workspaces.json")
    before = None
    if os.path.exists(real_recents):
        with open(real_recents) as f:
            before = f.read()

    ws = tmp_path / "repo"
    ws.mkdir()
    state.connect_workspace(str(ws))
    assert os.path.realpath(str(ws)) in state.load_recent_workspaces()

    after = None
    if os.path.exists(real_recents):
        with open(real_recents) as f:
            after = f.read()
    assert after == before, "a tmp_path workspace leaked into the real recents list"
