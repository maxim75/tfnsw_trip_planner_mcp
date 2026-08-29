"""End-to-end tests over real HTTP: transports, headers, health, tool listing."""

import socket
import threading
import time
from unittest import mock

import httpx2
import pytest
import uvicorn
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client
from tfnsw_trip_planner.models import Coordinate, Location
from tfnsw_trip_planner.models.enums import LocationType

from tfnsw_trip_planner_mcp.app import create_app

EXPECTED_TOOLS = {
    "find_stop",
    "find_stop_by_id",
    "best_stop",
    "plan_trip",
    "plan_trip_from_coordinate",
    "plan_cycling_trip",
    "get_departures",
    "get_alerts",
    "find_nearby",
    "get_vehicle_positions",
}


def make_location():
    return Location(
        id="10101331",
        name="Circular Quay",
        type=LocationType.STOP,
        coord=Coordinate(latitude=-33.8613, longitude=151.2107),
        modes=[1, 9],
        match_quality=1000,
        is_best=True,
        parent=None,
        building_number="",
        street_name="",
        properties={},
        distance=None,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    """Serve the real app on a loopback port for the duration of the module."""
    port = _free_port()
    # host="0.0.0.0" matches how the container runs, and keeps the SDK from
    # auto-enabling its localhost-only DNS-rebinding allowlist.
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


def http_client(base_url, **kwargs):
    return httpx2.AsyncClient(base_url=base_url, timeout=15, **kwargs)


async def test_health_is_reachable_without_a_key(base_url):
    async with http_client(base_url) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_root_documents_the_auth_contract(base_url):
    async with http_client(base_url) as client:
        response = await client.get("/")
    body = response.json()
    assert response.status_code == 200
    assert "X-API-Key" in body["authentication"]
    assert body["endpoints"]["streamable_http"] == "/mcp"


async def test_streamable_http_lists_every_tool(base_url):
    async with http_client(base_url, headers={"X-API-Key": "test-key"}) as client:
        async with streamable_http_client(f"{base_url}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS


async def test_tool_descriptions_are_populated(base_url):
    async with http_client(base_url, headers={"X-API-Key": "test-key"}) as client:
        async with streamable_http_client(f"{base_url}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

    for tool in tools.tools:
        assert tool.description, f"{tool.name} has no description for the model to read"


async def test_a_tool_call_uses_the_key_from_the_request_header(base_url):
    with mock.patch("tfnsw_trip_planner_mcp.auth.TripPlannerClient") as factory:
        factory.return_value.find_stop.return_value = []

        async with http_client(base_url, headers={"X-API-Key": "caller-key"}) as client:
            async with streamable_http_client(f"{base_url}/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("find_stop", {"query": "Circular Quay"})

    assert result.is_error is False
    # The key the caller sent on the HTTP request must be the one used upstream.
    assert factory.call_args.kwargs["api_key"] == "caller-key"


async def test_results_arrive_as_structured_content(base_url):
    location = make_location()
    with mock.patch("tfnsw_trip_planner_mcp.auth.TripPlannerClient") as factory:
        factory.return_value.find_stop.return_value = [location]

        async with http_client(base_url, headers={"X-API-Key": "caller-key"}) as client:
            async with streamable_http_client(f"{base_url}/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("find_stop", {"query": "Circular Quay"})

    # Clients (and the live tests) read results off structured_content, so the
    # dict a tool returns must survive the round trip intact.
    assert result.structured_content == {
        "count": 1,
        "locations": [
            {
                "id": "10101331",
                "name": "Circular Quay",
                "type": "stop",
                "coord": {"latitude": -33.8613, "longitude": 151.2107},
                "modes": [1, 9],
                "match_quality": 1000,
                "is_best": True,
                "parent": None,
                "building_number": "",
                "street_name": "",
                "properties": {},
                "distance": None,
            }
        ],
    }


async def test_two_callers_keys_do_not_leak_into_each_other(base_url):
    with mock.patch("tfnsw_trip_planner_mcp.auth.TripPlannerClient") as factory:
        factory.return_value.best_stop.return_value = None

        for key in ("key-one", "key-two"):
            async with http_client(base_url, headers={"X-API-Key": key}) as client:
                async with streamable_http_client(f"{base_url}/mcp", http_client=client) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await session.call_tool("best_stop", {"query": "Wynyard"})

    used = [call.kwargs["api_key"] for call in factory.call_args_list]
    assert used == ["key-one", "key-two"]


async def test_a_call_without_the_header_fails_with_actionable_guidance(base_url):
    async with http_client(base_url) as client:
        async with streamable_http_client(f"{base_url}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("find_stop", {"query": "Circular Quay"})

    assert result.is_error is True
    assert "X-API-Key" in result.content[0].text


async def test_sse_endpoint_is_mounted(base_url):
    # The legacy transport opens a stream and holds it, so just assert the route
    # exists and starts an event stream rather than driving a full session.
    async with http_client(base_url) as client:
        async with client.stream("GET", "/sse") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
