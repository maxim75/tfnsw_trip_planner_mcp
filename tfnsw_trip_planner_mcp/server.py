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
from tfnsw_trip_planner import APIError, NetworkError, TripPlannerClient
from tfnsw_trip_planner.models.enums import CyclingProfile

from .auth import API_KEY_HEADER, MissingAPIKeyError, client_for
from .serialization import to_jsonable

__all__ = ["mcp", "parse_when"]

INSTRUCTIONS = f"""\
Live public transport data for Sydney and New South Wales, Australia, from the
Transport for NSW Open Data APIs.

Every request must carry a TfNSW API key in the {API_KEY_HEADER} HTTP header.

To plan a journey, call plan_trip with plain place names — origin="100 Harris
Street Pyrmont", destination="Bondi Junction". It resolves them itself, so do
NOT call find_stop or best_stop first; that costs two extra round trips and
answers nothing plan_trip cannot.

Use find_stop or best_stop only when the caller wants to see the candidate
matches for an ambiguous name, or when you need a stop ID for get_departures,
which does still take an ID.

plan_trip defaults to detail="summary". For "when do I arrive" or "how long does
it take", pass detail="answer" instead — it returns the times, duration and
number of changes without any of the per-leg data.

All times are Australia/Sydney local time.
"""

mcp = MCPServer(
    "tfnsw-trip-planner",
    title="Transport for NSW Trip Planner",
    instructions=INSTRUCTIONS,
    website_url="https://opendata.transport.nsw.gov.au",
)

CyclingProfileName = Literal["EASIER", "MODERATE", "MORE_DIRECT"]

JourneyDetail = Literal["answer", "summary", "stops", "full"]

# Modes that are not a service you board, so they never count as an interchange.
_NON_TRANSIT_MODES = frozenset({"WALK", "WALK_ALT", "CYCLE"})

# The upstream property bag repeats the platform under three keys. Read the
# first one that is present and drop the rest.
_PLATFORM_KEYS = ("platformName", "plannedPlatformName", "stoppingPointPlanned")

# Below this, a resolved place name is a guess rather than a match, and planning
# from it would answer a question nobody asked. TfNSW's stop finder practically
# never returns nothing — it returns its nearest guess with a score — so without
# a floor here, resolving names server-side would silently plan the wrong trip.
# Measured against the live API: real places score 250 (street addresses, which
# are uniformly 250) up to 996 (stations), while nonsense queries score 47-154
# ("Zzzqqxnowhere Placeton" resolves to "Iceton Pl, Yass" at 108). 200 sits in
# the empty band between the two, so it rejects guesses without rejecting the
# address lookups that matter most.
_MIN_MATCH_QUALITY = 200

# Rejects a nonsensical limit at schema validation, before it can reach a slice.
# A model passing -1 to mean "unlimited" is the case that matters.
PositiveInt = Annotated[int, Field(ge=1)]

# Taken from the library rather than restated here, so the two cannot drift.
# That drift is exactly what broke this before: a hand-written list included
# "sydneytrains", which 404s on the v1 endpoint the library used to hardcode,
# so a feed that does exist was first advertised-but-broken and then removed
# outright. tfnsw-trip-planner 1.4.0 routes the superseded feeds (Sydney Trains
# and Inner West Light Rail) to the v2 endpoint that actually serves them.
VEHICLE_POSITION_MODES = TripPlannerClient.VEHICLE_POSITION_MODES


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


async def _run(ctx: Context, work: Any) -> Any:
    """Run ``work(client)`` for the caller, off the event loop.

    Takes a callable rather than a method name so one tool can make several
    library calls against a *single* client — `plan_trip` resolving two place
    names before planning is the case that matters. Doing that here rather than
    making the model call `best_stop` twice turns three round trips into one,
    and pays the client-construction cost once instead of three times.
    """
    try:
        with client_for(ctx) as client:
            # The library is synchronous `requests`; a slow TfNSW response must
            # not block the event loop and stall every other in-flight call.
            return await anyio.to_thread.run_sync(functools.partial(work, client))
    except MissingAPIKeyError as exc:
        raise ToolError(str(exc)) from None
    except APIError as exc:
        raise ToolError(f"TfNSW API request failed: {_redact(ctx, str(exc))}") from None
    except NetworkError as exc:
        raise ToolError(f"Could not reach the TfNSW API: {_redact(ctx, str(exc))}") from None
    except ImportError as exc:
        raise ToolError(str(exc)) from None


