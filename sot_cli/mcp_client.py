from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any
import os
import sys

# httpx-sse<0.4.4 references httpx.TransportError which was removed in httpx 0.28.x
# Shim: alias TransportError -> HTTPError so httpx-sse can subclass it at import time
import httpx
if not hasattr(httpx, "TransportError"):
    httpx.TransportError = httpx.HTTPError  # type: ignore[attr-defined]

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sot_cli.config.app import MCPServerConfig


class MCPManager:
    def __init__(self, servers_config: dict[str, MCPServerConfig]):
        self.servers_config = servers_config
        self.sessions: dict[str, ClientSession] = {}
        self.exit_stack = AsyncExitStack()
        self._tool_schemas: list[dict[str, Any]] = []
        self._tool_to_server: dict[str, str] = {}
        self._started = False

    async def start(self):
        if self._started:
            return
        self._started = True

        for name, config in self.servers_config.items():
            try:
                env = os.environ.copy()
                env.update(config.env)

                command = self._resolve_command(config.command)

                server_params = StdioServerParameters(
                    command=command,
                    args=config.args,
                    env=env
                )
                stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
                read, write = stdio_transport
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session

                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    tool_name = f"{name}__{tool.name}"
                    self._tool_to_server[tool_name] = name
                    self._tool_schemas.append({
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": tool.description or f"MCP tool {tool.name} from {name}",
                            "parameters": tool.inputSchema
                        }
                    })
            except Exception as e:
                # Log visible en stderr en vez de ocultarse en print bufferizado
                sys.stderr.write(f"\n[Warning] Failed to start MCP server '{name}': {e}\n")
                sys.stderr.flush()

    def _resolve_command(self, command: str) -> str:
        normalized = command.strip()
        if normalized in {"python", "python3"}:
            return sys.executable
        return normalized

    async def close(self):
        await self.exit_stack.aclose()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return self._tool_schemas

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._tool_to_server

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        server_name = self._tool_to_server.get(name)
        if not server_name:
            raise ValueError(f"Unknown MCP tool: {name}")
        session = self.sessions.get(server_name)
        if not session:
            raise ValueError(f"MCP server {server_name} is not connected")

        original_tool_name = name[len(server_name) + 2:]
        result = await session.call_tool(original_tool_name, arguments)

        if result.isError:
            raise RuntimeError(f"MCP Tool Error: {result.content}")

        text_parts = []
        for item in result.content:
            if item.type == "text":
                text_parts.append(item.text)
            else:
                text_parts.append(f"[{item.type} content]")

        return {"mcp_output": "\n".join(text_parts)}
