"""Tests for the 10 MCP tools: argument mapping, output shape, error mapping."""

from datetime import datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from tfnsw_trip_planner import APIError, NetworkError
from tfnsw_trip_planner.models import Coordinate, Location
from tfnsw_trip_planner.models.enums import CyclingProfile, LocationType

from tfnsw_trip_planner_mcp import server
from tfnsw_trip_planner_mcp.server import _capped, parse_when


def make_location(name="Circular Quay", loc_id="10101331"):
    return Location(
        id=loc_id,
        name=name,
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


# --------------------------------------------------------------------------
# parse_when
# --------------------------------------------------------------------------


def test_parse_when_returns_none_for_none():
    assert parse_when(None) is None


def test_parse_when_accepts_naive_iso_and_leaves_it_naive():
    # The library reads a naive datetime as Sydney local time, which is what a
    # caller asking for "09:15" almost certainly means.
    assert parse_when("2026-08-30T09:15") == datetime(2026, 8, 30, 9, 15)


def test_parse_when_preserves_an_explicit_offset():
    parsed = parse_when("2026-08-30T09:15:00+10:00")
    assert parsed.utcoffset().total_seconds() == 36000


def test_parse_when_accepts_a_bare_date():
    assert parse_when("2026-08-30") == datetime(2026, 8, 30, 0, 0)


@pytest.mark.parametrize("bad", ["tomorrow", "30/08/2026", "09:15", ""])
def test_parse_when_rejects_unparseable_values_loudly(bad):
    # Silently dropping a bad `when` would return departures for the wrong time,
    # which is worse than an error.
    with pytest.raises(ToolError) as excinfo:
        parse_when(bad)
    assert "ISO 8601" in str(excinfo.value)


# --------------------------------------------------------------------------
# Result capping and shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_capped_never_returns_more_than_asked_for_odd_limits(bad):
    # `items[:-1]` would return all-but-one, silently un-capping the result and
    # reintroducing the oversized payload that breaks the SSE transport. A model
    # passing -1 to mean "no limit" is a realistic way to trigger that.
    result = _capped(list(range(281)), bad, "alerts")

    assert result["count"] == 281
    assert result["returned"] == 1
    assert len(result["alerts"]) == 1


def test_capped_passes_through_when_under_the_limit():
    result = _capped([1, 2], 50, "alerts")

    assert result == {"count": 2, "returned": 2, "alerts": [1, 2]}


def test_capped_without_a_limit_reports_everything():
    result = _capped(list(range(7)), None, "journeys")

    assert result["count"] == 7
    assert result["returned"] == 7
    assert len(result["journeys"]) == 7


LIST_TOOL_KEYS = {
    "find_stop": "locations",
    "plan_trip": "journeys",
    "plan_trip_from_coordinate": "journeys",
    "plan_cycling_trip": "journeys",
    "get_departures": "departures",
    "get_alerts": "alerts",
    "find_nearby": "locations",
    "get_vehicle_positions": "vehicles",
}

LIST_TOOL_ARGS = {
    "find_stop": {"query": "x"},
    "plan_trip": {"origin_id": "A", "destination_id": "B"},
    "plan_trip_from_coordinate": {"latitude": 1.0, "longitude": 2.0, "destination_id": "B"},
    "plan_cycling_trip": {"origin_id": "A", "destination_id": "B"},
    "get_departures": {"stop_id": "A"},
    "get_alerts": {},
    "find_nearby": {"latitude": 1.0, "longitude": 2.0},
    "get_vehicle_positions": {"mode": "buses"},
}

LIBRARY_METHOD = {"get_vehicle_positions": "vehicle_positions"}


@pytest.mark.parametrize("tool_name", sorted(LIST_TOOL_KEYS))
async def test_every_list_tool_returns_the_same_shape(tool_name, ctx, client):
    # One shape for every list-returning tool, so a caller can always read
    # `returned` and always trust `count` to be the true total.
    getattr(client, LIBRARY_METHOD.get(tool_name, tool_name)).return_value = ["a", "b"]

    result = await getattr(server, tool_name)(**LIST_TOOL_ARGS[tool_name], ctx=ctx)

    assert set(result) == {"count", "returned", LIST_TOOL_KEYS[tool_name]}
    assert result["count"] == 2
    assert result["returned"] == 2


# --------------------------------------------------------------------------
# Stop finding
# --------------------------------------------------------------------------


async def test_find_stop_forwards_arguments_and_wraps_results(ctx, client):
    client.find_stop.return_value = [make_location(), make_location("Wynyard", "10101100")]

    result = await server.find_stop(query="Circular Quay", ctx=ctx)

    client.find_stop.assert_called_once_with(
        query="Circular Quay", location_type="any", max_results=10
    )
    assert result["count"] == 2
    assert [loc["name"] for loc in result["locations"]] == ["Circular Quay", "Wynyard"]
    assert result["locations"][0]["type"] == "stop"


async def test_find_stop_passes_through_overrides(ctx, client):
    client.find_stop.return_value = []

    result = await server.find_stop(query="Town Hall", location_type="platform", limit=3, ctx=ctx)

    # `limit` is an upstream query limit, so it reaches the library as
    # max_results rather than truncating a fetched list locally.
    client.find_stop.assert_called_once_with(
        query="Town Hall", location_type="platform", max_results=3
    )
    assert result == {"count": 0, "returned": 0, "locations": []}


async def test_find_stop_by_id_returns_a_single_location(ctx, client):
    client.find_stop_by_id.return_value = make_location()

    result = await server.find_stop_by_id(stop_id="10101331", ctx=ctx)

    client.find_stop_by_id.assert_called_once_with(stop_id="10101331")
    assert result["location"]["id"] == "10101331"


async def test_find_stop_by_id_returns_null_when_not_found(ctx, client):
    client.find_stop_by_id.return_value = None

    assert await server.find_stop_by_id(stop_id="nope", ctx=ctx) == {"location": None}


async def test_best_stop_returns_a_single_location(ctx, client):
    client.best_stop.return_value = make_location()

    result = await server.best_stop(query="Circular Quay", ctx=ctx)

    client.best_stop.assert_called_once_with(query="Circular Quay")
    assert result["location"]["name"] == "Circular Quay"


# --------------------------------------------------------------------------
# Trip planning
# --------------------------------------------------------------------------


async def test_plan_trip_uses_library_defaults(ctx, client):
    client.plan_trip.return_value = []

    await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    client.plan_trip.assert_called_once_with(
        origin_id="A",
        destination_id="B",
        when=None,
        arrive_by=False,
        origin_type="stop",
        destination_type="stop",
        realtime=True,
        wheelchair=False,
    )


async def test_plan_trip_parses_when_and_forwards_flags(ctx, client):
    client.plan_trip.return_value = []

    await server.plan_trip(
        origin_id="A",
        destination_id="B",
        when="2026-08-30T09:15",
        arrive_by=True,
        origin_type="coord",
        destination_type="poi",
        realtime=False,
        wheelchair=True,
        ctx=ctx,
    )

    kwargs = client.plan_trip.call_args.kwargs
    assert kwargs["when"] == datetime(2026, 8, 30, 9, 15)
    assert kwargs["arrive_by"] is True
    assert kwargs["origin_type"] == "coord"
    assert kwargs["destination_type"] == "poi"
    assert kwargs["realtime"] is False
    assert kwargs["wheelchair"] is True


async def test_plan_trip_wraps_journeys(ctx, client):
    client.plan_trip.return_value = ["j1", "j2", "j3"]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert result == {"count": 3, "returned": 3, "journeys": ["j1", "j2", "j3"]}


async def test_plan_trip_from_coordinate_forwards_the_coordinate(ctx, client):
    client.plan_trip_from_coordinate.return_value = []

    await server.plan_trip_from_coordinate(
        latitude=-33.8613, longitude=151.2107, destination_id="B", ctx=ctx
    )

    client.plan_trip_from_coordinate.assert_called_once_with(
        latitude=-33.8613,
        longitude=151.2107,
        destination_id="B",
        when=None,
        arrive_by=False,
        realtime=True,
        wheelchair=False,
    )


async def test_plan_cycling_trip_coerces_the_profile_to_the_library_enum(ctx, client):
    client.plan_cycling_trip.return_value = []

    await server.plan_cycling_trip(origin_id="A", destination_id="B", profile="EASIER", ctx=ctx)

    kwargs = client.plan_cycling_trip.call_args.kwargs
    assert kwargs["profile"] is CyclingProfile.EASIER


async def test_plan_cycling_trip_defaults_match_the_library(ctx, client):
    client.plan_cycling_trip.return_value = []

    await server.plan_cycling_trip(origin_id="A", destination_id="B", ctx=ctx)

    client.plan_cycling_trip.assert_called_once_with(
        origin_id="A",
        destination_id="B",
        profile=CyclingProfile.MODERATE,
        when=None,
        bike_only=True,
        max_time_minutes=240,
        cycle_speed=16,
    )


# --------------------------------------------------------------------------
# Departures and alerts
# --------------------------------------------------------------------------


async def test_get_departures_forwards_arguments(ctx, client):
    client.get_departures.return_value = []

    await server.get_departures(
        stop_id="200020", when="2026-08-30T09:15", platform_id="1", realtime=False, ctx=ctx
    )

    client.get_departures.assert_called_once_with(
        stop_id="200020",
        when=datetime(2026, 8, 30, 9, 15),
        platform_id="1",
        realtime=False,
    )


async def test_get_departures_wraps_results(ctx, client):
    client.get_departures.return_value = ["d1", "d2"]

    assert await server.get_departures(stop_id="200020", ctx=ctx) == {
        "count": 2,
        "returned": 2,
        "departures": ["d1", "d2"],
    }


async def test_get_alerts_forwards_arguments(ctx, client):
    client.get_alerts.return_value = []

    await server.get_alerts(stop_id="200020", current_only=False, ctx=ctx)

    client.get_alerts.assert_called_once_with(when=None, stop_id="200020", current_only=False)


async def test_get_alerts_wraps_results(ctx, client):
    client.get_alerts.return_value = ["a1"]

    assert await server.get_alerts(ctx=ctx) == {"count": 1, "returned": 1, "alerts": ["a1"]}


async def test_get_alerts_caps_results_and_reports_the_real_total(ctx, client):
    # A network-wide alert fetch returns ~283 alerts / 1.3MB of JSON, which is
    # more than a single SSE event may carry (1MiB) and far more than a model
    # can read. Cap it, but keep the true total visible.
    client.get_alerts.return_value = list(range(283))

    result = await server.get_alerts(max_results=5, ctx=ctx)

    assert result["count"] == 283
    assert result["returned"] == 5
    assert result["alerts"] == [0, 1, 2, 3, 4]


async def test_get_alerts_default_cap_keeps_the_payload_transportable(ctx, client):
    client.get_alerts.return_value = list(range(283))

    result = await server.get_alerts(ctx=ctx)

    assert result["returned"] < 283, "an unfiltered alert fetch must be capped by default"


# --------------------------------------------------------------------------
# Coordinates and vehicles
# --------------------------------------------------------------------------


async def test_find_nearby_forwards_arguments(ctx, client):
    client.find_nearby.return_value = [make_location()]

    result = await server.find_nearby(
        latitude=-33.8613, longitude=151.2107, radius_m=1000, draw_class=2, ctx=ctx
    )

    client.find_nearby.assert_called_once_with(
        latitude=-33.8613,
        longitude=151.2107,
        radius_m=1000,
        type_1="GIS_POINT",
        draw_class=2,
    )
    assert result["count"] == 1
    assert result["returned"] == 1


async def test_find_nearby_caps_results_and_reports_the_real_total(ctx, client):
    # A 500m radius around Circular Quay really does return ~618 locations.
    client.find_nearby.return_value = [make_location() for _ in range(618)]

    result = await server.find_nearby(latitude=-33.8613, longitude=151.2107, max_results=3, ctx=ctx)

    assert result["count"] == 618
    assert result["returned"] == 3
    assert len(result["locations"]) == 3


async def test_find_nearby_is_capped_by_default(ctx, client):
    client.find_nearby.return_value = [make_location() for _ in range(618)]

    result = await server.find_nearby(latitude=-33.8613, longitude=151.2107, ctx=ctx)

    assert result["returned"] < 618, "a nearby search must be capped by default"


async def test_get_vehicle_positions_caps_results_and_reports_the_real_total(ctx, client):
    client.vehicle_positions.return_value = list(range(250))

    result = await server.get_vehicle_positions(mode="buses", max_results=10, ctx=ctx)

    client.vehicle_positions.assert_called_once_with(mode="buses")
    # `count` must stay the true feed size so truncation is visible, not silent.
    assert result["count"] == 250
    assert result["returned"] == 10
    assert len(result["vehicles"]) == 10


async def test_get_vehicle_positions_returns_everything_when_under_the_cap(ctx, client):
    client.vehicle_positions.return_value = [1, 2, 3]

    result = await server.get_vehicle_positions(mode="metro", ctx=ctx)

    assert result == {"count": 3, "returned": 3, "vehicles": [1, 2, 3]}


async def test_get_vehicle_positions_rejects_an_unknown_feed(ctx, client):
    # An unknown mode otherwise reaches TfNSW and comes back as an opaque 404;
    # naming the valid feeds lets the model correct itself.
    with pytest.raises(ToolError) as excinfo:
        await server.get_vehicle_positions(mode="trains", ctx=ctx)

    assert "sydneytrains" in str(excinfo.value)
    client.vehicle_positions.assert_not_called()


@pytest.mark.parametrize("mode", server.VEHICLE_POSITION_MODES)
async def test_every_documented_feed_is_accepted(mode, ctx, client):
    client.vehicle_positions.return_value = []

    await server.get_vehicle_positions(mode=mode, ctx=ctx)

    client.vehicle_positions.assert_called_once_with(mode=mode)


def test_the_documented_feeds_match_the_validated_ones():
    # Keeps the docstring the model reads in step with the tuple that is
    # actually enforced, so neither can drift from the other unnoticed.
    doc = server.get_vehicle_positions.__doc__
    for mode in server.VEHICLE_POSITION_MODES:
        assert mode in doc, f"{mode} is accepted but not documented"


async def test_get_vehicle_positions_explains_the_missing_realtime_extra(ctx, client):
    client.vehicle_positions.side_effect = ImportError("needs gtfs-realtime-bindings")

    with pytest.raises(ToolError) as excinfo:
        await server.get_vehicle_positions(mode="buses", ctx=ctx)
    assert "gtfs-realtime-bindings" in str(excinfo.value)


# --------------------------------------------------------------------------
# Cross-cutting: auth and error mapping
# --------------------------------------------------------------------------


async def test_a_missing_api_key_becomes_an_actionable_tool_error(client, ctx_without_key):
    with pytest.raises(ToolError) as excinfo:
        await server.find_stop(query="Circular Quay", ctx=ctx_without_key)

    assert "X-API-Key" in str(excinfo.value)
    client.find_stop.assert_not_called()


async def test_api_errors_are_reported_with_their_status_code(ctx, client):
    client.find_stop.side_effect = APIError("API error 403: forbidden", status_code=403)

    with pytest.raises(ToolError) as excinfo:
        await server.find_stop(query="Circular Quay", ctx=ctx)

    assert "403" in str(excinfo.value)


async def test_network_errors_are_reported_as_tool_errors(ctx, client):
    client.get_departures.side_effect = NetworkError("Connection error: refused")

    with pytest.raises(ToolError) as excinfo:
        await server.get_departures(stop_id="200020", ctx=ctx)

    assert "Connection error" in str(excinfo.value)


async def test_the_api_key_never_appears_in_an_error_message(ctx, client):
    client.find_stop.side_effect = APIError("API error 401: bad key test-key", status_code=401)

    with pytest.raises(ToolError) as excinfo:
        await server.find_stop(query="Circular Quay", ctx=ctx)

    assert "test-key" not in str(excinfo.value)


async def test_every_tool_call_closes_its_client(ctx, client):
    client.find_stop.return_value = []

    await server.find_stop(query="Circular Quay", ctx=ctx)

    client.close.assert_called_once()
