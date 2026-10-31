"""Cancellation cannot discard an ASR close queued behind another decoder job."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import server.websocket as ws_mod


@pytest.mark.parametrize("ending", ["disconnect", "stop"])
def test_cancelled_socket_still_closes_its_queued_asr_session(monkeypatch, ending):
    release = threading.Event()
    occupied = threading.Event()

    class Session:
        close_count = 0

        def process(self, audio):
            return []

        def finish(self):
            return []

        def close(self):
            self.close_count += 1

    session = Session()

    async def scenario():
        loop = asyncio.get_running_loop()
        close_queued = asyncio.Event()

        class Executor(ThreadPoolExecutor):
            close_job = None

            def submit(self, fn, /, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                if fn == session.close:
                    self.close_job = future
                    loop.call_soon_threadsafe(close_queued.set)
                return future

        executor = Executor(max_workers=1)
        backend = SimpleNamespace(
            executor=executor,
            is_loaded=lambda: True,
            create_session=lambda: session,
        )
        monkeypatch.setattr(ws_mod, "get_backend", lambda name: backend)
        settings = {"transcription_backend": "whisper", "translate_mode": "off"}
        monkeypatch.setattr(
            ws_mod, "config_get", lambda key, default=None: settings.get(key, default)
        )

        async def occupy_executor():
            def block():
                occupied.set()
                assert release.wait(5), "test did not release the decoder worker"

            executor.submit(block)
            assert await asyncio.to_thread(occupied.wait, 2)

        class Socket:
            headers = {"origin": "http://localhost"}
            reads = 0

            async def accept(self):
                pass

            async def send_json(self, value):
                pass

            async def close(self, **kwargs):
                await occupy_executor()

            async def receive(self):
                self.reads += 1
                if self.reads == 1:
                    return {"type": "websocket.receive", "bytes": b"\x00\x00" * 1600}
                if ending == "stop":
                    return {"type": "websocket.receive", "text": json.dumps({"type": "stop"})}
                await occupy_executor()
                return {"type": "websocket.disconnect"}

        task = asyncio.create_task(ws_mod.websocket_endpoint(Socket(), dictation="1"))
        try:
            await asyncio.wait_for(close_queued.wait(), 2)
            assert session.close_count == 0
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            executor.shutdown(wait=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert executor.close_job is not None
        assert not executor.close_job.cancelled(), "socket cancellation discarded its queued close"
        assert session.close_count == 1

    asyncio.run(scenario())
