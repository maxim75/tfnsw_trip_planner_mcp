"""Shared fixtures."""

from unittest import mock

import pytest


class FakeContext:
    """Stands in for the MCP Context injected into a tool."""

    def __init__(self, headers=None):
        self.headers = {"x-api-key": "test-key"} if headers is None else headers


@pytest.fixture
def ctx():
    return FakeContext()


@pytest.fixture
def ctx_without_key():
    return FakeContext(headers={})


@pytest.fixture
def client():
    """Patch the library client so no request ever leaves the machine.

    Patched at the point `auth.py` imported it, so key extraction still runs for
    real and only the outbound HTTP is replaced.
    """
    with mock.patch("tfnsw_trip_planner_mcp.auth.TripPlannerClient") as factory:
        instance = factory.return_value
        instance.close.return_value = None
        yield instance
