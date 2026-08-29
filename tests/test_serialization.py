"""Tests for converting library dataclasses into JSON-safe structures."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import Enum

import pytest
from tfnsw_trip_planner.models import Coordinate, Location, StopEvent, VehiclePosition
from tfnsw_trip_planner.models.enums import CyclingProfile, LocationType, TransportMode
from tfnsw_trip_planner.models.stop_parent import StopParent

from tfnsw_trip_planner_mcp.serialization import to_jsonable


class Colour(Enum):
    RED = 1


class Flavour(str, Enum):
    SALTY = "salty"


@dataclass
class Inner:
    value: int


@dataclass
class Outer:
    name: str
    inner: Inner
    items: list[Inner]


def test_primitives_pass_through():
    assert to_jsonable("a") == "a"
    assert to_jsonable(1) == 1
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable(True) is True
    assert to_jsonable(None) is None


def test_int_enum_serializes_to_name():
    # "TRAIN" carries more meaning to a model than the raw product class 1.
    assert to_jsonable(TransportMode.TRAIN) == "TRAIN"
    assert to_jsonable(Colour.RED) == "RED"


def test_str_enum_serializes_to_value():
    assert to_jsonable(LocationType.STOP) == "stop"
    assert to_jsonable(CyclingProfile.MODERATE) == "MODERATE"
    assert to_jsonable(Flavour.SALTY) == "salty"


def test_datetimes_serialize_to_iso8601():
    aware = datetime(2026, 8, 30, 9, 15, tzinfo=UTC)
    assert to_jsonable(aware) == "2026-08-30T09:15:00+00:00"
    assert to_jsonable(datetime(2026, 8, 30, 9, 15)) == "2026-08-30T09:15:00"
    assert to_jsonable(date(2026, 8, 30)) == "2026-08-30"
    assert to_jsonable(time(9, 15)) == "09:15:00"


def test_nested_dataclasses_recurse():
    assert to_jsonable(Outer("x", Inner(1), [Inner(2), Inner(3)])) == {
        "name": "x",
        "inner": {"value": 1},
        "items": [{"value": 2}, {"value": 3}],
    }


def test_collections_become_lists():
    assert to_jsonable((Inner(1), Inner(2))) == [{"value": 1}, {"value": 2}]
    assert to_jsonable({"only"}) == ["only"]
    assert to_jsonable([]) == []


def test_dict_keys_stringified_and_values_recursed():
    assert to_jsonable({"a": Inner(1), 2: LocationType.POI}) == {"a": {"value": 1}, "2": "poi"}


def test_unknown_object_falls_back_to_str():
    class Opaque:
        def __repr__(self):
            return "<opaque>"

    assert to_jsonable(Opaque()) == "<opaque>"


def test_real_location_is_json_serializable():
    location = Location(
        id="10101331",
        name="Circular Quay",
        type=LocationType.STOP,
        coord=Coordinate(latitude=-33.8613, longitude=151.2107),
        modes=[1, 4, 5, 9],
        match_quality=1000,
        is_best=True,
        parent=StopParent(id="95301001", name="Sydney", type="locality"),
        building_number="",
        street_name="",
        properties={"STOP_GLOBAL_ID": "200020"},
        distance=None,
    )

    result = to_jsonable(location)

    assert result["type"] == "stop"
    assert result["coord"] == {"latitude": -33.8613, "longitude": 151.2107}
    assert result["parent"]["name"] == "Sydney"
    assert result["distance"] is None
    # The whole point: it must survive json.dumps without a custom encoder.
    json.dumps(result)


def test_real_stop_event_datetimes_are_iso():
    event = StopEvent.from_dict(
        {
            "location": {"id": "200020", "name": "Circular Quay"},
            "transportation": {"name": "T1", "product": {"class": 1}},
            "departureTimePlanned": "2026-08-30T09:15:00Z",
            "departureTimeEstimated": "2026-08-30T09:17:00Z",
        }
    )

    result = to_jsonable(event)

    # The library converts API timestamps to Australia/Sydney, so 09:15Z lands
    # at 19:15+10:00. Serialization must keep the offset rather than drop it.
    assert result["departure_planned"] == "2026-08-30T19:15:00+10:00"
    assert result["departure_estimated"] == "2026-08-30T19:17:00+10:00"
    json.dumps(result)


@pytest.mark.parametrize(
    "value",
    [
        VehiclePosition(
            vehicle_id="v1",
            trip_id="t1",
            route_id="r1",
            latitude=-33.8,
            longitude=151.2,
            bearing=90.0,
            speed=None,
            timestamp=datetime(2026, 8, 30, 9, 15, tzinfo=UTC),
        ),
        [Coordinate(1.0, 2.0)],
        {"nested": {"deeper": [LocationType.PLATFORM]}},
    ],
)
def test_results_always_json_dumpable(value):
    json.dumps(to_jsonable(value))
