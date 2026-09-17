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


def test_a_versioned_path_serves_the_same_file(client):
    """The version is only there to make the path new; it names nothing."""
    plain = client.get("/static/css/styles.css")
    versioned = client.get("/static/css/v1234567/styles.css")

    assert versioned.status_code == 200
    assert versioned.data == plain.data


def test_a_versioned_script_is_served_too(client):
    response = client.get("/static/js/v1234567/config.js")

    assert response.status_code == 200
    assert b"jobSteps" in response.data


def test_the_version_moves_when_a_file_does(tmp_path, monkeypatch):
    """A deploy touches the files, so the page asks for a path never seen."""
    before = web_ui.asset_version()

    import os
    import time

    stylesheet = os.path.join(os.path.dirname(os.path.abspath(web_ui.__file__)), "static/css/styles.css")
    later = time.time() + 5
    original = os.stat(stylesheet)
    try:
        os.utime(stylesheet, (later, later))
        assert web_ui.asset_version() != before
    finally:
        os.utime(stylesheet, (original.st_atime, original.st_mtime))


def test_the_page_points_at_the_versioned_paths(client):
    page = client.get("/").data.decode("utf-8")

    assert "/styles.css" in page
    assert "static/css/v" in page
    assert "static/js/v" in page
