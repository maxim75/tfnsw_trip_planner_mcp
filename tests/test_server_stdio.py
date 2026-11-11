"""End-to-end tests over stdio: spawn the real entry point as a subprocess."""

import sys

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from test_server_http import EXPECTED_TOOLS

from tfnsw_trip_planner_mcp.auth import API_KEY_ENV


def _server(*args: str, **env: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "tfnsw_trip_planner_mcp", *args],
        env={API_KEY_ENV: "", "MCP_TRANSPORT": "", **env},
    )


async def test_transport_flag_serves_the_handshake_over_stdio():
    async with stdio_client(_server("--transport", "stdio")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS


async def test_mcp_transport_env_var_still_selects_stdio():
    async with stdio_client(_server(MCP_TRANSPORT="stdio")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert len(tools.tools) == len(EXPECTED_TOOLS)


async def test_tool_call_without_a_key_names_the_env_var():
    async with stdio_client(_server("--transport", "stdio")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("find_stop", {"query": "Circular Quay"})
    assert result.is_error is True
    assert API_KEY_ENV in result.content[0].text
