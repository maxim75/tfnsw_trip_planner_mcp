"""Smoke tests against the real TfNSW API.

Skipped unless TFNSW_API_KEY is set, so the default suite stays offline:

    TFNSW_API_KEY=<key> uv run pytest -m live
"""

import os
import socket
import threading
import time
from contextlib import asynccontextmanager

import httpx2
import pytest
import uvicorn
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tfnsw_trip_planner_mcp import server
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


@asynccontextmanager
async def mcp_session(base_url: str, api_key: str):
    """Open an initialized MCP session over Streamable HTTP.

    Deliberately a context manager used inside the test body rather than a
    pytest fixture: the transport and session hold anyio cancel scopes, and an
    async-generator fixture can be finalized in a different task than it was
    entered in, which raises "Attempted to exit cancel scope in a different
    task". Entering and exiting within one test coroutine keeps them paired.
    """
    async with httpx2.AsyncClient(timeout=45, headers={"X-API-Key": api_key}) as http:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def real_session(base_url: str):
    async with mcp_session(base_url, os.environ["TFNSW_API_KEY"]) as session:
        yield session


async def test_find_stop_returns_real_sydney_stops(live_server):
    async with real_session(live_server) as session:
        result = await session.call_tool("find_stop", {"query": "Circular Quay"})

    assert result.is_error is False, result.content[0].text
    payload = result.structured_content
    assert payload["count"] > 0
    assert all(loc["id"] for loc in payload["locations"])


async def test_find_stop_results_carry_names(live_server):
    # Was an xfail: tfnsw-trip-planner <=1.3.1 read the name only from
    # properties.STOP_NAME_WITH_PLACE, which stop_finder does not send, so every
    # result came back with name="". Fixed in 1.4.0 by falling back to the
    # top-level data["name"]. Without names a model cannot tell results apart.
    async with real_session(live_server) as session:
        result = await session.call_tool("find_stop", {"query": "Circular Quay"})

    payload = result.structured_content
    assert any("Circular Quay" in loc["name"] for loc in payload["locations"])


async def test_best_stop_resolves_a_named_stop(live_server):
    async with real_session(live_server) as session:
        result = await session.call_tool("best_stop", {"query": "Katoomba Station"})

    location = result.structured_content["location"]
    assert location["id"]
    assert "Katoomba" in location["name"]


async def test_departures_for_a_resolved_stop(live_server):
    async with real_session(live_server) as session:
        best = await session.call_tool("best_stop", {"query": "Circular Quay"})
        assert best.is_error is False, best.content[0].text
        stop_id = best.structured_content["location"]["id"]

        result = await session.call_tool("get_departures", {"stop_id": stop_id})

    assert stop_id == "200020", f"best_stop should resolve Circular Quay, got {stop_id!r}"
    assert result.is_error is False, result.content[0].text
    # A live network always has something scheduled from Circular Quay.
    assert result.structured_content["count"] > 0
    # StopEvent uses a different model than Location and does parse names.
    assert result.structured_content["departures"][0]["location"]["name"]


async def test_plan_trip_between_two_real_stops(live_server):
    async with real_session(live_server) as session:
        result = await session.call_tool(
            "plan_trip", {"origin_id": "200020", "destination_id": "200070"}
        )

    assert result.is_error is False, result.content[0].text
    payload = result.structured_content
    assert payload["count"] > 0
    assert payload["journeys"][0]["legs"], "a journey should have at least one leg"


async def test_alerts_endpoint_answers(live_server):
    async with real_session(live_server) as session:
        result = await session.call_tool("get_alerts", {})

    assert result.is_error is False, result.content[0].text
    assert result.structured_content["count"] >= 0


async def test_a_bad_key_surfaces_the_upstream_error(live_server):
    async with mcp_session(live_server, "definitely-not-valid") as session:
        result = await session.call_tool("find_stop", {"query": "Circular Quay"})

    assert result.is_error is True
    assert "definitely-not-valid" not in result.content[0].text


@pytest.mark.parametrize("mode", server.VEHICLE_POSITION_MODES)
async def test_every_advertised_vehicle_feed_actually_exists(live_server, mode):
    """Regression: "sydneytrains" was advertised but 404s at TfNSW.

    The tool rejects unknown feeds by consulting VEHICLE_POSITION_MODES, so a
    wrong entry there is worse than no check at all — it waves through a mode
    that can only fail. Only a live call can tell the two apart.
    """
    async with real_session(live_server) as session:
        result = await session.call_tool("get_vehicle_positions", {"mode": mode, "max_results": 1})

    if result.is_error:
        message = result.content[0].text
        if "403" in message:
            pytest.skip(f"key is not subscribed to the vehicle positions feed: {message[:80]}")
        pytest.fail(f"advertised feed {mode!r} failed: {message[:200]}")
    assert result.structured_content["count"] >= 0
