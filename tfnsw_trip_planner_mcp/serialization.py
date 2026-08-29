"""Convert the library's dataclasses into JSON-safe structures.

The ``tfnsw_trip_planner`` models are plain dataclasses holding enums, datetimes
and nested models. ``dataclasses.asdict`` recurses but leaves enums and
datetimes untouched, so MCP clients would choke on the result. ``to_jsonable``
does the same recursion and normalises the leaves.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime, time
from enum import Enum
from typing import Any

__all__ = ["to_jsonable"]


def to_jsonable(obj: Any) -> Any:
    """Return *obj* rebuilt out of types that ``json.dumps`` accepts."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj

    if isinstance(obj, Enum):
        # LocationType/CyclingProfile are str enums whose values are the useful
        # form ("stop"). TransportMode is int-valued, where the name ("TRAIN")
        # is far more legible to a model than the product class (1).
        return obj.value if isinstance(obj, str) else obj.name

    if isinstance(obj, str):
        return obj

    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}

    if isinstance(obj, Mapping):
        return {str(key): to_jsonable(value) for key, value in obj.items()}

    if isinstance(obj, (Sequence, AbstractSet)):
        return [to_jsonable(item) for item in obj]

    return str(obj)
