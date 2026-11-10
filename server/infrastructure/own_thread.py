"""Run one blocking call on a thread of its own, awaitable from the loop.

A Bedrock call blocks its thread for as long as the answer streams, often
minutes. On a shared pool every answer in flight held a worker, so once the
answers filled the pool the next answer, and every tool call queued behind
them, waited with nothing on screen. A thread for each call has no such
ceiling: the session limit (MAX_ACTIVE_SESSIONS in src/hooks/useChatStream.ts)
and the agent limits bound how many run at once. The thread is a daemon, so a
reply that still streams never holds up the app's exit.
"""

import asyncio
import threading
from collections.abc import Callable


def run_on_own_thread[T](fn: Callable[[], T], *, name: str) -> asyncio.Future[T]:
    """Start ``fn`` on a new daemon thread. The returned future settles with
    its result or its exception. Call it from the running loop."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def settle(result, error: BaseException | None) -> None:
        if future.done():  # the awaiting task was cancelled
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def run() -> None:
        try:
            outcome = (fn(), None)
        except BaseException as error:  # noqa: BLE001 - handed to the awaiting task
            outcome = (None, error)
        try:
            loop.call_soon_threadsafe(settle, *outcome)
        except RuntimeError:
            pass  # the loop closed (shutdown): nobody is left to read it

    threading.Thread(target=run, name=name, daemon=True).start()
    return future
