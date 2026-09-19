"""A restart is a wait, not a failure.

Updating an add-on restarts Home Assistant, and Core needs a while before it
answers again. The add-on keeps running through that, so opening it lands on a
Supervisor with nothing to proxy to, which answers 502. The panel reported that
verbatim - `Error loading data: 502, message='Bad Gateway'` - which reads like
something is broken and offers the reader nothing to do.
"""

import asyncio
from unittest.mock import patch

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
import pytest
import yarl

import core_readiness
import web_ui

URL = "http://supervisor/core/api/states"


def response_error(status):
    """What aiohttp raises for a status, as raise_for_status() builds it.

    The request it carries is part of the error: reading it is how the message
    the panel used to show came about.
    """
    url = yarl.URL(URL)
    info = aiohttp.RequestInfo(url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url)
    return aiohttp.ClientResponseError(
        request_info=info,
        history=(),
        status=status,
        message="Bad Gateway" if status == 502 else "Service Unavailable",
    )


@pytest.fixture
def client():
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


@pytest.mark.parametrize("status", [502, 503, 504])
def test_the_supervisor_answering_for_a_missing_core_is_a_wait(status):
    assert core_readiness.core_is_starting(response_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_any_other_status_is_a_real_failure(status):
    assert core_readiness.core_is_starting(response_error(status)) is False


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ServerDisconnectedError(),
        aiohttp.ServerTimeoutError(),
        asyncio.TimeoutError(),
    ],
)
def test_a_connection_that_goes_away_is_a_wait(error):
    """Core is restarted in place: the socket goes before the proxy answers."""
    assert core_readiness.core_is_starting(error) is True


def test_a_programming_error_is_not_a_wait():
    assert core_readiness.core_is_starting(ValueError("nonsense")) is False


def test_the_hierarchy_says_it_is_waiting(client):
    with patch.object(web_ui, "load_areas_and_entities", side_effect=response_error(502)):
        response = client.get("/api/hierarchy")

    assert response.status_code == 503
    assert response.get_json()["core_starting"] is True


def test_a_real_failure_still_reads_as_one(client):
    with patch.object(web_ui, "load_areas_and_entities", side_effect=RuntimeError("no templates")):
        response = client.get("/api/hierarchy")

    assert response.status_code == 500
    body = response.get_json()
    assert "core_starting" not in body
    assert "no templates" in body["error"]


def test_the_broken_references_say_it_too(client):
    """They are read straight after the hierarchy, into the same restart."""
    with patch.object(web_ui, "get_reference_checker", side_effect=response_error(503)):
        response = client.get("/api/broken_references")

    assert response.status_code == 503
    assert response.get_json()["core_starting"] is True
