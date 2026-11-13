from __future__ import annotations

from mcp.server.fastmcp import FastMCP


server = FastMCP(
    "test",
    instructions="A minimal local MCP server for smoke tests.",
)


@server.tool(name="ping", description="Return a fixed response to confirm the MCP server is alive.")
async def ping() -> str:
    return "pong from test mcp"


@server.tool(name="add_numbers", description="Add two numbers and return the result.")
async def add_numbers(a: float, b: float) -> str:
    return str(a + b)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
