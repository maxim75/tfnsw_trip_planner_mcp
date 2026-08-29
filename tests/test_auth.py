"""Tests for extracting the caller's TfNSW API key from request headers."""

from unittest import mock

import pytest
from tfnsw_trip_planner import TripPlannerClient

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
