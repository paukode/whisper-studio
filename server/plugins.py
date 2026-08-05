"""
Plugin system for Whisper Studio.
Plugins are Python .py files or packages in the plugins/ directory.
Each plugin can expose:
  - __version__: str
  - __description__: str
  - register(app, executor_registry): called on startup to register tools/routes
"""

import ast
import importlib.util
import json
import logging
import os
import re
import sys

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile

from server.infrastructure.paths import data_root, plugins_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

PLUGINS_DIR = plugins_root()
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

# Upload guards.
_MAX_PLUGIN_BYTES = 512 * 1024  # 512 KB
_SAFE_PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9_]+\.py")

_loaded_plugins: dict[str, dict] = {}

# Live-load bookkeeping so enable/disable can register/unregister without a
# restart. Keyed by plugin name:
#   _plugin_executors — executor names the plugin ADDED to the EXECUTORS registry
#     (captured by diffing the registry across register()). Popped on disable so
#     the per-turn tool pool stops offering them immediately.
#   _plugin_tools — advertised tool schemas the plugin contributes to the pool
#     (from an optional module-level ``TOOLS`` list, else auto-derived from the
#     added executors). assemble_full_catalog folds these in per turn.
#   _plugin_side_effects — counts of hooks/routes the plugin registered. These
#     CANNOT be cleanly unregistered from a running app, so we surface an honest
#     "restart to fully unload" note when a plugin that uses them is toggled.
_plugin_executors: dict[str, list[str]] = {}
_plugin_tools: dict[str, list[dict]] = {}
_plugin_side_effects: dict[str, dict] = {}

# FastAPI app reference, stashed at init so the toggle endpoint can live-load a
# plugin (which needs the app to call register(app, ...)). Set in init_plugins.
_app: FastAPI | None = None


def _default_tool_schema() -> dict:
    """Minimal schema for an auto-advertised plugin tool. A single optional
    free-text arg so Bedrock never receives an empty-object schema."""
    return {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Optional request or arguments for this tool.",
            },
        },
        "required": [],
    }


def _derive_plugin_tools(module, added_execs: list[str]) -> list[dict]:
    """Build the advertised tool schemas for a plugin. Every returned tool is
    backed by an executor the plugin registered (``added_execs``), so the pool
    never advertises an uncallable tool. A module may declare richer schemas via
    a top-level ``TOOLS`` list ({name, description, input_schema}); executors
    without a matching declaration get an auto-derived schema."""
    declared: dict[str, dict] = {}
    raw = getattr(module, "TOOLS", None)
    if isinstance(raw, list):
        for t in raw:
            if isinstance(t, dict) and isinstance(t.get("name"), str):
                declared[t["name"]] = t
    desc = str(getattr(module, "__description__", "") or "").strip()
    tools: list[dict] = []
    for ename in added_execs:
        t = declared.get(ename)
        if t:
            tools.append(
                {
                    "name": ename,
                    "description": str(t.get("description", desc or ename)),
                    "input_schema": t.get("input_schema") or _default_tool_schema(),
                }
            )
        else:
            tools.append(
                {
                    "name": ename,
                    "description": desc or f"Custom tool '{ename}' provided by a plugin.",
                    "input_schema": _default_tool_schema(),
                }
            )
    return tools


def plugin_tools() -> list[dict]:
    """Tool schemas contributed by currently-loaded (enabled) plugins, folded
    into the per-turn tool pool by ``assemble_full_catalog``.

    Only plugins present in ``_plugin_tools`` are loaded, but we additionally
    gate on the persisted enabled set (plus PROTECTED_PLUGINS) so a plugin can
    never leak a tool into the pool while disabled — the enabled flag is the
    source of truth the pool consults each turn."""
    try:
        enabled = set(load_plugins_config().get("enabled", [])) | PROTECTED_PLUGINS
    except Exception:
        enabled = set(PROTECTED_PLUGINS)
    out: list[dict] = []
    for name, tools in _plugin_tools.items():
        if name in enabled:
            out.extend(tools)
    return out


