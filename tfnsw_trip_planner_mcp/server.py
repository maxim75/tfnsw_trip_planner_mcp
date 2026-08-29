"""The MCP server: one tool per public method of ``TripPlannerClient``.

Every tool follows the same shape — resolve the caller's API key into a
short-lived client, run the (synchronous) library call on a worker thread, and
return JSON-safe data wrapped in an object.
"""

from __future__ import annotations

import functools
from datetime import datetime
from typing import Any, Literal

import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
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

VEHICLE_POSITION_MODES = (
    "buses",
    "ferries/sydneyferries",
    "lightrail/cbdandsoutheast",
    "lightrail/newcastle",
    "lightrail/parramatta",
    "metro",
    "nswtrains",
    "sydneytrains",
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
    max_results: int = 10,
) -> dict[str, Any]:
    """Search for stops, stations, wharves, points of interest and addresses by name.

    Use this to turn a place name into the stop ID that the trip planning and
    departure tools require.

    Args:
        query: What to search for, e.g. "Circular Quay" or "Town Hall Station".
        location_type: Restrict results — "any", "stop", "platform", "poi",
            "address", "street" or "locality".
        max_results: Maximum number of matches to return.
    """
    locations = await _call(
        ctx, "find_stop", query=query, location_type=location_type, max_results=max_results
    )
    return {"count": len(locations), "locations": to_jsonable(locations)}


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
    return {"count": len(journeys), "journeys": to_jsonable(journeys)}


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
    return {"count": len(journeys), "journeys": to_jsonable(journeys)}


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
    return {"count": len(journeys), "journeys": to_jsonable(journeys)}


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
    return {"count": len(departures), "departures": to_jsonable(departures)}


# --------------------------------------------------------------------------
# Service Alert API
# --------------------------------------------------------------------------


@mcp.tool()
async def get_alerts(
    ctx: Context,
    when: str | None = None,
    stop_id: str | None = None,
    current_only: bool = True,
) -> dict[str, Any]:
    """Retrieve service alerts: disruptions, trackwork and planned changes.

    Args:
        when: Optional ISO 8601 date/time to check alerts for; Sydney local time
            if no offset given. Defaults to now.
        stop_id: Restrict to alerts affecting one stop. Omit for network-wide.
        current_only: Only alerts in effect now. Set false to include future ones.
    """
    alerts = await _call(
        ctx, "get_alerts", when=parse_when(when), stop_id=stop_id, current_only=current_only
    )
    return {"count": len(alerts), "alerts": to_jsonable(alerts)}


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
) -> dict[str, Any]:
    """Find stops and points of interest near a GPS coordinate.

    Each result carries its distance in metres from the coordinate.

    Args:
        latitude: Latitude in decimal degrees, e.g. -33.8613.
        longitude: Longitude in decimal degrees, e.g. 151.2107.
        radius_m: Search radius in metres.
        type_1: TfNSW result category. "GIS_POINT" covers stops and POIs.
        draw_class: Optional TfNSW sub-category filter.
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
    return {"count": len(locations), "locations": to_jsonable(locations)}


# --------------------------------------------------------------------------
# GTFS-Realtime Vehicle Positions API
# --------------------------------------------------------------------------


@mcp.tool()
async def get_vehicle_positions(
    mode: str,
    ctx: Context,
    max_results: int = 100,
) -> dict[str, Any]:
    """Fetch live GPS positions of vehicles currently running on a network.

    Unlike the other tools, which return timing estimates, this returns where
    each vehicle physically is. Note this feed is a separate product on the
    TfNSW Open Data portal — your API key must be subscribed to it as well.

    Feeds can carry thousands of vehicles, so results are capped: `count` is the
    true feed size and `returned` is how many are included.

    Args:
        mode: Which feed to read. Known values: "buses", "metro", "sydneytrains",
            "nswtrains", "ferries/sydneyferries", "lightrail/cbdandsoutheast",
            "lightrail/newcastle", "lightrail/parramatta".
        max_results: Maximum vehicles to return.
    """
    vehicles = await _call(ctx, "vehicle_positions", mode=mode)
    capped = vehicles[:max_results]
    return {
        "count": len(vehicles),
        "returned": len(capped),
        "vehicles": to_jsonable(capped),
    }
