"""The MCP server: one tool per public method of ``TripPlannerClient``.

Every tool follows the same shape — resolve the caller's API key into a
short-lived client, run the (synchronous) library call on a worker thread, and
return JSON-safe data wrapped in an object.
"""

from __future__ import annotations

import functools
from datetime import datetime
from typing import Annotated, Any, Literal

import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from tfnsw_trip_planner import APIError, NetworkError
from tfnsw_trip_planner.models.enums import CyclingProfile

from .auth import API_KEY_HEADER, MissingAPIKeyError, client_for
from .serialization import to_jsonable

__all__ = ["mcp", "parse_when"]

INSTRUCTIONS = f"""\
Live public transport data for Sydney and New South Wales, Australia, from the
Transport for NSW Open Data APIs.

Every request must carry a TfNSW API key in the {API_KEY_HEADER} HTTP header.

Stops are addressed by numeric ID, not by name. To answer a question about a
named place, call find_stop or best_stop first to resolve the name to an ID,
then pass that ID to plan_trip or get_departures.

All times are Australia/Sydney local time.
"""

mcp = MCPServer(
    "tfnsw-trip-planner",
    title="Transport for NSW Trip Planner",
    instructions=INSTRUCTIONS,
    website_url="https://opendata.transport.nsw.gov.au",
)

CyclingProfileName = Literal["EASIER", "MODERATE", "MORE_DIRECT"]

JourneyDetail = Literal["summary", "stops", "full"]

# Which leg fields each detail level drops. `coords` is the route polyline the
# API returns for drawing a map: on a real Sydney-to-Katoomba plan_trip it was
# 72% of a 1.06MB response, which exceeded the client's 1MB tool-result limit
# outright. `stop_sequence` (every intermediate stop) was another 24%. Together
# they are 96% of the payload and neither is needed to answer "how do I get
# there", so the default drops both and a caller opts back in.
_LEG_FIELDS_DROPPED: dict[str, tuple[str, ...]] = {
    "summary": ("coords", "stop_sequence"),
    "stops": ("coords",),
    "full": (),
}

# Rejects a nonsensical limit at schema validation, before it can reach a slice.
# A model passing -1 to mean "unlimited" is the case that matters.
PositiveInt = Annotated[int, Field(ge=1)]

# Verified against the live API: every entry returns 200 with vehicle data.
# "sydneytrains" is deliberately absent - TfNSW publishes no vehicle position
# feed for it under any path, and listing it made this server accept a mode that
# could only ever 404. Bare "lightrail" is absent for a similar reason: it
# responds 200 but with an empty feed, so it can only mislead.
VEHICLE_POSITION_MODES = (
    "buses",
    "ferries/sydneyferries",
    "lightrail/cbdandsoutheast",
    "lightrail/newcastle",
    "lightrail/parramatta",
    "metro",
    "nswtrains",
)


