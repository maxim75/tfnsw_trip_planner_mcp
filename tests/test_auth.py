"""Tests for extracting the caller's TfNSW API key from request headers."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest
from tfnsw_trip_planner import TripPlannerClient

from tfnsw_trip_planner_mcp import auth
from tfnsw_trip_planner_mcp.auth import (
    API_KEY_HEADER,
    MissingAPIKeyError,
    api_key_from_headers,
    client_for,
)


class FakeContext:
    """Stands in for mcp.server.mcpserver.Context, which exposes `.headers`."""

    def __init__(self, headers):
        self.headers = headers


def test_reads_the_documented_header():
    assert api_key_from_headers({"x-api-key": "abc123"}) == "abc123"


def test_header_lookup_is_case_insensitive():
    assert api_key_from_headers({"X-API-Key": "abc123"}) == "abc123"
    assert api_key_from_headers({"X-Api-Key": "abc123"}) == "abc123"
    assert api_key_from_headers({"X-API-KEY": "abc123"}) == "abc123"


def test_surrounding_whitespace_is_stripped():
    assert api_key_from_headers({"x-api-key": "  abc123 "}) == "abc123"


@pytest.mark.parametrize("prefix", ["apikey ", "apiKey ", "Bearer ", "bearer "])
def test_scheme_prefix_is_stripped(prefix):
    # TfNSW's own docs show `Authorization: apikey <key>`, so a pasted value may
    # arrive with the scheme still attached.
    assert api_key_from_headers({"x-api-key": f"{prefix}abc123"}) == "abc123"


def test_key_containing_the_word_apikey_is_not_mangled():
    assert api_key_from_headers({"x-api-key": "apikeyish-value"}) == "apikeyish-value"


@pytest.mark.parametrize("headers", [{}, {"x-api-key": ""}, {"x-api-key": "   "}, None])
def test_missing_or_blank_key_raises(headers):
    with pytest.raises(MissingAPIKeyError) as excinfo:
        api_key_from_headers(headers)
    # The message must tell the caller exactly what to send.
    assert API_KEY_HEADER in str(excinfo.value)


def test_bare_scheme_with_no_key_raises():
    with pytest.raises(MissingAPIKeyError):
        api_key_from_headers({"x-api-key": "apikey "})


def test_error_message_never_echoes_the_supplied_value():
    # A rejected value must not be reflected back into an error a client logs.
    with pytest.raises(MissingAPIKeyError) as excinfo:
        api_key_from_headers({"x-api-key": "bearer "})
    assert "bearer" not in str(excinfo.value).lower()


def test_client_for_builds_a_client_with_the_callers_key():
    with client_for(FakeContext({"x-api-key": "abc123"})) as client:
        assert client.api_key == "abc123"
        # The library forwards the key to TfNSW as an Authorization header.
        assert client._session.headers["Authorization"] == "apikey abc123"


def test_client_for_closes_the_client_on_the_way_out():
    with mock.patch.object(TripPlannerClient, "close") as close:
        with client_for(FakeContext({"x-api-key": "abc123"})):
            close.assert_not_called()
    close.assert_called_once()


def test_connections_are_pooled_across_calls():
    # Without a shared pool every tool call pays a fresh TCP+TLS handshake to
    # api.transport.nsw.gov.au. The pool is keyed by host, not by credential,
    # so sharing it costs nothing in isolation.
    with client_for(FakeContext({"x-api-key": "abc123"})) as first:
        first_pool = first._session.get_adapter("https://api.transport.nsw.gov.au")
    with client_for(FakeContext({"x-api-key": "abc123"})) as second:
        second_pool = second._session.get_adapter("https://api.transport.nsw.gov.au")

    assert first_pool is second_pool


def test_closing_a_client_does_not_tear_down_the_shared_pool():
    # Session.close() closes its adapters. If the shared adapter went with it,
    # the next call would rebuild the pool and the sharing would be pointless.
    with client_for(FakeContext({"x-api-key": "abc123"})) as client:
        adapter = client._session.get_adapter("https://api.transport.nsw.gov.au")
        pool_manager = adapter.poolmanager

    # Still usable after the context manager closed the session.
    assert adapter.poolmanager is pool_manager
    with client_for(FakeContext({"x-api-key": "abc123"})) as client:
        assert client._session.get_adapter("https://x").poolmanager is pool_manager


def test_each_caller_keeps_its_own_authorization_header():
    # The pool is shared but sessions are not: the library writes the key into
    # session headers, so a shared Session would let one caller's key overwrite
    # another's mid-flight. This is the property that must never regress.
    with client_for(FakeContext({"x-api-key": "key-one"})) as first:
        with client_for(FakeContext({"x-api-key": "key-two"})) as second:
            assert first._session is not second._session
            assert first._session.headers["Authorization"] == "apikey key-one"
            assert second._session.headers["Authorization"] == "apikey key-two"


def test_no_api_key_is_retained_on_the_shared_adapter():
    with client_for(FakeContext({"x-api-key": "secret-key"})) as client:
        adapter = client._session.get_adapter("https://api.transport.nsw.gov.au")

    assert "secret-key" not in repr(getattr(adapter, "__dict__", {}))


def test_the_shared_pool_opens_one_socket_for_many_sessions():
    """The point of the whole exercise: N calls, one TCP connection.

    Driven against a local HTTP server rather than TfNSW, so it proves socket
    reuse without a network dependency.
    """
    served = []
    source_ports = set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # required for keep-alive

        def do_GET(self):
            served.append(self.headers.get("Authorization"))
            # Every new TCP connection gets a distinct source port, so counting
            # them measures real socket reuse rather than a library's bookkeeping.
            source_ports.add(self.client_address[1])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    try:
        for index in range(4):
            session = auth._pooled_session()
            session.headers["Authorization"] = f"apikey key-{index}"
            session.get(url)
            # Mirrors client_for: the per-call session is closed each time.
            session.close()
    finally:
        server.shutdown()

    assert len(served) == 4
    assert len(source_ports) == 1, (
        f"expected all 4 requests on one socket, saw {len(source_ports)} connections"
    )
    # Each request still carried its own caller's key over that shared socket.
    assert served == ["apikey key-0", "apikey key-1", "apikey key-2", "apikey key-3"]


def test_client_for_closes_the_client_even_when_the_body_raises():
    with mock.patch.object(TripPlannerClient, "close") as close:
        with pytest.raises(RuntimeError):
            with client_for(FakeContext({"x-api-key": "abc123"})):
                raise RuntimeError("boom")
    close.assert_called_once()


def test_client_for_without_a_key_raises_before_building_a_client():
    with pytest.raises(MissingAPIKeyError):
        with client_for(FakeContext({})):
            pass


def test_client_for_handles_a_context_with_no_headers_at_all():
    # stdio transports leave `headers` as None.
    with pytest.raises(MissingAPIKeyError):
        with client_for(FakeContext(None)):
            pass
