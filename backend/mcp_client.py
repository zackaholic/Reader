"""
mcp_client.py — persistent MCP client that keeps a single subprocess + session
alive for the life of the Flask process.

Previously we spawned a fresh subprocess per tool call (~300-500ms cold start
+ MCP handshake each time). Now we start one subprocess on boot, hold the
session open, and reuse it for every call.

Flask is sync/threaded, so we run a dedicated asyncio event loop in a
background thread and dispatch coroutines to it via run_coroutine_threadsafe.
"""

import asyncio
import sys
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_SERVER_PATH = Path(__file__).parent.parent / "mcp_server" / "server.py"


class _MCPClient:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._session: ClientSession | None = None
        self._tools: list[dict] | None = None
        self._ready = threading.Event()

        # Run the event loop forever in a background daemon thread
        t = threading.Thread(target=self._loop.run_forever, daemon=True)
        t.start()

        # Start the long-lived MCP connection coroutine
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop)

        # Block until the session is initialized (or raise if it takes too long)
        if not self._ready.wait(timeout=30):
            raise RuntimeError("MCP server failed to start within 30s")

    async def _connect(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SERVER_PATH)],
            env=None,
            cwd=str(MCP_SERVER_PATH.parent),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session

                # Cache tool definitions — they never change at runtime
                result = await session.list_tools()
                self._tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema,
                    }
                    for t in result.tools
                ]

                self._ready.set()

                # Hold the session open forever (until process exits)
                await asyncio.get_event_loop().create_future()

    def _run(self, coro):
        """Submit a coroutine to the background loop and block until done."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def list_tools(self) -> list[dict]:
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> str:
        async def _call():
            result = await self._session.call_tool(name, arguments)
            parts = [block.text for block in result.content if hasattr(block, "text")]
            return "\n".join(parts)
        return self._run(_call())


# Module-level singleton — initialized once when Flask imports this module
_client = _MCPClient()


def list_tools() -> list[dict]:
    return _client.list_tools()


def call_tool(name: str, arguments: dict) -> str:
    return _client.call_tool(name, arguments)