def parse_when(value: str | None) -> datetime | None:
    """Parse an optional ISO 8601 ``when`` argument.

    A value without an offset is left naive, which the library reads as
    Australia/Sydney local time. Anything unparseable raises rather than being
    dropped — silently planning a trip for the wrong time is worse than an error.
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(
            f"Could not read {value!r} as a date/time. Use ISO 8601, for example "
            "'2026-08-30T09:15' (Sydney local time) or '2026-08-30T09:15+10:00'."
        ) from None


async def _call(ctx: Context, method: str, **kwargs: Any) -> Any:
    """Run ``client.<method>(**kwargs)`` for the caller, off the event loop."""
    try:
        with client_for(ctx) as client:
            bound = functools.partial(getattr(client, method), **kwargs)
            # The library is synchronous `requests`; a slow TfNSW response must
            # not block the event loop and stall every other in-flight call.
            return await anyio.to_thread.run_sync(bound)
    except MissingAPIKeyError as exc:
        raise ToolError(str(exc)) from None
    except APIError as exc:
        raise ToolError(f"TfNSW API request failed: {_redact(ctx, str(exc))}") from None
    except NetworkError as exc:
        raise ToolError(f"Could not reach the TfNSW API: {_redact(ctx, str(exc))}") from None
    except ImportError as exc:
        raise ToolError(str(exc)) from None


def _capped(items: list[Any], max_results: int | None, key: str) -> dict[str, Any]:
    """Wrap a list result, truncating it to *max_results*.

    Every list-returning tool goes through here so they all answer with the same
    shape — `count` (the true total), `returned` (how many are included), and the
    items. Pass ``max_results=None`` for endpoints the upstream API already
    bounds; `returned` then equals `count`.

    Some endpoints answer with far more than a caller can use: an unfiltered
    alert fetch returns every alert in NSW (~283, 1.3MB of JSON) and a 500m
    nearby search can return 600+ locations. Beyond flooding the model's
    context, an oversized reply breaks the transport outright — MCP sends the
    payload twice (as text content and as structured content) and clients cap a
    single SSE event at 1MiB, so a large reply is dropped mid-stream and the
    call fails with "SSE stream ended without a response".
    """
    if max_results is None:
        capped = items
    else:
        # Guard the slice rather than trusting the caller: items[:-1] would
        # return all-but-one, silently undoing the cap and re-breaking the
        # transport. The schema also enforces a minimum of 1, so this is the
        # second line of defence, not the only one.
        capped = items[: max(1, max_results)]
    return {"count": len(items), "returned": len(capped), key: to_jsonable(capped)}


def _journeys_result(
    journeys: list[Any], max_results: int | None, detail: JourneyDetail
) -> dict[str, Any]:
    """Wrap journeys, trimming each leg to the requested level of detail.

    Carries `detail` back to the caller so a model that wants the geometry can
    see the result was trimmed and ask again, rather than concluding the data
    does not exist.
    """
    result = _capped(journeys, max_results, "journeys")
    dropped = _LEG_FIELDS_DROPPED[detail]
    if dropped:
        for journey in result["journeys"]:
            # Serialized journeys are dicts of dicts, but trimming is a
            # presentation concern and must never be the thing that fails a
            # call, so anything unexpected is left untouched rather than raising.
            if not isinstance(journey, dict):
                continue
            for leg in journey.get("legs") or ():
                if not isinstance(leg, dict):
                    continue
                for field in dropped:
                    leg.pop(field, None)
    result["detail"] = detail
    return result


def _redact(ctx: Context, message: str) -> str:
    """Strip the caller's API key out of *message*, defensively.

    Upstream is not expected to echo the key back, but an error string reaches
    the model and the client's logs, so it is not the place to find out.
    """
    headers = getattr(ctx, "headers", None) or {}
    for name, value in headers.items():
        if name.lower() == API_KEY_HEADER.lower() and value:
            message = message.replace(value.strip(), "[redacted]")
    return message


# --------------------------------------------------------------------------
# Stop Finder API
# --------------------------------------------------------------------------


@mcp.tool()
async def find_stop(
    query: str,
    ctx: Context,
    location_type: str = "any",
    limit: PositiveInt = 10,
) -> dict[str, Any]:
    """Search for stops, stations, wharves, points of interest and addresses by name.

    Use this to turn a place name into the stop ID that the trip planning and
    departure tools require.

    Args:
        query: What to search for, e.g. "Circular Quay" or "Town Hall Station".
        location_type: Restrict results — "any", "stop", "platform", "poi",
            "address", "street" or "locality".
        limit: Ask the TfNSW API to return at most this many matches. Unlike
            max_results on the capped tools, this bounds the upstream query
            rather than truncating a fetched list, so `count` is exact.
    """
    locations = await _call(
        ctx, "find_stop", query=query, location_type=location_type, max_results=limit
    )
    return _capped(locations, None, "locations")


@mcp.tool()
async def find_stop_by_id(stop_id: str, ctx: Context) -> dict[str, Any]:
    """Look up a single stop by its numeric TfNSW stop ID.

    Returns `{"location": null}` if no stop carries that ID.

    Args:
        stop_id: The stop ID, e.g. "10101331".
    """
    location = await _call(ctx, "find_stop_by_id", stop_id=stop_id)
    return {"location": to_jsonable(location)}


@mcp.tool()
async def best_stop(query: str, ctx: Context) -> dict[str, Any]:
    """Return only the single best-matching location for a name.

    A shortcut for find_stop when you just need one stop ID and do not want to
    weigh alternatives. Returns `{"location": null}` if nothing matches.

    Args:
        query: The place name to resolve, e.g. "Bondi Junction".
    """
    location = await _call(ctx, "best_stop", query=query)
    return {"location": to_jsonable(location)}


# --------------------------------------------------------------------------
# Trip Planner API
# --------------------------------------------------------------------------


@mcp.tool()
async def plan_trip(
    origin_id: str,
    destination_id: str,
    ctx: Context,
    when: str | None = None,
    arrive_by: bool = False,
    origin_type: str = "stop",
    destination_type: str = "stop",
    realtime: bool = True,
    wheelchair: bool = False,
    detail: JourneyDetail = "summary",
    max_results: PositiveInt = 5,
) -> dict[str, Any]:
    """Plan a public transport journey between two stops.

    Both IDs must be TfNSW stop IDs — resolve names with find_stop or best_stop
    first. Each journey comes back as a list of legs with times, modes and
    interchanges.

    Args:
        origin_id: Stop ID to depart from.
        destination_id: Stop ID to arrive at.
        when: Optional ISO 8601 date/time, e.g. "2026-08-30T09:15". Without an
            offset this is Sydney local time. Defaults to now.
        arrive_by: Treat `when` as the desired arrival time instead of departure.
        origin_type: Kind of the origin ID — "stop", "poi" or "coord".
        destination_type: Kind of the destination ID.
        realtime: Include live delay information.
        wheelchair: Return only wheelchair-accessible journeys.
        detail: How much per-leg data to return. "summary" (default) omits the
            route polyline and the intermediate stop list, keeping times, modes,
            interchanges and durations — enough to answer almost any trip
            question at a fraction of the size. "stops" adds the intermediate
            stops. "full" adds the map polyline too and is very large — pair it
            with max_results=1 or 2, or a long journey will exceed the client's
            tool-result limit and the call will fail.
        max_results: Maximum journeys to return.
    """
    journeys = await _call(
        ctx,
        "plan_trip",
        origin_id=origin_id,
        destination_id=destination_id,
        when=parse_when(when),
        arrive_by=arrive_by,
        origin_type=origin_type,
        destination_type=destination_type,
        realtime=realtime,
        wheelchair=wheelchair,
    )
    return _journeys_result(journeys, max_results, detail)


@mcp.tool()
async def plan_trip_from_coordinate(
    latitude: float,
    longitude: float,
    destination_id: str,
    ctx: Context,
    when: str | None = None,
    arrive_by: bool = False,
    realtime: bool = True,
    wheelchair: bool = False,
    detail: JourneyDetail = "summary",
    max_results: PositiveInt = 5,
) -> dict[str, Any]:
    """Plan a journey starting from a GPS coordinate rather than a stop.

    Use this when the starting point is a user's current location or an
    arbitrary address, and only the destination is a known stop.

    Args:
        latitude: Starting latitude in decimal degrees, e.g. -33.8613.
        longitude: Starting longitude in decimal degrees, e.g. 151.2107.
        destination_id: Stop ID to arrive at.
        when: Optional ISO 8601 date/time; Sydney local time if no offset given.
        arrive_by: Treat `when` as the desired arrival time.
        realtime: Include live delay information.
        wheelchair: Return only wheelchair-accessible journeys.
        detail: How much per-leg data to return. "summary" (default) omits the
            route polyline and the intermediate stop list, keeping times, modes,
            interchanges and durations — enough to answer almost any trip
            question at a fraction of the size. "stops" adds the intermediate
            stops. "full" adds the map polyline too and is very large — pair it
            with max_results=1 or 2, or a long journey will exceed the client's
            tool-result limit and the call will fail.
        max_results: Maximum journeys to return.
    """
    journeys = await _call(
        ctx,
        "plan_trip_from_coordinate",
        latitude=latitude,
        longitude=longitude,
        destination_id=destination_id,
        when=parse_when(when),
        arrive_by=arrive_by,
        realtime=realtime,
        wheelchair=wheelchair,
    )
    return _journeys_result(journeys, max_results, detail)


@mcp.tool()
async def plan_cycling_trip(
    origin_id: str,
    destination_id: str,
    ctx: Context,
    profile: CyclingProfileName = "MODERATE",
    when: str | None = None,
    bike_only: bool = True,
    max_time_minutes: int = 240,
    cycle_speed: int = 16,
    detail: JourneyDetail = "summary",
    max_results: PositiveInt = 5,
) -> dict[str, Any]:
    """Plan a cycling route, optionally combined with public transport.

    Args:
        origin_id: Stop ID to start from.
        destination_id: Stop ID to finish at.
        profile: Route preference — "EASIER" (gentler gradients and quieter
            roads), "MODERATE", or "MORE_DIRECT" (fastest, busier roads).
        when: Optional ISO 8601 date/time; Sydney local time if no offset given.
        bike_only: Cycle the whole way. Set false to allow mixed bike + transit.
        max_time_minutes: Reject routes longer than this.
        cycle_speed: Assumed cycling speed in km/h.
        detail: How much per-leg data to return. "summary" (default) omits the
            route polyline and the intermediate stop list, keeping times, modes,
            interchanges and durations — enough to answer almost any trip
            question at a fraction of the size. "stops" adds the intermediate
            stops. "full" adds the map polyline too and is very large — pair it
            with max_results=1 or 2, or a long journey will exceed the client's
            tool-result limit and the call will fail.
        max_results: Maximum journeys to return.
    """
    journeys = await _call(
        ctx,
        "plan_cycling_trip",
        origin_id=origin_id,
        destination_id=destination_id,
        profile=CyclingProfile(profile),
        when=parse_when(when),
        bike_only=bike_only,
        max_time_minutes=max_time_minutes,
        cycle_speed=cycle_speed,
    )
    return _journeys_result(journeys, max_results, detail)


# --------------------------------------------------------------------------
# Departure API
# --------------------------------------------------------------------------


@mcp.tool()
async def get_departures(
    stop_id: str,
    ctx: Context,
    when: str | None = None,
    platform_id: str | None = None,
    realtime: bool = True,
) -> dict[str, Any]:
    """List upcoming departures from a stop — the live departure board.

    Args:
        stop_id: Stop ID to read departures for. Resolve names with find_stop.
        when: Optional ISO 8601 date/time to board from; Sydney local time if no
            offset given. Defaults to now.
        platform_id: Restrict to a single platform or stand.
        realtime: Include live delay information alongside scheduled times.
    """
    departures = await _call(
        ctx,
        "get_departures",
        stop_id=stop_id,
        when=parse_when(when),
        platform_id=platform_id,
        realtime=realtime,
    )
    return _capped(departures, None, "departures")


# --------------------------------------------------------------------------
# Service Alert API
# --------------------------------------------------------------------------


@mcp.tool()
async def get_alerts(
    ctx: Context,
    when: str | None = None,
    stop_id: str | None = None,
    current_only: bool = True,
    max_results: PositiveInt = 20,
) -> dict[str, Any]:
    """Retrieve service alerts: disruptions, trackwork and planned changes.

    Pass a stop_id whenever you can. A network-wide fetch returns every alert in
    NSW — hundreds of them — so results are capped: `count` is the true total
    and `returned` is how many are included.

    Args:
        when: Optional ISO 8601 date/time to check alerts for; Sydney local time
            if no offset given. Defaults to now.
        stop_id: Restrict to alerts affecting one stop. Omit for network-wide.
        current_only: Only alerts in effect now. Set false to include future ones.
        max_results: Maximum alerts to return.
    """
    alerts = await _call(
        ctx, "get_alerts", when=parse_when(when), stop_id=stop_id, current_only=current_only
    )
    return _capped(alerts, max_results, "alerts")


# --------------------------------------------------------------------------
# Coordinate Request API
# --------------------------------------------------------------------------


@mcp.tool()
async def find_nearby(
    latitude: float,
    longitude: float,
    ctx: Context,
    radius_m: int = 500,
    type_1: str = "GIS_POINT",
    draw_class: int | None = None,
    max_results: PositiveInt = 50,
) -> dict[str, Any]:
    """Find stops and points of interest near a GPS coordinate.

    Each result carries its distance in metres from the coordinate. A dense area
    can return hundreds of locations within 500m, so results are capped:
    `count` is the true total and `returned` is how many are included. Narrow
    `radius_m` rather than raising `max_results` to get more relevant results.

    Args:
        latitude: Latitude in decimal degrees, e.g. -33.8613.
        longitude: Longitude in decimal degrees, e.g. 151.2107.
        radius_m: Search radius in metres.
        type_1: TfNSW result category. "GIS_POINT" covers stops and POIs.
        draw_class: Optional TfNSW sub-category filter.
        max_results: Maximum locations to return.
    """
    locations = await _call(
        ctx,
        "find_nearby",
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        type_1=type_1,
        draw_class=draw_class,
    )
    return _capped(locations, max_results, "locations")


# --------------------------------------------------------------------------
# GTFS-Realtime Vehicle Positions API
# --------------------------------------------------------------------------


@mcp.tool()
async def get_vehicle_positions(
    mode: str,
    ctx: Context,
    max_results: PositiveInt = 100,
) -> dict[str, Any]:
    """Fetch live GPS positions of vehicles currently running on a network.

    Unlike the other tools, which return timing estimates, this returns where
    each vehicle physically is. Note this feed is a separate product on the
    TfNSW Open Data portal — your API key must be subscribed to it as well.

    Feeds can carry thousands of vehicles, so results are capped: `count` is the
    true feed size and `returned` is how many are included.

    Args:
        mode: Which feed to read. One of "buses", "metro", "nswtrains",
            "ferries/sydneyferries", "lightrail/cbdandsoutheast",
            "lightrail/newcastle", "lightrail/parramatta". Note there is no
            Sydney Trains vehicle position feed; use get_departures for
            suburban train times.
        max_results: Maximum vehicles to return.
    """
    if mode not in VEHICLE_POSITION_MODES:
        # An unknown feed otherwise reaches TfNSW and returns an opaque 404.
        # Naming the valid feeds lets the model correct itself in one step.
        raise ToolError(
            f"Unknown vehicle position feed {mode!r}. Valid feeds are: "
            + ", ".join(VEHICLE_POSITION_MODES)
        )
    vehicles = await _call(ctx, "vehicle_positions", mode=mode)
    return _capped(vehicles, max_results, "vehicles")
