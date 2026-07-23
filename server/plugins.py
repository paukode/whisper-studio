"""
Plugin system for Whisper Studio.
Plugins are Python .py files or packages in the plugins/ directory.
Each plugin can expose:
  - __version__: str
  - __description__: str
  - register(app, executor_registry): called on startup to register tools/routes
"""

import importlib.util
import json
import logging
import os
import sys

from fastapi import APIRouter, FastAPI, HTTPException

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
PLUGINS_DIR = os.path.join(BASE_DIR, "plugins")
DATA_DIR = data_root()
PLUGINS_CONFIG_PATH = os.path.join(DATA_DIR, "plugins_config.json")

# Plugins that ship with the product and provide a safety guarantee. These
# are loaded unconditionally on startup — independent of plugins_config.json
# — and the toggle endpoint refuses to disable them. The UI surfaces them
# as a locked, checked toggle so the user understands they are non-optional.
#
# Rationale: ``security_checks`` scans file writes for dangerous patterns
# (eval, subprocess shell=True, hardcoded secrets, SQL injection shapes,
# innerHTML XSS sinks). Letting the user accidentally — or the model
# silently via a config edit — disable it would defeat the in-bubble
# safety net for ws_write_file / ws_edit_file / ws_create_file.
PROTECTED_PLUGINS: frozenset[str] = frozenset({"security_checks"})

_loaded_plugins: dict[str, dict] = {}


def load_plugins_config() -> dict:
    """Load the plugin config. Schema is opt-in via ``enabled: [...]``.

    Backward compatibility: configs written with the old opt-out
    ``disabled: [...]`` schema are migrated lazily — see
    ``_migrate_disabled_to_enabled``. The migration runs on the first
    ``init_plugins`` after the upgrade so existing users keep whatever
    plugins they currently have loaded, and any NEW plugin file
    dropped into ``plugins/`` afterwards is OFF until they explicitly
    enable it (instead of silently auto-loading).
    """
    try:
        with open(PLUGINS_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"enabled": []}