async def _call(ctx: Context, method: str, **kwargs: Any) -> Any:
    """Run ``client.<method>(**kwargs)`` for the caller, off the event loop."""
    return await _run(ctx, lambda client: getattr(client, method)(**kwargs))


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


def _stop_name(stop: Any) -> str:
    """The most useful name for a stop.

    `disassembled_name` is the middle of three widths the API sends: on every one
    of 44 stops in a real response it was a strict substring of `name` (which
    prefixes the suburb) and a superset of the platform. Emitting one of the
    three loses nothing a passenger reads.
    """
    return getattr(stop, "disassembled_name", "") or getattr(stop, "name", "") or ""


def _add_time(payload: dict[str, Any], label: str, planned: Any, estimated: Any) -> None:
    """Record one time as the value that will actually happen, plus any delay.

    The API sends planned and estimated separately for both arrival and
    departure — four fields, of which half were null and 20 of the 22 non-null
    ones held byte-identical values. Emit the estimate (what you catch) and keep
    the schedule only when the two genuinely differ, i.e. when there is a delay
    worth reporting.
    """
    actual = estimated or planned
    if actual is None:
        return
    payload[label] = actual.isoformat()
    if estimated and planned and estimated != planned:
        payload[f"scheduled_{label}"] = planned.isoformat()


def _stop_payload(stop: Any) -> dict[str, Any] | None:
    """An allowlisted stop: what it is called, where, and when."""
    if stop is None:
        return None
    name = _stop_name(stop)
    payload: dict[str, Any] = {"name": name}

    stop_id = getattr(stop, "id", "")
    # Kept so a model can chain straight into get_departures or get_alerts
    # without spending another find_stop call to re-resolve this stop — but only
    # when it is a real stop ID. An address resolves to a 129-byte composite
    # `streetID:...` blob that those tools do not accept, so echoing it once per
    # leg buys nothing. Composite IDs are the ones with colons in them.
    if stop_id and ":" not in stop_id:
        payload["id"] = stop_id

    properties = getattr(stop, "properties", None) or {}
    for key in _PLATFORM_KEYS:
        platform = properties.get(key)
        # Only when the name does not already say it, which it usually does.
        if platform and platform not in name:
            payload["platform"] = platform
            break

    _add_time(
        payload,
        "departure",
        getattr(stop, "departure_planned", None),
        getattr(stop, "departure_estimated", None),
    )
    _add_time(
        payload,
        "arrival",
        getattr(stop, "arrival_planned", None),
        getattr(stop, "arrival_estimated", None),
    )
    return payload


def _mode_name(leg: Any) -> str:
    mode = getattr(getattr(leg, "transportation", None), "mode", None)
    return getattr(mode, "name", None) or str(mode)


def _leg_payload(leg: Any, detail: JourneyDetail) -> dict[str, Any]:
    """An allowlisted leg.

    Deliberately an allowlist. The old trimming was a blocklist — it dropped
    `coords` and `stop_sequence` and let everything else through, including the
    raw upstream `properties` bag (lift equipment heights, `AREA_NIVEAU_DIVA`,
    `areaGid`, `pbyb` and three copies of the platform name). On a real trip that
    passthrough was 28% of the payload and none of it is readable by a model.
    An allowlist cannot regress that way when TfNSW adds a field.
    """
    transport = getattr(leg, "transportation", None)
    payload: dict[str, Any] = {
        "mode": _mode_name(leg),
        "duration_min": round(getattr(leg, "duration", 0) / 60),
        "from": _stop_payload(getattr(leg, "origin", None)),
        "to": _stop_payload(getattr(leg, "destination", None)),
    }

    # Walking legs carry a Transport block of empty strings and -1 icon ids —
    # 209 bytes each of nothing. Omit a field rather than serialize its absence.
    number = getattr(transport, "number", "") or getattr(transport, "name", "")
    if number:
        payload["route"] = number
    towards = getattr(transport, "destination_name", "")
    if towards:
        payload["towards"] = towards
    if getattr(leg, "is_realtime", False):
        payload["realtime"] = True

    alerts = [
        subtitle
        for info in getattr(leg, "infos", None) or ()
        if (subtitle := getattr(info, "subtitle", ""))
    ]
    if alerts:
        # The subtitle is the whole alert as far as a model is concerned; the
        # affected_stops/affected_lines arrays behind it can run to hundreds of
        # ids that only a map would use.
        payload["alerts"] = alerts

    if detail in ("stops", "full"):
        # Names only. Embedding whole Stop objects here was 24% of a real
        # response and repeated the property bag once per intermediate stop.
        payload["stops"] = [_stop_name(stop) for stop in getattr(leg, "stop_sequence", None) or ()]
    if detail == "full":
        # [lat, lon] pairs rather than {"latitude": .., "longitude": ..}, which
        # is the same information for a third of the bytes.
        payload["coords"] = [
            [coord.latitude, coord.longitude] for coord in getattr(leg, "coords", None) or ()
        ]
    return payload


