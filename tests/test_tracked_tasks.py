"""spawn() must hold a strong reference until completion and log failures."""

import asyncio

from server.infrastructure.async_tasks import _TASKS, spawn


def test_spawn_holds_reference_until_done():
    async def go():
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0)

        task = spawn(work(), name="test-work")
        await started.wait()
        assert task in _TASKS
        await task
        # Let the done-callback run.
        await asyncio.sleep(0)
        assert task not in _TASKS

    asyncio.run(go())


def test_spawn_logs_exception(caplog):
    async def go():
        async def boom():
            raise ValueError("kaboom")

        task = spawn(boom(), name="test-boom")
        # Wait for completion without retrieving the exception ourselves,
        # so the done-callback is what surfaces it.
        await asyncio.wait([task])
        await asyncio.sleep(0)

    with caplog.at_level("ERROR", logger="whisper-studio"):
        asyncio.run(go())

    assert any(
        "test-boom" in rec.getMessage() and "kaboom" in rec.getMessage() for rec in caplog.records
    )


def test_spawn_swallows_cancellation(caplog):
    async def go():
        async def forever():
            await asyncio.sleep(3600)

        task = spawn(forever(), name="test-cancel")
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait([task])
        await asyncio.sleep(0)
        assert task not in _TASKS

    with caplog.at_level("ERROR", logger="whisper-studio"):
        asyncio.run(go())

    assert not caplog.records
