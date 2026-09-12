"""The Flask application still answers when it is served over ASGI."""

import asyncio

import asgi
import web_ui

INGRESS = "172.30.32.2"


def call(application, path, peer=INGRESS):
    """Drive the ASGI application by hand and collect the response."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "client": (peer, 51000),
        "server": ("localhost", 5000),
    }
    received = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
        elif message["type"] == "http.response.body":
            received["body"] += message.get("body", b"")

    asyncio.run(application(scope, receive, send))
    return received


def test_the_web_interface_answers_through_asgi():
    application = asgi.build(web_ui.app)

    answer = call(application, "/api/network")

    assert answer["status"] == 200
    assert b"policy" in answer["body"]


def test_the_gate_still_sees_the_real_caller_through_asgi():
    """The peer address has to survive the wrapping, or the gate is blind."""
    application = asgi.build(web_ui.app)

    assert call(application, "/api/network", peer="203.0.113.7")["status"] == 403
    assert call(application, "/api/network", peer=INGRESS)["status"] == 200