def _journey_payload(journey: Any, detail: JourneyDetail) -> Any:
    """A journey led by the answer, not by the raw data.

    `Journey` already computes departure, arrival, duration and a mode summary
    as properties, but `to_jsonable` walks `dataclasses.fields()` and so dropped
    every one of them. A model asking "when do I arrive" therefore had to
    reconstruct the answer by reading all of the legs. Now it is stated up front,
    and `detail="answer"` can drop the legs entirely.
    """
    legs = getattr(journey, "legs", None)
    if legs is None:
        # Not a Journey. Serializing is a presentation concern and must never be
        # the thing that fails a call, so anything unexpected passes through.
        return to_jsonable(journey)

    payload: dict[str, Any] = {}
    _add_time(payload, "departure", None, getattr(journey, "departure_time", None))
    _add_time(payload, "arrival", None, getattr(journey, "arrival_time", None))
    payload["duration_min"] = round(getattr(journey, "total_duration", 0) / 60)
    payload["changes"] = max(
        0, sum(1 for leg in legs if _mode_name(leg) not in _NON_TRANSIT_MODES) - 1
    )
    payload["via"] = getattr(journey, "summary", "")
    if detail != "answer":
        payload["legs"] = [_leg_payload(leg, detail) for leg in legs]
    return payload


def _journeys_result(
    journeys: list[Any], max_results: int | None, detail: JourneyDetail
) -> dict[str, Any]:
    """Wrap journeys, rebuilt at the requested level of detail.

    Carries `detail` back to the caller so a model that wants the geometry can
    see the result was trimmed and ask again, rather than concluding the data
    does not exist.
    """
    capped = journeys if max_results is None else journeys[: max(1, max_results)]
    return {
        "count": len(journeys),
        "returned": len(capped),
        "detail": detail,
        "journeys": [_journey_payload(journey, detail) for journey in capped],
    }


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


def _check_endpoint(name: str | None, ident: str | None, label: str) -> None:
    """Reject an under- or over-specified end of a trip."""
    if name and ident:
        raise ToolError(
            f"Pass either {label} (a place name) or {label}_id (a stop ID), not both. "
            f"Got {label}={name!r} and {label}_id={ident!r}."
        )
    if not name and not ident:
        raise ToolError(
            f"A trip needs {'an' if label == 'origin' else 'a'} {label}: pass "
            f'{label}="<place name>" or {label}_id="<stop ID>".'
        )


