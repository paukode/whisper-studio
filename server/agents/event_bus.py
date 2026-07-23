"""
Agent progress event bus — per-session pub/sub for live agent activity.

Distinct from ``messaging.MessageBus`` (agent-to-agent mailboxes). The
event bus carries one-way progress notifications from running agents to
whatever surfaces them to the user (the chat SSE stream today).
"""

import asyncio
import logging
import threading

log = logging.getLogger("whisper-studio")

_MAX_QUEUE = 512


class AgentEventBus:
    """Thread-safe per-session event queue.

    Multiple subscribers per session are supported; ``publish`` fans the
    event out to every queue. Queues are bounded — if a subscriber falls
    behind, the event is dropped (a log warning is emitted).

    ``publish`` is safe to call from any thread: each subscriber records
    the event loop it lives on, and cross-thread delivery routes through
    ``loop.call_soon_threadsafe``. Cron jobs publish from a daemon
    thread, so this isn't optional.
    """

    def __init__(self) -> None:
        # Each subscriber is (queue, loop) so cross-thread publishes
        # can hand the put off to the right event loop.
        self._subscribers: dict[str, list[tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers.setdefault(session_id, []).append((q, loop))
        return q

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            entries = self._subscribers.get(session_id)
            if not entries:
                return
            entries[:] = [(q, listener) for (q, listener) in entries if q is not queue]
            if not entries:
                self._subscribers.pop(session_id, None)

    def publish(self, session_id: str, event: dict) -> None:
        """Non-blocking publish — drops the event if any subscriber is full.

        Safe to call from threads other than the subscriber's loop;
        delivery is scheduled via ``call_soon_threadsafe`` when needed.
        """
        with self._lock:
            entries = list(self._subscribers.get(session_id, ()))
        if not entries:
            return
        for q, loop in entries:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                # Same loop — direct put is fine and avoids a hop.
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning(
                        "event_bus: dropped event for session=%s (queue full)",
                        session_id,
                    )
            else:
                # Cross-thread (or no loop) — hop into the subscriber's loop.
                def _deliver(q=q, event=event, sid=session_id):
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        log.warning(
                            "event_bus: dropped event for session=%s (queue full)",
                            sid,
                        )

                try:
                    loop.call_soon_threadsafe(_deliver)
                except RuntimeError:
                    # Loop closed between snapshot and publish — drop silently.
                    pass

    def has_subscribers(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._subscribers.get(session_id))


event_bus = AgentEventBus()
