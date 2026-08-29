"""Per-request authentication.

This server holds no TfNSW credentials of its own. Every caller supplies their
own key on every request via the ``X-API-Key`` header, and each tool call gets a
freshly built client that is closed again as soon as the call returns. Keeping
clients per-call (rather than caching one per key) means one caller's key can
never be reused to serve another's request.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from tfnsw_trip_planner import TripPlannerClient

__all__ = ["API_KEY_HEADER", "MissingAPIKeyError", "api_key_from_headers", "client_for"]

API_KEY_HEADER = "X-API-Key"

_SCHEMES = frozenset({"apikey", "bearer"})

_MISSING_KEY_MESSAGE = (
    f"No TfNSW API key supplied. Send your key in the {API_KEY_HEADER} HTTP header "
    "on every request to this server. Get a free key from "
    "https://opendata.transport.nsw.gov.au."
)


class MissingAPIKeyError(ValueError):
    """The request carried no usable TfNSW API key."""


def api_key_from_headers(headers: Mapping[str, str] | None) -> str:
    """Pull the caller's TfNSW API key out of *headers*.

    Raises:
        MissingAPIKeyError: if the header is absent, blank, or carries only a
            scheme prefix. The message names the expected header and never
            echoes the supplied value.
    """
    value = ""
    if headers:
        wanted = API_KEY_HEADER.lower()
        for name, candidate in headers.items():
            if name.lower() == wanted:
                value = (candidate or "").strip()
                break

    # TfNSW's own API takes `Authorization: apikey <key>`, so a copy-pasted
    # value may still carry the scheme. Split on the first run of whitespace so
    # a bare scheme with no key behind it falls through to the error below,
    # and a key that merely starts with those letters is left alone.
    parts = value.split(None, 1)
    if parts and parts[0].lower() in _SCHEMES:
        value = parts[1].strip() if len(parts) > 1 else ""

    if not value:
        raise MissingAPIKeyError(_MISSING_KEY_MESSAGE)
    return value


@contextmanager
def client_for(ctx) -> Iterator[TripPlannerClient]:
    """Yield a ``TripPlannerClient`` built from the current request's API key.

    ``ctx`` is the MCP ``Context`` injected into a tool. Its ``headers`` are
    populated by the HTTP transports and are ``None`` under stdio.
    """
    api_key = api_key_from_headers(getattr(ctx, "headers", None))
    client = TripPlannerClient(api_key=api_key)
    try:
        yield client
    finally:
        client.close()
