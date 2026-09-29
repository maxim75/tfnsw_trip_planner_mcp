"""Tests for the 10 MCP tools: argument mapping, output shape, error mapping."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from tfnsw_trip_planner import APIError, NetworkError
from tfnsw_trip_planner.models import Coordinate, Journey, Leg, Location
from tfnsw_trip_planner.models.enums import CyclingProfile, LocationType, TransportMode
from tfnsw_trip_planner.models.service_alert import ServiceAlert
from tfnsw_trip_planner.models.stop import Stop
from tfnsw_trip_planner.models.stop_event import StopEvent
from tfnsw_trip_planner.models.transport import Transport

from tfnsw_trip_planner_mcp import server
from tfnsw_trip_planner_mcp.serialization import to_jsonable
from tfnsw_trip_planner_mcp.server import _capped, parse_when


def make_location(name="Circular Quay", loc_id="10101331", match_quality=987):
    return Location(
        id=loc_id,
        name=name,
        type=LocationType.STOP,
        coord=Coordinate(latitude=-33.8613, longitude=151.2107),
        modes=[1, 9],
        match_quality=match_quality,
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

    # Journey tools additionally report the detail level they applied, and
    # get_departures whether it trimmed each departure.
    assert {"count", "returned", LIST_TOOL_KEYS[tool_name]} <= set(result)
    assert set(result) - {"detail", "concise"} == {"count", "returned", LIST_TOOL_KEYS[tool_name]}
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
        origin_type="any",
        destination_type="any",
        realtime=True,
        wheelchair=False,
    )


async def test_plan_trip_defaults_to_any_so_address_ids_resolve(ctx, client):
    # Regression, verified against the live API: find_stop resolves an address to
    # a `streetID:...` ID of type "singlehouse". Planning with type_origin="stop"
    # — the old default — returns ZERO journeys for it, while "any" returns four.
    # The old default therefore silently failed the commonest question there is
    # ("how do I get from my place to theirs") and cost a wasted retry.
    client.plan_trip.return_value = []

    await server.plan_trip(
        origin_id="streetID:1500000010:100:95301023:-1:Harris St:Pyrmont",
        ctx=ctx,
        destination_id="streetID:1500000096:32:95357012:-1:Geelong Rd:Engadine",
    )

    kwargs = client.plan_trip.call_args.kwargs
    assert kwargs["origin_type"] == "any"
    assert kwargs["destination_type"] == "any"


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

    assert result["count"] == 3
    assert result["returned"] == 3
    assert result["journeys"] == ["j1", "j2", "j3"]


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
        "concise": True,
        "departures": ["d1", "d2"],
    }


def make_stop_event(dep=None, dep_est=None, number="T4", name="Sydney Trains Network"):
    transport = make_transport(number=number)
    transport.name = name
    return StopEvent(
        location=make_stop(),
        transportation=transport,
        departure_planned=dep,
        departure_estimated=dep_est,
        onwards_locations=[{"properties": dict(REAL_STOP_JUNK)}],
    )


async def test_get_departures_is_concise_by_default(ctx, client):
    client.get_departures.return_value = [
        make_stop_event(dep=at(18, 36), dep_est=at(18, 41)),
        make_stop_event(dep=at(18, 50), number="T8"),
    ]

    result = await server.get_departures(stop_id="200060", ctx=ctx)

    assert result["concise"] is True
    assert result["departures"] == [
        {
            "departure_planned": "2026-09-08T18:36:00+10:00",
            "departure_estimated": "2026-09-08T18:41:00+10:00",
            "transportation": {"name": "Sydney Trains Network", "number": "T4"},
        },
        {
            # A null estimate is kept: it says "no live data", which is itself
            # an answer to "is it running late".
            "departure_planned": "2026-09-08T18:50:00+10:00",
            "departure_estimated": None,
            "transportation": {"name": "Sydney Trains Network", "number": "T8"},
        },
    ]


async def test_get_departures_concise_false_returns_every_field(ctx, client):
    event = make_stop_event(dep=at(18, 36), dep_est=at(18, 41))
    client.get_departures.return_value = [event]

    result = await server.get_departures(stop_id="200060", concise=False, ctx=ctx)

    assert result["concise"] is False
    assert result["departures"] == [to_jsonable(event)]
    assert result["departures"][0]["location"]["name"]
    assert result["departures"][0]["onwards_locations"]


async def test_get_departures_concise_is_far_smaller(ctx, client):
    client.get_departures.return_value = [
        make_stop_event(dep=at(18, 36), dep_est=at(18, 41)) for _ in range(40)
    ]

    concise = await server.get_departures(stop_id="200060", ctx=ctx)
    full = await server.get_departures(stop_id="200060", concise=False, ctx=ctx)

    assert len(json.dumps(concise)) < len(json.dumps(full)) / 4


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


@pytest.mark.parametrize("bad_mode", ["trains", "ferries", "lightrail", "regionbuses"])
async def test_get_vehicle_positions_rejects_an_unknown_feed(bad_mode, ctx, client):
    # An unknown mode otherwise reaches TfNSW and comes back as an opaque 404;
    # naming the valid feeds lets the model correct itself. These are the
    # plausible-but-wrong guesses: bare "ferries" and "lightrail" need a
    # sub-feed, and "trains" is neither sydneytrains nor nswtrains.
    with pytest.raises(ToolError) as excinfo:
        await server.get_vehicle_positions(mode=bad_mode, ctx=ctx)

    assert "buses" in str(excinfo.value), "the error should list the valid feeds"
    client.vehicle_positions.assert_not_called()


async def test_sydneytrains_is_accepted(ctx, client):
    # Regression: this was advertised, then wrongly removed as non-existent.
    # It is real, on the v2 endpoint that tfnsw-trip-planner 1.4.0 routes to.
    client.vehicle_positions.return_value = []

    await server.get_vehicle_positions(mode="sydneytrains", ctx=ctx)

    client.vehicle_positions.assert_called_once_with(mode="sydneytrains")


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


# --------------------------------------------------------------------------
# Journey shape: the allowlist, the totals, and the detail levels
# --------------------------------------------------------------------------

SYD = timezone(timedelta(hours=10))


def at(hour, minute):
    return datetime(2026, 9, 8, hour, minute, tzinfo=SYD)


# Real property keys from a live Pyrmont -> Engadine response. `accessArray`
# carries lift equipment heights and weight limits; the rest are internal GIS
# bookkeeping. None of it is readable by a model, and it was 28% of the payload.
REAL_STOP_JUNK = {
    "accessArray": [{"stoppointAccess": {"height": 780, "equipment": 0, "maxWeight": 0}}],
    "WheelchairAccess": "true",
    "AREA_NIVEAU_DIVA": "0",
    "areaGid": "G200932",
    "area": "0",
    "pbyb": "1",
}

# The three keys the platform is repeated under, verbatim from the same response.
PLATFORM_KEYS = ("stoppingPointPlanned", "plannedPlatformName", "platformName")


def make_stop(
    name="Sydney, Central Station, Platform 25",
    disassembled="Central Station, Platform 25",
    stop_id="200060",
    dep=None,
    dep_est=None,
    arr=None,
    arr_est=None,
    platform="Platform 25",
):
    # Mirrors the real nesting, which measurement confirmed on all 44 stops of a
    # live response: `name` strictly contains `disassembled_name`, which in turn
    # already contains the platform. Hence three widths of the same string.
    properties = dict(REAL_STOP_JUNK)
    if platform:
        properties.update(dict.fromkeys(PLATFORM_KEYS, platform))
    return Stop(
        id=stop_id,
        name=name,
        disassembled_name=disassembled,
        coord=Coordinate(-33.8832, 151.2069),
        departure_planned=dep,
        departure_estimated=dep_est,
        arrival_planned=arr,
        arrival_estimated=arr_est,
        wheelchair_access=True,
        properties=properties,
    )


def make_transport(mode=TransportMode.TRAIN, number="South Coast Line", towards="Waterfall"):
    empty = mode in (TransportMode.WALK, TransportMode.WALK_ALT)
    return Transport(
        id="" if empty else "T1",
        name="" if empty else "Sydney Trains Network",
        disassembled_name="",
        number="" if empty else number,
        icon_id=-1 if empty else 1,
        description="",
        product=None,
        destination_name="" if empty else towards,
        mode=mode,
    )


def make_leg(
    mode=TransportMode.TRAIN,
    duration=2370,
    origin=None,
    destination=None,
    coord_points=250,
    stops=30,
    infos=None,
):
    return Leg(
        duration=duration,
        origin=origin if origin is not None else make_stop(dep=at(18, 36), dep_est=at(18, 36)),
        destination=destination
        if destination is not None
        else make_stop(
            name="Engadine, Engadine Station, Platform 2",
            disassembled="Engadine Station, Platform 2",
            stop_id="221420",
            arr=at(19, 15),
            arr_est=at(19, 15),
            platform="Platform 2",
        ),
        transportation=make_transport(mode),
        stop_sequence=[
            make_stop(name=f"Suburb, Stop {i}", disassembled=f"S{i}", platform=None)
            for i in range(stops)
        ],
        coords=[Coordinate(-33.7 + i / 1000, 150.3 + i / 1000) for i in range(coord_points)],
        infos=infos or [],
        hints=[],
        properties={},
        is_realtime=True,
    )


def make_journey(legs=2):
    return Journey(legs=[make_leg() for _ in range(legs)])


def realistic_journey():
    """Walk -> Bus -> Train -> Walk: one interchange, two walking legs."""
    return Journey(
        legs=[
            make_leg(
                mode=TransportMode.WALK_ALT,
                duration=180,
                origin=make_stop(
                    "Pyrmont, 100 Harris St",
                    "100 Harris St",
                    "addr1",
                    dep=at(18, 8),
                    platform=None,
                ),
                destination=make_stop(
                    "Pyrmont, Miller St", "Miller St", "2009110", arr=at(18, 11), platform=None
                ),
            ),
            make_leg(
                mode=TransportMode.BUS,
                duration=390,
                origin=make_stop(
                    "Pyrmont, Miller St", "Miller St", "2009110", dep=at(18, 11), platform=None
                ),
                destination=make_stop(
                    "Ultimo, Broadway", "Broadway", "2007123", arr=at(18, 17), platform=None
                ),
            ),
            make_leg(
                mode=TransportMode.TRAIN,
                duration=2370,
                origin=make_stop(dep=at(18, 36), dep_est=at(18, 36)),
                destination=make_stop(
                    "Engadine, Engadine Station, Platform 2",
                    "Engadine Station, Platform 2",
                    "221420",
                    arr=at(19, 15),
                    platform="Platform 2",
                ),
            ),
            make_leg(
                mode=TransportMode.WALK_ALT,
                duration=60,
                origin=make_stop(
                    "Engadine, Geelong Rd", "Geelong Rd", "2233110", dep=at(19, 30), platform=None
                ),
                destination=make_stop(
                    "Engadine, 32 Geelong Rd",
                    "32 Geelong Rd",
                    "addr2",
                    arr=at(19, 31),
                    platform=None,
                ),
            ),
        ]
    )


JOURNEY_TOOLS = {
    "plan_trip": {"origin_id": "A", "destination_id": "B"},
    "plan_trip_from_coordinate": {"latitude": 1.0, "longitude": 2.0, "destination_id": "B"},
    "plan_cycling_trip": {"origin_id": "A", "destination_id": "B"},
}


# ---- journey-level totals ------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(JOURNEY_TOOLS))
async def test_journey_carries_the_answer_without_reading_any_leg(tool_name, ctx, client):
    # Journey.departure_time/.arrival_time/.total_duration are @property, and
    # to_jsonable only walks dataclasses.fields(), so they never reached the
    # model — it had to rebuild the answer by reading all 22 leg objects.
    getattr(client, tool_name).return_value = [realistic_journey()]

    result = await getattr(server, tool_name)(**JOURNEY_TOOLS[tool_name], ctx=ctx)

    journey = result["journeys"][0]
    assert journey["departure"] == "2026-09-08T18:08:00+10:00"
    assert journey["arrival"] == "2026-09-08T19:31:00+10:00"
    assert journey["duration_min"] == 50
    assert journey["via"] == "Walk Alt → Bus → Train → Walk Alt"


async def test_changes_counts_interchanges_not_walking_legs(ctx, client):
    # Walk -> Bus -> Train -> Walk is ONE change, not three.
    client.plan_trip.return_value = [realistic_journey()]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert result["journeys"][0]["changes"] == 1


async def test_a_walk_only_journey_reports_zero_changes(ctx, client):
    client.plan_trip.return_value = [Journey(legs=[make_leg(mode=TransportMode.WALK)])]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert result["journeys"][0]["changes"] == 0


# ---- detail levels -------------------------------------------------------


async def test_answer_detail_omits_legs_entirely(ctx, client):
    client.plan_trip.return_value = [realistic_journey()]

    result = await server.plan_trip(origin_id="A", destination_id="B", detail="answer", ctx=ctx)

    journey = result["journeys"][0]
    assert "legs" not in journey
    assert journey["arrival"] == "2026-09-08T19:31:00+10:00"
    assert result["detail"] == "answer"


async def test_summary_detail_keeps_legs_but_not_geometry_or_stops(ctx, client):
    client.plan_trip.return_value = [realistic_journey()]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    leg = result["journeys"][0]["legs"][2]
    assert leg["mode"] == "TRAIN"
    assert leg["route"] == "South Coast Line"
    assert leg["duration_min"] == 40
    assert "coords" not in leg
    assert "stops" not in leg


async def test_stops_detail_lists_intermediate_stops_by_name_only(ctx, client):
    # The old "stops" level embedded 30 full Stop objects per leg (163 KB on a
    # real trip). A model reading a stop list wants the names, not each stop's
    # coordinates, wheelchair flags and property bag.
    client.plan_trip.return_value = [make_journey(legs=1)]

    result = await server.plan_trip(origin_id="A", destination_id="B", detail="stops", ctx=ctx)

    stops = result["journeys"][0]["legs"][0]["stops"]
    assert len(stops) == 30
    assert stops[0] == "S0", "an intermediate stop should serialize to its name"


async def test_full_detail_adds_geometry_as_compact_pairs(ctx, client):
    client.plan_trip.return_value = [make_journey(legs=1)]

    result = await server.plan_trip(origin_id="A", destination_id="B", detail="full", ctx=ctx)

    coords = result["journeys"][0]["legs"][0]["coords"]
    assert len(coords) == 250
    # [lat, lon] pairs, not {"latitude": .., "longitude": ..} objects.
    assert coords[0] == [-33.7, 150.3]


async def test_the_result_says_which_detail_level_it_used(ctx, client):
    client.plan_trip.return_value = [realistic_journey()]

    assert (await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx))["detail"] == (
        "summary"
    )
    assert (await server.plan_trip(origin_id="A", destination_id="B", detail="full", ctx=ctx))[
        "detail"
    ] == "full"


# ---- the allowlist -------------------------------------------------------


@pytest.mark.parametrize("junk", ["accessArray", "AREA_NIVEAU_DIVA", "areaGid", "pbyb", "area"])
async def test_raw_upstream_property_bags_never_reach_the_model(junk, ctx, client):
    # These were passed straight through because trimming was a blocklist.
    client.plan_trip.return_value = [realistic_journey()]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert junk not in json.dumps(result)


async def test_the_platform_survives_the_property_purge(ctx, client):
    # Dropping the property bag must not lose the one field inside it a
    # passenger actually needs.
    client.plan_trip.return_value = [make_journey(legs=1)]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert "Platform 25" in result["journeys"][0]["legs"][0]["from"]["name"]


async def test_the_platform_is_not_repeated_three_times(ctx, client):
    # It arrives under three keys and inside two of the three name widths.
    # Once is enough.
    client.plan_trip.return_value = [make_journey(legs=1)]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert json.dumps(result).count("Platform 25") == 1


async def test_a_platform_the_name_does_not_mention_is_still_surfaced(ctx, client):
    # The flip side: dropping the bag whenever the name looks sufficient would
    # lose the platform for stops whose name stops at the station.
    client.plan_trip.return_value = [
        Journey(
            legs=[
                make_leg(
                    origin=make_stop(
                        "Sydney, Central Station",
                        "Central Station",
                        dep=at(18, 36),
                        platform="Platform 25",
                    )
                )
            ]
        )
    ]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert result["journeys"][0]["legs"][0]["from"]["platform"] == "Platform 25"


async def test_a_realtime_estimate_equal_to_the_schedule_is_not_duplicated(ctx, client):
    # 20 of 22 timed stops on a real trip had estimated == planned exactly.
    client.plan_trip.return_value = [
        Journey(legs=[make_leg(origin=make_stop(dep=at(18, 36), dep_est=at(18, 36)))])
    ]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    origin = result["journeys"][0]["legs"][0]["from"]
    assert origin["departure"] == "2026-09-08T18:36:00+10:00"
    assert "scheduled_departure" not in origin


async def test_a_real_delay_is_reported_against_the_schedule(ctx, client):
    client.plan_trip.return_value = [
        Journey(legs=[make_leg(origin=make_stop(dep=at(18, 36), dep_est=at(18, 41)))])
    ]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    origin = result["journeys"][0]["legs"][0]["from"]
    assert origin["departure"] == "2026-09-08T18:41:00+10:00", "the estimate is what you catch"
    assert origin["scheduled_departure"] == "2026-09-08T18:36:00+10:00"


async def test_null_times_are_omitted_rather_than_serialized(ctx, client):
    # Half of all stop time fields on a real trip were null.
    client.plan_trip.return_value = [
        Journey(legs=[make_leg(origin=make_stop(dep=at(18, 36), dep_est=at(18, 36)))])
    ]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert "null" not in json.dumps(result)


async def test_walking_legs_omit_the_empty_transport_fields(ctx, client):
    # A walking leg's Transport block is 209 bytes of empty strings and -1s.
    client.plan_trip.return_value = [Journey(legs=[make_leg(mode=TransportMode.WALK_ALT)])]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    leg = result["journeys"][0]["legs"][0]
    assert leg["mode"] == "WALK_ALT"
    assert "route" not in leg
    assert "towards" not in leg


async def test_alerts_are_reduced_to_their_subtitles(ctx, client):
    alert = ServiceAlert(
        subtitle="Bus stop closure, Ultimo",
        url="https://transportnsw.info/alerts/details#/ems-59309",
        last_modification=None,
        affected_stops=[{"id": "x"}] * 40,
        affected_lines=[{"id": "y"}] * 40,
    )
    client.plan_trip.return_value = [Journey(legs=[make_leg(infos=[alert])])]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    leg = result["journeys"][0]["legs"][0]
    assert leg["alerts"] == ["Bus stop closure, Ultimo"]
    assert "affected_stops" not in json.dumps(result)


async def test_the_new_summary_is_far_smaller_than_the_old_passthrough(ctx, client):
    client.plan_trip.return_value = [realistic_journey() for _ in range(4)]

    summary = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)
    full = await server.plan_trip(origin_id="A", destination_id="B", detail="full", ctx=ctx)
    answer = await server.plan_trip(origin_id="A", destination_id="B", detail="answer", ctx=ctx)

    assert len(json.dumps(answer)) < len(json.dumps(summary)) / 4
    assert len(json.dumps(summary)) < len(json.dumps(full)) / 10


async def test_journeys_are_capped(ctx, client):
    client.plan_trip.return_value = [make_journey() for _ in range(9)]

    result = await server.plan_trip(origin_id="A", destination_id="B", max_results=2, ctx=ctx)

    assert result["count"] == 9
    assert result["returned"] == 2


async def test_every_leg_is_trimmed_not_just_the_first(ctx, client):
    client.plan_trip.return_value = [make_journey(legs=3)]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    legs = result["journeys"][0]["legs"]
    assert len(legs) == 3
    assert all("coords" not in leg for leg in legs)


# --------------------------------------------------------------------------
# Resolving place names inside plan_trip (three tool calls -> one)
# --------------------------------------------------------------------------


async def test_plan_trip_accepts_place_names_and_resolves_them(ctx, client):
    # The whole point: "from 100 Harris Street to 32 Geelong Rd" used to cost
    # two best_stop round trips through the model before planning could start.
    client.best_stop.side_effect = [
        make_location("100 Harris St, Pyrmont", "streetID:100"),
        make_location("32 Geelong Rd, Engadine", "streetID:32"),
    ]
    client.plan_trip.return_value = []

    await server.plan_trip(origin="100 Harris Street Pyrmont", destination="32 Geelong Rd", ctx=ctx)

    assert [c.kwargs["query"] for c in client.best_stop.call_args_list] == [
        "100 Harris Street Pyrmont",
        "32 Geelong Rd",
    ]
    kwargs = client.plan_trip.call_args.kwargs
    assert kwargs["origin_id"] == "streetID:100"
    assert kwargs["destination_id"] == "streetID:32"


async def test_resolution_reuses_one_client_for_all_three_upstream_calls(ctx, client):
    # Resolving inside the tool is only a win if it does not pay the client
    # setup cost three times over.
    client.best_stop.side_effect = [make_location(), make_location()]
    client.plan_trip.return_value = []

    await server.plan_trip(origin="Circular Quay", destination="Bondi Junction", ctx=ctx)

    client.close.assert_called_once()


async def test_resolved_names_report_what_they_matched(ctx, client):
    # A model must be able to see that "Harris St" became "100 Harris St,
    # Pyrmont" — silently planning from the wrong place is the failure mode.
    client.best_stop.side_effect = [
        make_location("100 Harris St, Pyrmont", "streetID:100"),
        make_location("32 Geelong Rd, Engadine", "streetID:32"),
    ]
    client.plan_trip.return_value = []

    result = await server.plan_trip(
        origin="100 Harris Street", destination="32 Geelong Rd", ctx=ctx
    )

    assert result["origin"]["name"] == "100 Harris St, Pyrmont"
    assert result["destination"]["name"] == "32 Geelong Rd, Engadine"


async def test_an_unresolvable_name_is_an_actionable_error(ctx, client):
    client.best_stop.side_effect = [None]

    with pytest.raises(ToolError) as excinfo:
        await server.plan_trip(origin="Nowhere At All", destination="Bondi Junction", ctx=ctx)

    assert "Nowhere At All" in str(excinfo.value)
    client.plan_trip.assert_not_called()


async def test_plan_trip_requires_an_origin_of_some_kind(ctx, client):
    with pytest.raises(ToolError) as excinfo:
        await server.plan_trip(destination_id="B", ctx=ctx)

    assert "origin" in str(excinfo.value).lower()
    client.plan_trip.assert_not_called()


async def test_plan_trip_rejects_a_name_and_an_id_for_the_same_end(ctx, client):
    # Ambiguous: silently preferring one would plan a trip the caller did not ask
    # for, which is the exact class of error that is hardest to notice.
    with pytest.raises(ToolError):
        await server.plan_trip(
            origin="Circular Quay", origin_id="200020", destination_id="B", ctx=ctx
        )

    client.plan_trip.assert_not_called()


async def test_an_id_still_works_without_any_resolution(ctx, client):
    client.plan_trip.return_value = []

    await server.plan_trip(origin_id="200020", destination_id="200070", ctx=ctx)

    client.best_stop.assert_not_called()


async def test_opaque_composite_ids_are_not_echoed_into_legs(ctx, client):
    # An address resolves to a 129-byte `streetID:...` blob. It is kept in the
    # top-level origin/destination echo (where it identifies what was matched),
    # but inside a leg it is pure weight: get_departures and get_alerts take
    # numeric stop IDs, so a composite ID cannot be chained anywhere.
    address = "streetID:1500000010:100:95301023:-1:Harris St:Pyrmont:Harris St"
    client.plan_trip.return_value = [
        Journey(
            legs=[
                make_leg(
                    origin=make_stop(
                        "Pyrmont, 100 Harris St",
                        "100 Harris St",
                        address,
                        dep=at(18, 8),
                        platform=None,
                    )
                )
            ]
        )
    ]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    origin = result["journeys"][0]["legs"][0]["from"]
    assert origin["name"] == "100 Harris St", "the name still identifies the place"
    assert "id" not in origin


async def test_numeric_stop_ids_are_still_echoed_for_chaining(ctx, client):
    client.plan_trip.return_value = [make_journey(legs=1)]

    result = await server.plan_trip(origin_id="A", destination_id="B", ctx=ctx)

    assert result["journeys"][0]["legs"][0]["from"]["id"] == "200060"


async def test_a_poor_fuzzy_match_is_refused_and_names_what_it_found(ctx, client):
    # TfNSW's stop finder almost never returns nothing — it returns its best
    # guess with a score. Measured against the live API: real places score 250
    # (addresses) to 996 (stations), while nonsense scores 47-154.
    # "Zzzqqxnowhere Placeton" really does resolve to "Iceton Pl, Yass" at 108.
    # Planning that trip silently is the worst possible outcome, so refuse it and
    # name the candidate, which lets a model confirm or correct in one step.
    client.best_stop.side_effect = [make_location("Iceton Pl, Yass", "s1", match_quality=108)]

    with pytest.raises(ToolError) as excinfo:
        await server.plan_trip(origin="Zzzqqxnowhere Placeton", destination_id="B", ctx=ctx)

    message = str(excinfo.value)
    assert "Zzzqqxnowhere Placeton" in message, "the error must quote what was asked for"
    assert "Iceton Pl, Yass" in message, "and name the weak candidate it declined to use"
    client.plan_trip.assert_not_called()


async def test_an_address_grade_match_is_good_enough_to_plan(ctx, client):
    # Addresses score exactly 250 — far below a station's ~987, but a real match.
    # A threshold that rejected these would break the commonest question there is.
    client.best_stop.side_effect = [make_location("100 Harris St, Pyrmont", "s1", 250)]
    client.plan_trip.return_value = []

    result = await server.plan_trip(origin="100 Harris Street Pyrmont", destination_id="B", ctx=ctx)

    assert result["origin"]["name"] == "100 Harris St, Pyrmont"
