import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from server.approval.bootstrap import register_defaults as register_approval_defaults
from server.approval.router import router as approval_router
from server.attachments import cleanup_loop
from server.attachments import router as attachments_router
from server.auto_mode import router as auto_mode_router

# server.ask_user is a tool-descriptor module only — it has no HTTP
# handlers, so there's nothing to mount. Import elsewhere only when
# the tool descriptors themselves are needed.
from server.buddy import router as buddy_router
from server.chat import router as chat_router
from server.ci.routes import router as ci_router
from server.costs.tracker import router as cost_router
from server.cron_scheduler import init_scheduler
from server.cron_scheduler import router as cron_router
from server.doctor import router as doctor_router
from server.git.router import router as git_router
from server.goals.routes import router as goals_router
from server.hooks.routes import router as hooks_router
from server.index import init_index_scheduler
from server.index import router as index_router
from server.infrastructure.async_tasks import spawn
from server.infrastructure.boot_status import health_payload, record_boot_error
from server.infrastructure.config import router as config_router
from server.infrastructure.data_retention import router as data_retention_router
from server.infrastructure.feature_flags import router as feature_flags_router
from server.infrastructure.result_cache import router as result_cache_router
from server.infrastructure.sessions import router as sessions_router
from server.lsp import router as lsp_router
from server.lsp_proxy import router as lsp_proxy_router
from server.mcp import mcp_manager
from server.mcp import router as mcp_router
from server.memory import init_memory
from server.memory.router import memory_router
from server.migrations.runner import run_migrations
from server.notifications import router as notifications_router
from server.plans.routes import router as plans_router
from server.plugins import init_plugins
from server.plugins import router as plugins_router
from server.preview.install_routes import router as preview_install_router
from server.preview.manager import preview_manager
from server.preview.routes import router as preview_router
from server.preview.screencast import router as preview_screencast_router
from server.security.permissions import router as permissions_router
from server.skills import init_skills, whisper_md_router
from server.skills import router as skills_router
from server.tasks.routes import router as background_tasks_router
from server.tasks_tracker import router as tasks_router
from server.terminal import router as terminal_router
from server.websocket import router as ws_router
from server.workflows.routes import router as workflows_router
from server.workspace import router as workspace_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("whisper-studio")


class _QuietPollAccessLog(logging.Filter):
    """Drop uvicorn access-log lines for high-frequency UI poll endpoints so an
    in-progress index (the dialog polls index status every 2s) doesn't bury the
    terminal. Only these pure-polling GETs are hidden; every other request —
    builds, connects, removes, errors — still logs normally."""

    _NOISY = ("/api/workspace/index/status",)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self._NOISY)


