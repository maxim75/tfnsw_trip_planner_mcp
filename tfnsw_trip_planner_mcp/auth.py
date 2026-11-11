"""Per-request authentication.

This server holds no TfNSW credentials of its own. Every caller supplies their
own key on every request via the ``X-API-Key`` header, and each tool call gets a
freshly built client that is closed again as soon as the call returns.

The one exception is stdio. A stdio server is spawned by exactly one local
client and has no HTTP headers, so that client passes its key through the
``TFNSW_API_KEY`` environment variable instead. The entry point registers it
with :func:`set_stdio_api_key` only when it starts in stdio mode; an HTTP
deployment never reads that variable, so it can never serve a key of its own. Keeping
clients per-call (rather than caching one per key) means one caller's key can
never be reused to serve another's request.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

import requests
from requests.adapters import HTTPAdapter
from tfnsw_trip_planner import TripPlannerClient

__all__ = [
    "API_KEY_ENV",
    "API_KEY_HEADER",
    "MissingAPIKeyError",
    "api_key_from_headers",
    "client_for",
    "set_stdio_api_key",
]

API_KEY_HEADER = "X-API-Key"
API_KEY_ENV = "TFNSW_API_KEY"

_SCHEMES = frozenset({"apikey", "bearer"})

_MISSING_KEY_MESSAGE = (
    f"No TfNSW API key supplied. Send your key in the {API_KEY_HEADER} HTTP header "
    "on every request to this server, or, when running it over stdio, set the "
    f"{API_KEY_ENV} environment variable for the server process. Get a free key "
    "from https://opendata.transport.nsw.gov.au."
)

# Set only by the stdio entry point. None means "headers only", which is what
# every HTTP deployment gets.
_stdio_api_key: str | None = None


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
                value = candidate or ""
                break
    return _normalise(value)


def _normalise(value: str) -> str:
    """Strip whitespace and any scheme prefix; raise if no key is left."""
    value = value.strip()
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


def set_stdio_api_key(value: str | None) -> bool:
    """Register the key a stdio server uses when a request carries no headers.

    Called by the entry point only when serving over stdio. Accepts the same
    forms as the header (surrounding whitespace, an ``apikey``/``Bearer``
    prefix). A missing or blank value clears it, so tool calls fail with the
    usual message while the handshake still works — which is what directory
    scanners that spawn the server without a key need.

    Returns:
        True if a usable key was registered.
    """
    global _stdio_api_key
    try:
        _stdio_api_key = _normalise(value or "")
    except MissingAPIKeyError:
        _stdio_api_key = None
    return _stdio_api_key is not None


class _SharedPoolAdapter(HTTPAdapter):
    """An adapter whose connection pool outlives the sessions that mount it.

    ``Session.close()`` closes every adapter it holds. Each tool call gets its
    own short-lived session, so the default behaviour would discard the
    connection pool with it and every call would pay a fresh TCP and TLS
    handshake. Closing is therefore a no-op here: the pool is process-wide and
    is torn down only when the process exits.
    """

    def close(self) -> None:  # pragma: no cover - trivial override
        pass


# Shared across every caller. This is safe precisely because a connection pool
# is keyed by host, not by credential: it holds sockets to
# api.transport.nsw.gov.au and nothing about who is calling. urllib3's
# PoolManager is thread-safe, which matters because tool calls run on anyio
# worker threads. `pool_maxsize` is raised well above the default 10 so
# concurrent calls are not serialised on a starved pool.
_SHARED_POOL = _SharedPoolAdapter(pool_connections=4, pool_maxsize=32)


def _pooled_session() -> requests.Session:
    """Return a fresh session that borrows the process-wide connection pool."""
    session = requests.Session()
    session.mount("https://", _SHARED_POOL)
    session.mount("http://", _SHARED_POOL)
    return session


@contextmanager
def client_for(ctx) -> Iterator[TripPlannerClient]:
    """Yield a ``TripPlannerClient`` built from the current request's API key.

    ``ctx`` is the MCP ``Context`` injected into a tool. Its ``headers`` are
    populated by the HTTP transports and are ``None`` under stdio, where the key
    registered by :func:`set_stdio_api_key` is used instead.

    The client and its session are per-call, but the underlying connections are
    pooled process-wide. The split matters: ``TripPlannerClient`` writes the
    API key into ``session.headers``, so sharing one session between callers
    would let a second caller's key overwrite a first caller's mid-flight.
    Sharing only the pool keeps credentials strictly per-call while still
    reusing sockets.
    """
    headers = getattr(ctx, "headers", None)
    if headers is None and _stdio_api_key is not None:
        api_key = _stdio_api_key
    else:
        api_key = api_key_from_headers(headers)
    client = TripPlannerClient(api_key=api_key, session=_pooled_session())
    try:
        yield client
    finally:
        client.close()
