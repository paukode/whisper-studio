"""web_search provider routing: Tavily if a key is set, else the AgentCore
browser if enabled, else a setup message. No network calls."""

import sys
import types

import server.executors.web as web


def test_routes_to_setup_message_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(web, "config_get", lambda k, d="": "")
    monkeypatch.setattr(web, "_agentcore_browser_enabled", lambda: False)
    r = web.exec_web_search({"query": "x"}, None, None)
    assert "isn't set up" in r and "AgentCore" in r and "Tavily" in r


def test_routes_to_agentcore_when_enabled_and_no_tavily(monkeypatch):
    monkeypatch.setattr(web, "config_get", lambda k, d="": "")
    monkeypatch.setattr(web, "_agentcore_browser_enabled", lambda: True)
    r = web.exec_web_search({"query": "weather in Warsaw"}, None, None)
    assert "start_browser_session" in r and "stop_browser_session" in r
    assert "weather in Warsaw" in r  # the query is handed to the browser


def test_tavily_key_takes_precedence(monkeypatch):
    monkeypatch.setattr(
        web, "config_get", lambda k, d="": "tvly-key" if k == "tavily_api_key" else d
    )
    monkeypatch.setattr(
        web, "_agentcore_browser_enabled", lambda: True
    )  # ignored when Tavily is set

    class FakeClient:
        def __init__(self, api_key):
            pass

        def search(self, q, max_results=5):
            return {"results": [{"title": "T", "url": "https://u", "content": "c"}]}

    fake = types.ModuleType("tavily")
    fake.TavilyClient = FakeClient
    monkeypatch.setitem(sys.modules, "tavily", fake)
    r = web.exec_web_search({"query": "x"}, None, None)
    assert "Title: T" in r and "https://u" in r


def test_agentcore_detection_reads_enabled_mcp(monkeypatch):
    import server.mcp as mcp

    monkeypatch.setattr(
        mcp.mcp_manager,
        "load_config",
        lambda: {
            "servers": {
                "Bedrock AgentCore": {
                    "args": ["awslabs.amazon-bedrock-agentcore-mcp-server@latest"],
                    "enabled": True,
                },
                "Other": {"args": ["some-other-server"], "enabled": True},
            }
        },
    )
    assert web._agentcore_browser_enabled() is True
    monkeypatch.setattr(
        mcp.mcp_manager,
        "load_config",
        lambda: {
            "servers": {
                "Bedrock AgentCore": {
                    "args": ["awslabs.amazon-bedrock-agentcore-mcp-server@latest"],
                    "enabled": False,
                }
            }
        },
    )
    assert web._agentcore_browser_enabled() is False
