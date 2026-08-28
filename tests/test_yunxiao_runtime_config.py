"""Yunxiao MCP provider and transport configuration tests."""
from __future__ import annotations

import pytest

from src.agents import reviewer
from src.agents.mcp_client import McpClientError, SseMcpClient, create_http_mcp_clients


def test_openai_yunxiao_stdio_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("YUNXIAO_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("YUNXIAO_MCP_URL", raising=False)

    with pytest.raises(ValueError, match="OpenAI runtime 不支持 stdio MCP"):
        reviewer._get_yunxiao_mcp_config()  # noqa: SLF001


def test_openai_yunxiao_sse_configuration_is_preserved(monkeypatch):
    monkeypatch.setenv("YUNXIAO_MCP_TRANSPORT", "sse")
    monkeypatch.setenv("YUNXIAO_MCP_URL", "https://example.test/sse")
    monkeypatch.setenv("YUNXIAO_ACCESS_TOKEN", "test-token")

    config = reviewer._get_yunxiao_mcp_config()  # noqa: SLF001

    assert config["transport"] == "sse"
    assert config["server_url"] == "https://example.test/sse"
    assert config["headers"]["Authorization"] == "Bearer test-token"


def test_openai_yunxiao_configuration_requires_url(monkeypatch):
    monkeypatch.setenv("YUNXIAO_MCP_TRANSPORT", "http")
    monkeypatch.delenv("YUNXIAO_MCP_URL", raising=False)

    with pytest.raises(ValueError, match="缺少 YUNXIAO_MCP_URL"):
        reviewer._get_yunxiao_mcp_config()  # noqa: SLF001


@pytest.mark.parametrize("transport", ["stdio", "websocket"])
def test_openai_mcp_factory_rejects_explicit_unsupported_transport(transport: str):
    with pytest.raises(McpClientError, match="不支持"):
        create_http_mcp_clients(
            [{"transport": transport, "server_url": "https://example.test/mcp"}]
        )


def test_openai_mcp_factory_rejects_command_only_stdio_config():
    with pytest.raises(McpClientError, match="command-only stdio MCP"):
        create_http_mcp_clients([{"command": "npx", "args": ["-y", "server"]}])


def test_openai_mcp_factory_allows_empty_server_list():
    assert create_http_mcp_clients([]) == []


@pytest.mark.parametrize("transport", ["http", "streamable_http", "sse"])
def test_openai_mcp_factory_rejects_supported_transport_without_url(transport: str):
    with pytest.raises(McpClientError, match="缺少 server_url/url"):
        create_http_mcp_clients([{"transport": transport}])


def test_openai_mcp_factory_accepts_explicit_sse_transport():
    clients = create_http_mcp_clients(
        [{"transport": "sse", "server_url": "https://example.test/messages"}]
    )
    try:
        assert len(clients) == 1
        assert isinstance(clients[0], SseMcpClient)
    finally:
        for client in clients:
            client.close()


def test_openai_yunxiao_configuration_rejects_unknown_transport(monkeypatch):
    monkeypatch.setenv("YUNXIAO_MCP_TRANSPORT", "websocket")
    monkeypatch.setenv("YUNXIAO_MCP_URL", "https://example.test/mcp")

    with pytest.raises(ValueError, match="不支持的 YUNXIAO_MCP_TRANSPORT"):
        reviewer._get_yunxiao_mcp_config()  # noqa: SLF001


@pytest.mark.asyncio
async def test_claude_provider_ignores_openai_stdio_guard(monkeypatch):
    class FakeRuntime:
        options = None

        async def run(self, _prompt, options):
            self.options = options
            return [
                {"type": "assistant", "content": ["done"]},
                {"type": "result", "subtype": "success", "content": "done"},
            ]

    fake_runtime = FakeRuntime()
    monkeypatch.setenv("AGENT_PROVIDER", "claude")
    monkeypatch.setenv("YUNXIAO_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(reviewer, "create_agent_runtime", lambda: fake_runtime)
    agent = reviewer.CodeReviewAgent(custom_hooks={})

    result = await agent.review_yunxiao_mr("repo", "1", auto_comment=False)

    assert result["is_error"] is False
    assert fake_runtime.options.remote_mcp_servers == []
    assert "yunxiao" in fake_runtime.options.claude_mcp_servers