def unload_plugin(name: str) -> dict:
    """Best-effort live unload: pop the plugin's executors and tool schemas so
    the per-turn pool stops offering them immediately. Returns the plugin's
    side-effect counts ({hooks, routes}) so the caller can tell the user a
    restart is needed to fully unload hooks/routes (which have no clean
    in-process unregister)."""
    from server.executors import EXECUTOR_META, EXECUTORS

    for ename in _plugin_executors.pop(name, []):
        EXECUTORS.pop(ename, None)
        EXECUTOR_META.pop(ename, None)
    _plugin_tools.pop(name, None)
    side = _plugin_side_effects.pop(name, {})
    _loaded_plugins.pop(name, None)
    return side


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
    """Load and register a single plugin.

    Diffs the executor registry (and the hook/route counts) across
    ``register()`` so enable/disable can be reversed live: the executor names a
    plugin adds are tracked for unregister, and its advertised tool schemas are
    recorded for the per-turn pool. Hooks and HTTP routes are counted only —
    they have no clean in-process unregister, so the toggle surfaces a restart
    note when a plugin uses them."""
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
            from server.infrastructure import plugin_hooks

            before_execs = set(EXECUTORS)
            before_hooks = sum(len(v) for v in plugin_hooks._hooks.values())
            before_routes = len(app.routes)

            module.register(app, EXECUTORS)

            added_execs = [k for k in EXECUTORS if k not in before_execs]
            added_hooks = sum(len(v) for v in plugin_hooks._hooks.values()) - before_hooks
            added_routes = len(app.routes) - before_routes

            _plugin_executors[name] = added_execs
            _plugin_tools[name] = _derive_plugin_tools(module, added_execs)
            _plugin_side_effects[name] = {
                "hooks": max(0, added_hooks),
                "routes": max(0, added_routes),
            }
            meta["registered"] = True
            meta["tools"] = [t["name"] for t in _plugin_tools[name]]

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
    # Stash the app so the toggle endpoint can live-load a plugin later.
    global _app
    _app = app
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
                # Tool names the plugin registered live (empty until loaded).
                # Lets the UI / tests confirm executors reached the pool.
                "tools": loaded.get("tools", []),
            }
        )
    return {"plugins": result, "plugins_dir": PLUGINS_DIR}


@router.patch("/{name}/toggle")
async def toggle_plugin(name: str):
    """Flip ``name`` in the enabled set and apply the change LIVE — no restart.

    Enabling calls ``load_plugin`` so the plugin's executors register into the
    live EXECUTORS registry; the per-turn tool pool (assemble_full_catalog)
    picks them up on the user's next message. Disabling pops those executors and
    tool schemas so the pool stops offering them immediately.

    Honest caveat: a plugin that registers FastAPI HTTP routes (rather than tool
    executors) or in-process hooks cannot fully hot-(un)register those into a
    running app — the response flags ``restartRequired`` and says so when the
    toggled plugin uses them. Tool executors are always live.

    Protected plugins are non-toggleable and respond with 409 — they are part of
    the product's safety contract (see PROTECTED_PLUGINS).
    """
    if name in PROTECTED_PLUGINS:
        raise HTTPException(
            status_code=409,
            detail=(f"Plugin '{name}' is required for safety and cannot be disabled."),
        )
    discovered = discover_plugins()
    info = next((p for p in discovered if p["name"] == name), None)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found.")
    config = _migrate_disabled_to_enabled(load_plugins_config(), discovered)
    enabled = list(config.get("enabled", []))

    if name in enabled:
        # --- Disable: persist, then live-unload executors/tools. ---
        enabled.remove(name)
        config["enabled"] = enabled
        save_plugins_config(config)
        side = unload_plugin(name)
        restart = bool(side.get("hooks") or side.get("routes"))
        note = (
            "Disabled. Its tools are no longer offered. A restart is needed to "
            "fully unload its hooks/routes."
            if restart
            else "Disabled. Its tools are no longer offered."
        )
        return {"name": name, "enabled": False, "restartRequired": restart, "note": note}

    # --- Enable: persist, then live-load so executors register now. ---
    enabled.append(name)
    config["enabled"] = enabled
    save_plugins_config(config)
    if _app is None:
        # No app reference (e.g. init never ran). The flag is set; a restart
        # will load it. This should not happen in the running server.
        return {
            "name": name,
            "enabled": True,
            "loaded": False,
            "restartRequired": True,
            "note": "Enabled. Restart the server to load this plugin.",
        }
    ok = load_plugin(info, _app)
    if not ok:
        err = _loaded_plugins.get(name, {}).get("error", "unknown error")
        return {
            "name": name,
            "enabled": True,
            "loaded": False,
            "restartRequired": True,
            "note": f"Enabled, but load failed: {err}. Fix the plugin, then restart.",
        }
    side = _plugin_side_effects.get(name, {})
    restart = bool(side.get("routes"))
    note = "Enabled. Its tools are available on your next message."
    if restart:
        note += " It also registers HTTP routes, which need a server restart."
    return {"name": name, "enabled": True, "loaded": True, "restartRequired": restart, "note": note}


