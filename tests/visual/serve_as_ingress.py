"""Serve the web UI for the visual test, as Ingress delivers a request.

The add-on answers the web interface only to Home Assistant's Ingress, which
sits on the Supervisor's own network and has already authenticated the user.
Everything arriving over a published port gets the API at most, and only with a
token. A browser on the loopback address is neither, so the gate refuses it -
correctly, and that refusal is worth keeping.

The visual test still needs to look at the real pages. This launcher therefore
puts each loopback request into the shape Ingress would have delivered it in,
by setting the peer address the gate reads. It wraps the app from the outside,
ahead of the middleware that captures the peer address, which is the only place
where this is possible at all - a request header would not do it, by design.

It lives in the test tree on purpose. The add-on has no such switch, and
run.sh sets nothing of the kind: faking the peer address is a thing the tests
may do to themselves, never something the shipped add-on offers.
"""

import os
import sys

# The app modules live in the repository root, one level above tests/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asgi  # noqa: E402
import web_ui  # noqa: E402

# Any address inside the Supervisor's network; the gate only asks which network
# the peer is on, not which host.
AS_INGRESS = "172.30.32.2"


class ArrivesThroughIngress:
    """Hand every request to the app with the peer address Ingress would have."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        environ["REMOTE_ADDR"] = AS_INGRESS
        return self.wsgi_app(environ, start_response)


def main() -> None:
    port = int(os.getenv("WEB_UI_PORT", 5000))
    # Assigning here wraps the chain from the outside, so this runs before the
    # middleware that records the real peer.
    web_ui.app.wsgi_app = ArrivesThroughIngress(web_ui.app.wsgi_app)
    asgi.serve(asgi.build(web_ui.app, None), port)


if __name__ == "__main__":
    main()
