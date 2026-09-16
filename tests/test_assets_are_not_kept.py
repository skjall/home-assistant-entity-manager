"""The page and its assets must not be served from yesterday.

Home Assistant puts a service worker in front of an add-on that keys on the path
alone, so a cache-busting query is not enough. A deploy then leaves the browser
on the old stylesheet while the page itself is new, and the result looks like a
layout bug rather than a caching one.
"""

import pytest

import web_ui


@pytest.fixture
def client():
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


@pytest.mark.parametrize(
    "path",
    ["/static/css/styles.css", "/static/js/config.js", "/static/translations/de.json"],
)
def test_an_asset_says_it_must_not_be_kept(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert "no-store" in response.headers.get("Cache-Control", "")


def test_a_font_may_be_kept(client):
    """It is named after its content and never changes under its own name."""
    response = client.get("/static/css/remixicon.woff2")

    assert "no-store" not in response.headers.get("Cache-Control", "")
