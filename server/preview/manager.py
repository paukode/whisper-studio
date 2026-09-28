"""PreviewSession + PreviewManager: named, concurrent dev-server + browser
sessions. Mirrors server/mcp.py's MCPManager shape.

A preview belongs to the chat session that started it (``owner``). Names stay
global (they resolve against the workspace's .whisper/launch.json), so another
chat can see and inspect a running preview, but only its owner may stop,
replace or drive it, and the refusal names the owning chat. The human kill
switch in Settings (a REST DELETE with no chat attached) is not restricted.

Also hosts the thin per-tool wrapper functions the approval executors call
(start_preview_session, stop_preview_session, navigate_in_preview, ...) —
no business logic duplicated in the approval layer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from server.preview.browser import BrowserSession
from server.preview.process import DevServerProcess, DevServerSpawnError

log = logging.getLogger("whisper-studio")


@dataclass
class PreviewSession:
    id: str
    process: DevServerProcess | None
    browser: BrowserSession = field(default_factory=BrowserSession)
    port: int | None = None
    url: str | None = None
    created_at: float = field(default_factory=time.time)
    # Chat session id that started this preview ("" when none was known).
    owner: str = ""


class PreviewNotOwned(Exception):
    """A chat tried to stop or replace a preview another chat owns."""

    def __init__(self, name: str, owner: str):
        super().__init__(name, owner)
        self.name = name
        self.owner = owner


def owned_by_other(session: PreviewSession, caller: str) -> bool:
    """True when ``caller`` (a chat session id) is not the preview's owner.
    An unattributed caller or an ownerless preview is never a mismatch."""
    return bool(session.owner and caller and session.owner != caller)


def server_exited(session: PreviewSession) -> bool:
    """True when the preview's dev server has exited (it crashed or was
    killed). A browser-only preview has no server and never counts."""
    return session.process is not None and not session.process.alive


class PreviewManager:
    """Tracks multiple concurrent named preview sessions."""

    def __init__(self):
        self._sessions: dict[str, PreviewSession] = {}
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start_session(
        self,
        name: str,
        *,
        command: list[str] | None,
        cwd: str,
        port: int | None = None,
        url: str | None = None,
        env: dict | None = None,
        owner: str = "",
    ) -> PreviewSession:
        async with self._get_lock():
            existing = self._sessions.get(name)
            if existing is not None:
                if owned_by_other(existing, owner):
                    raise PreviewNotOwned(name, existing.owner)
                if server_exited(existing):
                    # Its dev server is gone (the Live pane shows "Preview
                    # server stopped"): starting it again is a restart, not a
                    # clash. The browser still goes, so no page is leaked.
                    self._sessions.pop(name, None)
                    await _teardown(existing)
                else:
                    where = " in this chat" if existing.owner and existing.owner == owner else ""
                    raise ValueError(
                        f"Preview session '{name}' is already running{where}. Reuse it "
                        "(preview_navigate), or preview_stop it first to restart it."
                    )
            process = None
            if command:
                process = await DevServerProcess.spawn(command, cwd=cwd, env=env)
            session = PreviewSession(id=name, process=process, port=port, url=url, owner=owner)
            self._sessions[name] = session
            return session

    def get(self, name: str) -> PreviewSession | None:
        return self._sessions.get(name)

    async def stop_session(self, name: str, *, caller: str = "") -> bool:
        """Stop and forget a preview. ``caller`` is the chat asking: one that
        does not own the preview gets PreviewNotOwned, checked under the same
        lock as the removal. Empty (the Settings kill switch) stops any."""
        async with self._get_lock():
            session = self._sessions.get(name)
            if session is not None and owned_by_other(session, caller):
                raise PreviewNotOwned(name, session.owner)
            session = self._sessions.pop(name, None)
        if not session:
            return False
        # The session has already left the registry, so a teardown that stops
        # halfway leaves a server nothing can reach any more. It runs as its
        # own tracked task awaited under shield: a caller cancelled mid-stop
        # (Stop on the turn that auto-ran preview_stop) no longer skips it.
        from server.infrastructure.async_tasks import spawn

        await asyncio.shield(spawn(_teardown(session), name=f"preview-stop-{name}"))
        return True

    def list_sessions(self) -> list[dict]:
        return [
            {
                "id": s.id,
                "url": s.url,
                "port": s.port,
                "process_alive": s.process.alive if s.process else None,
                "browser_started": s.browser.page is not None,
                "created_at": s.created_at,
                "owner": s.owner,
            }
            for s in self._sessions.values()
        ]

    async def stop_all(self):
        for name in list(self._sessions.keys()):
            await self.stop_session(name)


async def _teardown(session: PreviewSession) -> None:
    """Kill the dev server, then close the browser. The kill goes first: its
    SIGTERM is sent before its first await, so nothing that happens later in
    the teardown can leave the server running and holding its port."""
    try:
        if session.process:
            await session.process.stop()
    finally:
        await session.browser.close()


preview_manager = PreviewManager()


def _require_session(name: str) -> PreviewSession:
    session = preview_manager.get(name)
    if not session:
        raise ValueError(
            f"No preview session named '{name}'. Call preview_start first, or check preview_list."
        )
    return session


async def owner_label(owner: str) -> str:
    """How a refusal names the owning chat: by its title when it has one."""
    title = ""
    try:
        from server.infrastructure.sessions import get_session_meta

        meta = await asyncio.to_thread(get_session_meta, owner)
        title = ((meta or {}).get("title") or "").strip()
    except Exception as e:  # noqa: BLE001 - a refusal must not fail on its label
        log.debug("preview owner label lookup failed: %s", e)
    return f'the chat "{title}"' if title else f"another chat (session {owner[:8]})"


async def _not_owned_message(name: str, owner: str, verb: str) -> str:
    return (
        f"Preview session '{name}' belongs to {await owner_label(owner)}; only that chat "
        f"can {verb} it. This chat can still inspect it (preview_screenshot, "
        "preview_snapshot, preview_logs, preview_console_logs, preview_network), or "
        "start its own preview with preview_start and a different session_name."
    )


async def _require_own_session(payload: dict, verb: str) -> PreviewSession:
    """The named preview when the calling chat may drive it; otherwise a
    ValueError carrying the reason (no such preview, or another chat's)."""
    name = payload.get("session_name", "")
    session = _require_session(name)
    if owned_by_other(session, str(payload.get("session_id") or "")):
        raise ValueError(await _not_owned_message(name, session.owner, verb))
    return session


# --- Approval-gated actions (thin wrappers; called from approval executors) ---


async def start_preview_session(payload: dict) -> tuple[bool, str]:
    """Start the preview a payload describes.

    A named config runs the command pinned into the payload when the tool was
    dispatched (server/preview/start_policy.py), which is what the approval
    decision and the card saw; launch.json is read here only for a payload
    with no pin (the Live pane's Restart button).
    """
    from server.preview.launch_config import resolve_launch_command
    from server.preview.start_policy import pinned_config
    from server.workspace.state import get_workspace_path

    name = (payload.get("session_name") or "").strip()
    if not name:
        return False, "session_name is required"

    config_workspace = None
    command = payload.get("runtimeExecutable")
    if command:
        args = payload.get("runtimeArgs") or []
        command = [str(command), *[str(a) for a in args]]
        port = payload.get("port")
    else:
        resolved = pinned_config(payload) or resolve_launch_command(name)
        if not resolved:
            return False, (
                f"No .whisper/launch.json entry named '{name}' and no runtimeExecutable given. "
                "Either add a launch config or pass runtimeExecutable/runtimeArgs directly."
            )
        command, port = resolved["command"], resolved["port"]
        config_workspace = resolved.get("workspace")

    cwd = payload.get("cwd") or config_workspace or get_workspace_path()
    if not cwd:
        return False, "cwd is required (no workspace connected)"

    try:
        await preview_manager.start_session(
            name,
            command=command,
            cwd=cwd,
            port=port,
            url=payload.get("url"),
            owner=str(payload.get("session_id") or ""),
        )
    except PreviewNotOwned as e:
        return False, (
            f"A preview named '{name}' is already running in {await owner_label(e.owner)}. "
            "Pick a different session_name for this chat's preview; the running one can "
            "be inspected from here but not replaced or stopped."
        )
    except (DevServerSpawnError, ValueError) as e:
        return False, str(e)
    return (
        True,
        f"Started preview session '{name}' (cwd={cwd}). Use preview_navigate to load a page.",
    )


async def stop_preview_session(payload: dict) -> tuple[bool, str]:
    name = (payload.get("session_name") or "").strip()
    if not name:
        return False, "session_name is required"
    try:
        stopped = await preview_manager.stop_session(
            name, caller=str(payload.get("session_id") or "")
        )
    except PreviewNotOwned as e:
        return False, await _not_owned_message(name, e.owner, "stop")
    if not stopped:
        return False, f"No such session '{name}'"
    return True, f"Stopped preview session '{name}'."


async def navigate_in_preview(payload: dict) -> tuple[bool, str]:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    url = (payload.get("url") or "").strip()
    try:
        session = await _require_own_session(payload, "navigate")
        if not url:
            return False, "url is required"
        await session.browser.ensure_started()
        await session.browser.page.goto(url)
        title = await session.browser.page.title()
        return True, f"Navigated to {session.browser.page.url} (title: {title!r})"
    except ValueError as e:
        return False, str(e)
    except PlaywrightTimeoutError:
        return False, (
            f"Navigation to {url!r} timed out — the dev server may still be "
            "starting or is unreachable. Check preview_logs, then retry preview_navigate."
        )
    except Exception as e:  # noqa: BLE001
        return False, f"Navigation failed: {e}"


async def click_in_preview(payload: dict) -> tuple[bool, str]:
    try:
        session = await _require_own_session(payload, "click in")
        selector = payload.get("selector") or ""
        if not selector:
            return False, "selector is required"
        await session.browser.ensure_started()
        if payload.get("doubleClick"):
            await session.browser.page.dblclick(selector)
        else:
            await session.browser.page.click(selector)
        return True, f"Clicked {selector!r}"
    except ValueError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"Click failed: {e}"


async def fill_in_preview(payload: dict) -> tuple[bool, str]:
    try:
        session = await _require_own_session(payload, "fill in")
        selector = payload.get("selector") or ""
        if not selector:
            return False, "selector is required"
        await session.browser.ensure_started()
        await session.browser.page.fill(selector, str(payload.get("value") or ""))
        return True, f"Filled {selector!r}"
    except ValueError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"Fill failed: {e}"


async def eval_in_preview(payload: dict) -> tuple[bool, str]:
    try:
        session = await _require_own_session(payload, "run code in")
        expression = payload.get("expression") or ""
        if not expression:
            return False, "expression is required"
        await session.browser.ensure_started()
        # page.evaluate ignores the context default timeout (it waits on the JS
        # promise), so bound it explicitly — a blocking expression must not wedge
        # the turn.
        result = await asyncio.wait_for(session.browser.page.evaluate(expression), timeout=15)
        return True, str(result)
    except ValueError as e:
        return False, str(e)
    except asyncio.TimeoutError:
        return False, "Eval timed out after 15s (long-running or blocking expression?)."
    except Exception as e:  # noqa: BLE001
        return False, f"Eval failed: {e}"


async def resize_preview(payload: dict) -> tuple[bool, str]:
    _PRESETS = {"mobile": (375, 812), "tablet": (768, 1024), "desktop": (1280, 800)}
    try:
        session = await _require_own_session(payload, "resize")
        await session.browser.ensure_started()
        preset = payload.get("preset")
        if preset in _PRESETS:
            w, h = _PRESETS[preset]
        else:
            w = int(payload.get("width") or 1280)
            h = int(payload.get("height") or 800)
        await session.browser.page.set_viewport_size({"width": w, "height": h})
        color_scheme = payload.get("colorScheme")
        if color_scheme in ("light", "dark"):
            await session.browser.page.emulate_media(color_scheme=color_scheme)
        return True, f"Resized to {w}x{h}" + (f", {color_scheme} mode" if color_scheme else "")
    except ValueError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"Resize failed: {e}"


# --- Read-only actions (no approval; called directly from router.py) ---


def list_preview_sessions(caller: str = "") -> str:
    """This chat's previews in full; other chats' as metadata only, since
    they can be inspected from here but never stopped or driven."""
    import json

    sessions = preview_manager.list_sessions()
    if not sessions:
        return "No preview sessions running."
    mine: list[dict] = []
    others: list[dict] = []
    for row in sessions:
        owner = row.pop("owner", "")
        (others if (owner and caller and owner != caller) else mine).append(row)
    out: dict = {"this_chat": mine}
    if others:
        out["other_chats"] = [
            {k: row[k] for k in ("id", "url", "port", "process_alive")} for row in others
        ]
        out["note"] = (
            "other_chats previews belong to other chats: inspect them if useful, but "
            "never stop or drive them, and pick a new session_name for your own."
        )
    return json.dumps(out, indent=2)


async def preview_logs_text(payload: dict) -> str:
    session = preview_manager.get(payload.get("session_name", ""))
    if not session:
        return f"No such session '{payload.get('session_name')}'"
    if not session.process:
        return "This session has no dev-server subprocess (browser-only session)."
    return await session.process.logs(
        stream=payload.get("stream", "both"), tail_bytes=int(payload.get("tail_bytes", 8192))
    )


def preview_console_text(payload: dict) -> str:
    session = preview_manager.get(payload.get("session_name", ""))
    if not session:
        return f"No such session '{payload.get('session_name')}'"
    return session.browser.console_text(
        level=payload.get("level"), lines=int(payload.get("lines", 100))
    )


def preview_network_text(payload: dict) -> str:
    session = preview_manager.get(payload.get("session_name", ""))
    if not session:
        return f"No such session '{payload.get('session_name')}'"
    return session.browser.network_text(
        only_failed=(payload.get("filter") == "failed"), lines=int(payload.get("lines", 100))
    )


async def preview_snapshot_text(payload: dict) -> str:
    session = preview_manager.get(payload.get("session_name", ""))
    if not session or session.browser.page is None:
        return "No page loaded yet for this session — call preview_navigate first."
    snapshot = await session.browser.page.aria_snapshot()
    return snapshot or "(empty accessibility tree)"


async def preview_inspect_text(payload: dict) -> str:
    session = preview_manager.get(payload.get("session_name", ""))
    if not session or session.browser.page is None:
        return "No page loaded yet for this session — call preview_navigate first."
    selector = payload.get("selector") or ""
    if not selector:
        return "selector is required"
    styles = payload.get("styles") or [
        "display",
        "position",
        "width",
        "height",
        "color",
        "background-color",
        "padding",
        "margin",
    ]
    try:
        result = await session.browser.page.eval_on_selector(
            selector,
            """(el, styleProps) => {
                const cs = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const styles = {};
                for (const p of styleProps) styles[p] = cs.getPropertyValue(p);
                return {
                    tagName: el.tagName, id: el.id, className: el.className,
                    textContent: (el.textContent || '').slice(0, 200),
                    boundingBox: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    styles,
                };
            }""",
            styles,
        )
        import json

        return json.dumps(result, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Inspect failed: {e}"


async def preview_screenshot_sentinel(payload: dict) -> str:
    from server.preview.screenshot import take_screenshot

    name = payload.get("session_name", "")
    session = preview_manager.get(name)
    if not session:
        return f"No such session '{name}'"
    await session.browser.ensure_started()
    return await take_screenshot(name, session.browser.page)