def save_plugins_config(config: dict):
    os.makedirs(os.path.dirname(PLUGINS_CONFIG_PATH), exist_ok=True)
    with open(PLUGINS_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _migrate_disabled_to_enabled(config: dict, discovered: list[dict]) -> dict:
    """One-shot upgrade: old `disabled: []` config → new `enabled: []`.

    Preserves the current observable state: anything that WOULD have
    loaded under opt-out (i.e. discovered minus disabled) becomes the
    new ``enabled`` set. After this the plugin loader is purely opt-in;
    dropping a new file into ``plugins/`` no longer auto-executes it.
    """
    if "enabled" in config:
        return config  # already on new schema
    disabled = set(config.get("disabled", []))
    enabled = sorted(p["name"] for p in discovered if p["name"] not in disabled)
    migrated = {"enabled": enabled}
    save_plugins_config(migrated)
    log.info(
        "plugins: migrated %d plugin(s) from opt-out 'disabled' schema "
        "to opt-in 'enabled' schema (carried over: %s)",
        len(enabled),
        enabled,
    )
    return migrated


def discover_plugins() -> list[dict]:
    """Scan the plugins/ directory for installable plugins."""
    plugins = []
    if not os.path.isdir(PLUGINS_DIR):
        return plugins
    for entry in sorted(os.listdir(PLUGINS_DIR)):
        if entry.startswith("_") or entry.startswith(".") or entry == "README.md":
            continue
        path = os.path.join(PLUGINS_DIR, entry)
        if entry.endswith(".py"):
            plugins.append({"name": entry[:-3], "path": path, "type": "module"})
        elif os.path.isdir(path) and os.path.exists(os.path.join(path, "__init__.py")):
            plugins.append({"name": entry, "path": path, "type": "package"})
    return plugins


def load_plugin(plugin_info: dict, app: FastAPI = None) -> bool:
    """Load and register a single plugin."""
    name = plugin_info["name"]
    path = plugin_info["path"]
    try:
        if plugin_info["type"] == "module":
            spec = importlib.util.spec_from_file_location(f"whisper_plugin_{name}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        else:
            if PLUGINS_DIR not in sys.path:
                sys.path.insert(0, PLUGINS_DIR)
            module = importlib.import_module(name)

        meta = {
            "name": name,
            "version": getattr(module, "__version__", "0.1.0"),
            "description": getattr(module, "__description__", name),
            "path": path,
            "status": "loaded",
        }

        if hasattr(module, "register") and app is not None:
            from server.executors import EXECUTORS

            module.register(app, EXECUTORS)
            meta["registered"] = True

        _loaded_plugins[name] = meta
        log.info("Plugin loaded: %s v%s", name, meta["version"])
        return True
    except Exception as e:
        log.error("Plugin load error (%s): %s", name, e)
        _loaded_plugins[name] = {"name": name, "path": path, "status": "error", "error": str(e)}
        return False


def init_plugins(app: FastAPI = None):
    """Initialize explicitly enabled plugins at startup.

    Opt-in: only plugins whose name appears in
    ``plugins_config.json:enabled`` are loaded. Dropping a file into
    ``plugins/`` no longer auto-executes it — the user must enable it
    via the settings UI (or hand-edit the config). Closes the drive-by
    "share a cool plugin in Discord, restart, get RCE" vector.
    """
    # Ensure plugins directory and README exist
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    readme = os.path.join(PLUGINS_DIR, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w") as f:
            f.write(
                "# Whisper Studio Plugins\n\n"
                "Drop `.py` files here to add custom tools and executors.\n\n"
                "Plugins are **opt-in**. After dropping a file here you must\n"
                "explicitly enable it in Settings → Plugins (or restart with\n"
                "the name added to `data/plugins_config.json`:`enabled`).\n\n"
                "## Plugin interface\n\n"
                "```python\n"
                "__version__ = '1.0.0'\n"
                "__description__ = 'My custom plugin'\n\n"
                "def register(app, executor_registry):\n"
                "    # Register a new executor tool\n"
                "    def my_tool(tool_input, transcript, attachments):\n"
                "        return 'Hello from plugin!'\n"
                "    executor_registry['my_tool'] = my_tool\n"
                "```\n"
            )

    discovered = discover_plugins()
    config = _migrate_disabled_to_enabled(load_plugins_config(), discovered)
    # PROTECTED_PLUGINS are loaded even if the user hand-removed them from
    # the config file — they are part of the product's safety contract,
    # not user-configurable. Union (not intersection) so a missing config
    # still loads them.
    effective_enabled = set(config.get("enabled", [])) | PROTECTED_PLUGINS

    for plugin_info in discovered:
        if plugin_info["name"] in effective_enabled:
            load_plugin(plugin_info, app)

    log.info(
        "Plugins initialized: %d loaded, %d discovered-but-disabled",
        len(_loaded_plugins),
        max(0, len(discovered) - len(_loaded_plugins)),
    )


# --- API Routes ---


@router.get("")
async def list_plugins():
    discovered = discover_plugins()
    # Migrate-on-read so the API surface reflects the new schema even
    # if init_plugins hasn't run yet in this process (e.g. tests).
    config = _migrate_disabled_to_enabled(load_plugins_config(), discovered)
    enabled = set(config.get("enabled", []))
    result = []
    for p in discovered:
        loaded = _loaded_plugins.get(p["name"], {})
        is_protected = p["name"] in PROTECTED_PLUGINS
        result.append(
            {
                "name": p["name"],
                "version": loaded.get("version", ""),
                "description": loaded.get("description", p["name"]),
                "status": loaded.get("status", "discovered"),
                "error": loaded.get("error"),
                # Protected plugins are reported as enabled regardless of the
                # config file's contents so the UI never shows them as "Off".
                "enabled": is_protected or p["name"] in enabled,
                "protected": is_protected,
            }
        )
    return {"plugins": result, "plugins_dir": PLUGINS_DIR}


@router.patch("/{name}/toggle")
async def toggle_plugin(name: str):
    """Flip ``name`` in the enabled set. New plugins start disabled
    until the user explicitly toggles them on here.

    Protected plugins are non-toggleable and respond with 409 — they are
    part of the product's safety contract (see PROTECTED_PLUGINS).
    """
    if name in PROTECTED_PLUGINS:
        raise HTTPException(
            status_code=409,
            detail=(f"Plugin '{name}' is required for safety and cannot be disabled."),
        )
    discovered = discover_plugins()
    config = _migrate_disabled_to_enabled(load_plugins_config(), discovered)
    enabled = list(config.get("enabled", []))
    if name in enabled:
        enabled.remove(name)
        is_enabled = False
    else:
        enabled.append(name)
        is_enabled = True
    config["enabled"] = enabled
    save_plugins_config(config)
    return {"name": name, "enabled": is_enabled, "note": "Restart server to apply changes"}