def _validate_plugin_source(data: bytes) -> tuple[bool, str]:
    """Static validation of an uploaded plugin: valid UTF-8, parses, and defines
    a top-level ``register`` function. Uses ``ast`` only — the source is NEVER
    executed here (that is the RCE guard; execution happens only on explicit
    enable)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False, "file is not valid UTF-8"
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return False, f"syntax error: {e.msg} (line {e.lineno})"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "register":
            return True, ""
    return False, "no top-level register(app, executor_registry) function found"


@router.post("/upload")
async def upload_plugin(file: UploadFile = File(...), overwrite: str = Form("false")):
    """Add a plugin from the UI: a single ``.py`` file. It is validated
    statically (safe name, size limit, parses, defines register) and written to
    the plugins folder DISABLED — the user must enable it to load it (the
    new-plugins-start-off RCE guard is intentional and preserved)."""
    # Validate the RAW filename: the regex forbids path separators and ``..``,
    # so a name like ``foo/bar.py`` or ``../evil.py`` is rejected outright
    # rather than silently basenamed into an accepted name.
    filename = (file.filename or "").strip()
    if not filename or not _SAFE_PLUGIN_NAME_RE.fullmatch(filename):
        raise HTTPException(
            status_code=400,
            detail="Plugin filename must match [A-Za-z0-9_]+.py (no path separators).",
        )
    stem = filename[:-3]
    if stem.startswith("_"):
        raise HTTPException(status_code=400, detail="Plugin name cannot start with '_'.")
    if stem.lower() == "readme":
        raise HTTPException(status_code=400, detail="'README' is a reserved name.")
    if stem in PROTECTED_PLUGINS:
        raise HTTPException(
            status_code=409,
            detail=f"'{stem}' is a protected built-in plugin and cannot be replaced.",
        )
    data = await file.read()
    if len(data) > _MAX_PLUGIN_BYTES:
        raise HTTPException(
            status_code=400, detail=f"Plugin exceeds the {_MAX_PLUGIN_BYTES // 1024} KB limit."
        )
    ok, reason = _validate_plugin_source(data)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Rejected: {reason}")

    os.makedirs(PLUGINS_DIR, exist_ok=True)
    root = os.path.realpath(PLUGINS_DIR)
    dest = os.path.join(root, filename)
    # Defense in depth: the regex already forbids separators, but confirm
    # containment before writing.
    if os.path.dirname(os.path.realpath(dest)) != root:
        raise HTTPException(status_code=400, detail="Invalid destination path.")
    overwrite_flag = str(overwrite).strip().lower() in ("1", "true", "yes", "on")
    if os.path.exists(dest) and not overwrite_flag:
        raise HTTPException(
            status_code=409,
            detail=f"Plugin '{filename}' already exists. Enable overwrite to replace it.",
        )
    with open(dest, "wb") as f:
        f.write(data)
    log.info("Plugin uploaded (disabled): %s", filename)
    return {
        "name": stem,
        "uploaded": True,
        "enabled": False,
        "note": "Uploaded and left OFF for safety. Enable it in the list to load it.",
    }
