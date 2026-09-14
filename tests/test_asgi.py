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
    assert b"state" in answer["body"]


def test_the_gate_still_sees_the_real_caller_through_asgi():
    """The peer address has to survive the wrapping, or the gate is blind."""
    application = asgi.build(web_ui.app)

    assert call(application, "/api/network", peer="203.0.113.7")["status"] == 403
    assert call(application, "/api/network", peer=INGRESS)["status"] == 200


def test_requests_that_wait_are_served_at_the_same_time():
    """A slow request must not hold up every other one.

    This is not theoretical: the first bridge tried here sent every call
    through a single thread, so eight requests that each wait a fifth of a
    second took eight fifths, and the web interface froze while the hierarchy
    was being rebuilt.
    """
    import time

    def waiting_app(environ, start_response):
        time.sleep(0.2)
        start_response("200 OK", [("content-type", "text/plain")])
        return [b"ok"]

    application = asgi.build(waiting_app)

    async def eight_at_once():
        started = time.perf_counter()
        await asyncio.gather(*(_drive(application) for _ in range(8)))
        return time.perf_counter() - started

    took = asyncio.run(eight_at_once())

    assert took < 0.6, f"eight waiting requests took {took:.2f}s, so they ran one after another"


async def _drive(application):
    """Send one bare request through an ASGI application."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "client": (INGRESS, 51000),
        "server": ("localhost", 5000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await application(scope, receive, send)


def test_the_mcp_address_works_without_a_trailing_slash():
    """Clients write /mcp. A mount only covers what is below it, so the bare
    address would otherwise fall through to the web interface and be refused."""
    reached = []

    async def mcp_app(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mcp"})

    application = asgi.build(web_ui.app, mcp_app)

    assert call(application, "/mcp")["body"] == b"mcp"
    assert call(application, "/mcp/")["body"] == b"mcp"
    assert reached == ["/mcp/", "/mcp/"]


def test_everything_else_still_goes_to_the_web_interface():
    async def mcp_app(scope, receive, send):
        raise AssertionError("the web interface was routed to MCP")

    application = asgi.build(web_ui.app, mcp_app)

    assert call(application, "/api/network")["status"] == 200