@mcp.tool()
async def plan_trip(
    ctx: Context,
    origin: str | None = None,
    destination: str | None = None,
    origin_id: str | None = None,
    destination_id: str | None = None,
    when: str | None = None,
    arrive_by: bool = False,
    origin_type: str = "any",
    destination_type: str = "any",
    realtime: bool = True,
    wheelchair: bool = False,
    detail: JourneyDetail = "summary",
    max_results: PositiveInt = 5,
) -> dict[str, Any]:
    """Plan a public transport journey between two places.

    Prefer passing plain place names as `origin` and `destination` — addresses,
    stations, suburbs and landmarks all work, and the server resolves them
    itself. You do NOT need to call find_stop or best_stop first; doing so costs
    two extra round trips for no benefit. Use `origin_id`/`destination_id` only
    when you already hold a stop ID from an earlier call.

    Args:
        origin: Place to depart from, e.g. "100 Harris Street Pyrmont" or
            "Circular Quay". Resolved server-side.
        destination: Place to arrive at, e.g. "32 Geelong Rd Engadine".
        origin_id: Stop ID to depart from. Alternative to `origin`, not both.
        destination_id: Stop ID to arrive at. Alternative to `destination`.
        when: Optional ISO 8601 date/time, e.g. "2026-08-30T09:15". Without an
            offset this is Sydney local time. Defaults to now.
        arrive_by: Treat `when` as the desired arrival time instead of departure.
        origin_type: Kind of the origin ID. Leave as "any", which resolves stops,
            addresses and POIs alike; "stop" rejects address IDs and returns
            nothing for them.
        destination_type: Kind of the destination ID. Leave as "any".
        realtime: Include live delay information.
        wheelchair: Return only wheelchair-accessible journeys.
        detail: How much to return per journey. "answer" gives only departure,
            arrival, duration, changes and the mode summary — use it for "when
            do I get there" and "how long does it take", which is most questions.
            "summary" (default) adds the legs, each with its times, route,
            platform and any alerts. "stops" adds intermediate stop names.
            "full" adds the map polyline and is very large — pair it with
            max_results=1 or 2 or the call may exceed the client's size limit.
        max_results: Maximum journeys to return.
    """
    _check_endpoint(origin, origin_id, "origin")
    _check_endpoint(destination, destination_id, "destination")
    parsed_when = parse_when(when)

    def work(client: Any) -> tuple[Any, dict[str, Any]]:
        resolved: dict[str, Any] = {}
        ends = {"origin": origin_id, "destination": destination_id}
        for label, query in (("origin", origin), ("destination", destination)):
            if not query:
                continue
            location = client.best_stop(query=query)
            if location is None:
                raise ToolError(
                    f"Could not find anywhere matching {query!r}. Try a fuller name, "
                    "e.g. include the suburb, or search with find_stop."
                )
            if getattr(location, "match_quality", 0) < _MIN_MATCH_QUALITY:
                raise ToolError(
                    f"No confident match for {query!r} — the closest was "
                    f"{location.name!r}, which looks wrong. Check the spelling, add "
                    "the suburb, or call find_stop to see the candidates."
                )
            ends[label] = location.id
            # Report what the name became: silently planning from the wrong
            # place is the failure a caller is least likely to notice.
            resolved[label] = {"id": location.id, "name": location.name}

        journeys = client.plan_trip(
            origin_id=ends["origin"],
            destination_id=ends["destination"],
            when=parsed_when,
            arrive_by=arrive_by,
            origin_type=origin_type,
            destination_type=destination_type,
            realtime=realtime,
            wheelchair=wheelchair,
        )
        return journeys, resolved

    journeys, resolved = await _run(ctx, work)
    return {**_journeys_result(journeys, max_results, detail), **resolved}


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
        detail: How much to return per journey. "answer" gives only departure,
            arrival, duration, changes and the mode summary — use it for "when
            do I get there" and "how long does it take", which is most questions.
            "summary" (default) adds the legs, each with its times, route,
            platform and any alerts. "stops" adds intermediate stop names.
            "full" adds the map polyline and is very large — pair it with
            max_results=1 or 2 or the call may exceed the client's size limit.
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
        detail: How much to return per journey. "answer" gives only departure,
            arrival, duration, changes and the mode summary — use it for "when
            do I get there" and "how long does it take", which is most questions.
            "summary" (default) adds the legs, each with its times, route,
            platform and any alerts. "stops" adds intermediate stop names.
            "full" adds the map polyline and is very large — pair it with
            max_results=1 or 2 or the call may exceed the client's size limit.
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
        mode: Which feed to read. One of "buses", "sydneytrains", "metro",
            "nswtrains", "ferries/sydneyferries", "lightrail/cbdandsoutheast",
            "lightrail/innerwest", "lightrail/newcastle",
            "lightrail/parramatta".
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