def _raise_fd_soft_limit() -> None:
    """Bump RLIMIT_NOFILE soft to the hard cap so a burst of agents
    can't exhaust the macOS default of 256 file descriptors.

    The cascade we used to see on team-agent spawns started here:
        [Errno 24] Too many open files
        -> sqlite "unable to open database file"
        -> "Could not connect to bedrock-runtime…" (socket() returns 24)
        -> "Failed to resolve …" (resolver also needs sockets)

    Each agent owns its own toolchain (boto3 pool, sqlite handle,
    ripgrep / git subprocesses), so the headroom multiplies fast.
    macOS hard limit is normally 10240+, so raising the soft cap to
    match is free and dramatically reduces the chance of hitting
    the wall during a parallel team run.
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard == resource.RLIM_INFINITY:
            target = 4096
        elif soft < hard:
            target = hard
        else:
            target = soft
        if target > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log.info("RLIMIT_NOFILE raised from %d to %d (hard=%s)", soft, target, hard)
    except Exception as exc:
        log.warning("failed to raise RLIMIT_NOFILE: %s", exc)


_raise_fd_soft_limit()

# Import executor modules to trigger registration. Each module
# registers tool executors as a side-effect of being imported. We do
# this in a guarded loop so a single broken executor (e.g. a missing
# optional dep) doesn't take down the whole app at boot — the rest of
# the registry still loads, and the failing executor logs an error.
import importlib  # noqa: E402 — must come after logging.basicConfig

_EXECUTOR_MODULES = (
    "server.executors.web",
    "server.executors.code",
    "server.executors.content",
    "server.executors.terminal_run",
    "server.executors.preview",  # metadata-only — dispatch is in tool_router.py
    "server.executors.result_cache",
    "server.search",  # ws_grep / ws_glob
    "server.index",  # workspace_semantic_search
    "server.git.executor",
    "server.memory.executor",
)

for _modname in _EXECUTOR_MODULES:
    try:
        importlib.import_module(_modname)
    except Exception as _exc:  # noqa: BLE001 — boot-time best-effort
        log.error("Executor module %s failed to register: %s", _modname, _exc)
        record_boot_error(_modname, str(_exc))

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


def _warm_transcription_models() -> None:
    """Warm ONLY Parakeet at startup — nothing else.

    Parakeet is the streaming/record engine (config alias ``"streaming"``); loading
    it here makes the first mic/record click instant. Every other model stays lazy
    and loads on first real use: Whisper (only used for file transcription, and not
    the default engine) downloads/loads on demand, the speaker/diarization encoder
    loads on the first diarized utterance, and the chat/index models load when a
    turn or a build needs them. Keeping startup to a single resident model also
    avoids the MPS memory contention that could OOM an index build.

    Parakeet is warmed only when it is actually the active record engine; if the
    user has switched the engine to Whisper, nothing is eager-loaded at startup
    (Whisper still loads lazily on first record), honoring the Parakeet-only rule.
    """
    from server.asr import get_backend, resolve_name
    from server.infrastructure.config import get as config_get

    if resolve_name(config_get("transcription_backend")) != "parakeet":
        return
    try:
        get_backend("parakeet").preload()
    except Exception as e:
        log.warning("Parakeet warmup failed: %s", e)


@asynccontextmanager
async def lifespan(app):
    # Installed here (not at import) so it survives uvicorn's own logging
    # dictConfig, which runs before the lifespan and would otherwise reset the
    # access logger's filters.
    logging.getLogger("uvicorn.access").addFilter(_QuietPollAccessLog())
    # Quiet a cosmetic third-party flood: pdfminer (via MarkItDown) logs a
    # FontBBox warning for every malformed font descriptor while extracting
    # scanned/older PDFs during folder indexing. Harmless; keep ERROR+ only.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    run_migrations()
    init_skills()
    init_plugins(app)
    init_memory()
    # Start the git file watcher and point it at the connected workspace.
    # Watches .git/HEAD, .git/config, and the current branch ref — pushes
    # invalidations to the SSE subscribers so the UI updates within ~1s
    # of any out-of-band git change, without polling.
    from server.git.watcher import git_watcher
    from server.workspace import get_workspace_path

    git_watcher.start()
    git_watcher.set_workspace(get_workspace_path())
    cleanup_task = asyncio.create_task(cleanup_loop())
    mcp_task = asyncio.create_task(mcp_manager.start_all())
    # Warm the transcription stack in the background so the first
    # recording doesn't pay model-load latency. The websocket path loads
    # everything lazily anyway, so a warmup failure only costs the first
    # user a wait — best-effort by design.
    spawn(asyncio.to_thread(_warm_transcription_models), name="model-warmup")
    # Unified background-task framework: capture the server loop for
    # thread-safe session-event emission, then reconcile leases left
    # 'running' by a previous server process (dead shell pids fail cleanly,
    # surviving ones are re-adopted).
    from server.tasks.events import init_task_service
    from server.tasks.registry import reconcile_on_boot

    await init_task_service()
    reconcile_on_boot()

    # Flip orphaned workflow runs (server died mid-run) to 'stale'; journals
    # stay resumable.
    try:
        from server.workflows.manager import reconcile_stale as _wf_reconcile

        _wf_reconcile()
    except Exception as _exc:
        log.warning("workflow reconcile failed: %s", _exc)
    # Prune notifications past the retention window (30 days).
    from server.notifications import gc_old as _notifications_gc

    _notifications_gc()
    await init_scheduler()
    await init_index_scheduler()
    # Record our PID so the background index-refresh agent (launchd) knows the
    # app is running and stays idle, avoiding a double refresh of the same index.
    from server.index import agent as index_agent

    index_agent.mark_app_running()
    yield
    index_agent.mark_app_stopped()
    git_watcher.stop()
    await mcp_manager.stop_all()
    await preview_manager.stop_all()
    cleanup_task.cancel()
    mcp_task.cancel()
    # Wait for the cancellations to actually take effect before the
    # event loop tears down. Without an awaited gather, uvicorn may
    # close the loop while these tasks are still resolving CancelError,
    # leaving "Task was destroyed but it is pending" warnings — or
    # worse, half-done cleanup. ``return_exceptions=True`` keeps a
    # noisy cancellation from masking the shutdown path.
    await asyncio.gather(cleanup_task, mcp_task, return_exceptions=True)


app = FastAPI(lifespan=lifespan)

# Reject cross-site requests (CSRF / DNS-rebinding defense). The API is
# unauthenticated and bound to localhost; this stops a malicious web page you
# visit from POSTing to http://localhost:<port>/api/... and triggering, e.g.,
# the command-execution endpoints. See server/infrastructure/security.py.
from server.infrastructure.security import origin_guard  # noqa: E402

app.middleware("http")(origin_guard)

# Mount all routers
app.include_router(config_router)
app.include_router(data_retention_router)
app.include_router(result_cache_router)
app.include_router(mcp_router)
app.include_router(skills_router)
app.include_router(whisper_md_router)
app.include_router(memory_router)
app.include_router(workspace_router)
app.include_router(attachments_router)
app.include_router(chat_router)
app.include_router(sessions_router)
app.include_router(ws_router)
app.include_router(permissions_router)
app.include_router(hooks_router)
app.include_router(goals_router)
app.include_router(workflows_router)
app.include_router(ci_router)
app.include_router(tasks_router)
app.include_router(background_tasks_router)
app.include_router(notifications_router)
app.include_router(cron_router)
app.include_router(index_router)
app.include_router(plugins_router)
app.include_router(lsp_router)
app.include_router(lsp_proxy_router)
app.include_router(terminal_router)
app.include_router(buddy_router)
app.include_router(auto_mode_router)
app.include_router(doctor_router)
app.include_router(git_router)
app.include_router(feature_flags_router)
app.include_router(cost_router)
app.include_router(approval_router)
app.include_router(preview_router)
app.include_router(preview_install_router)
app.include_router(preview_screencast_router)
app.include_router(plans_router)

# Register declarative approval specs for built-in workspace tools.
# This is a function call (not a side-effect of import) so test code can
# register a fresh set without import-order surprises.
register_approval_defaults()


# Static files. Vite emits content-hashed filenames under dist/assets, so those
# are safe to cache immutably for a year (a new build = a new filename). Other
# static files (index.html, favicon) keep the default heuristic caching so a
# rebuild is picked up promptly.
class _CachedStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if "assets/" in path and resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


app.mount("/static", _CachedStatic(directory=os.path.join(BASE_DIR, "static")), name="static")


def _spa_index_response() -> Response:
    react_index = os.path.join(BASE_DIR, "static", "dist", "index.html")
    if os.path.exists(react_index):
        return FileResponse(react_index)
    return Response(
        content="Frontend build missing. Run: npm install && npm run build",
        status_code=503,
        media_type="text/plain",
    )


@app.get("/")
async def index():
    # Serve the React build; without one there is nothing to serve (the
    # old vanilla-JS template fallback was removed once it went stale).
    return _spa_index_response()


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return health_payload()


# SPA fallback: any GET that wasn't matched by an /api, /ws, or /static route
# above gets the index.html so client-side navigation survives a hard reload.
# The matcher excludes prefixes that the routers already own, so this stays a
# safe net rather than shadowing real handlers.
_SPA_RESERVED_PREFIXES = ("api/", "ws/", "static/", "health", "favicon.ico")


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if any(full_path == p.rstrip("/") or full_path.startswith(p) for p in _SPA_RESERVED_PREFIXES):
        # Let FastAPI return the real 404 from the matched router rather than
        # masking it with the SPA shell.
        return Response(status_code=404)
    return _spa_index_response()


if __name__ == "__main__":
    import uvicorn

    os.chdir(BASE_DIR)
    port = int(os.environ.get("PORT", 8000))
    # Bind to localhost by default so the dev server isn't exposed on
    # every coffee-shop / hotel / conference Wi-Fi the laptop joins.
    # Power users wanting LAN access set HOST=0.0.0.0 explicitly:
    #
    #   HOST=0.0.0.0 bash setup.sh --prod
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
