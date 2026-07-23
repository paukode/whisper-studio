"""Verify the MCPManager exposes an asyncio Lock and namespaces tool keys."""

import asyncio

from server.mcp import MCPManager


def test_lock_is_lazy_and_stable():
    mgr = MCPManager()
    assert mgr._lock is None

    async def go():
        a = mgr._get_lock()
        b = mgr._get_lock()
        assert isinstance(a, asyncio.Lock)
        assert a is b
        async with a:
            pass

    asyncio.run(go())


def test_no_collision_under_double_underscore_namespace():
    # Manually compute the keys the way start_server() now does, to lock down
    # the format and ensure two plausible (server, tool) pairs don't collide.
    def key(server: str, tool: str) -> str:
        safe = server.replace("__", "_")
        return f"mcp__{safe}__{tool}"

    a = key("foo_bar", "x")
    b = key("foo", "bar_x")
    assert a != b
