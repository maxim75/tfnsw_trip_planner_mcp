"""Smoke tests against the real TfNSW API.

Skipped unless TFNSW_API_KEY is set, so the default suite stays offline:

    TFNSW_API_KEY=<key> uv run pytest -m live
"""

import os
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tfnsw_trip_planner_mcp.app import create_app

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("TFNSW_API_KEY"),
        reason="set TFNSW_API_KEY to run tests against the real TfNSW API",
    ),
]


@pytest.fixture(scope="module")
def live_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    config = uvicorn.Config(
        create_app(host="0.0.0.0"), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
async def session(live_server):
    """An initialized MCP session carrying the real API key."""
    headers = {"X-API-Key": os.environ["TFNSW_API_KEY"]}
    async with httpx2.AsyncClient(timeout=45, headers=headers) as http:
        async with streamable_http_client(f"{live_server}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                yield mcp_session


async def test_find_stop_returns_real_sydney_stops(session):
    result = await session.call_tool("find_stop", {"query": "Circular Quay"})

    assert result.is_error is False, result.content[0].text
    payload = result.structured_content
    assert payload["count"] > 0
    assert any("Circular Quay" in loc["name"] for loc in payload["locations"])


async def test_departures_for_a_resolved_stop(session):
    best = await session.call_tool("best_stop", {"query": "Circular Quay"})
    assert best.is_error is False, best.content[0].text
    stop_id = best.structured_content["location"]["id"]

    result = await session.call_tool("get_departures", {"stop_id": stop_id})

    assert result.is_error is False, result.content[0].text
    # A live network always has something scheduled from Circular Quay.
    assert result.structured_content["count"] > 0


async def test_a_bad_key_surfaces_the_upstream_error(live_server):
    async with httpx2.AsyncClient(
        timeout=45, headers={"X-API-Key": "definitely-not-valid"}
    ) as http:
        async with streamable_http_client(f"{live_server}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                result = await mcp_session.call_tool("find_stop", {"query": "Circular Quay"})

    assert result.is_error is True
    assert "definitely-not-valid" not in result.content[0].text
