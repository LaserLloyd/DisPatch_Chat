"""4.3 (#2): an unclaimed install now also needs a loopback Host name. TestClient sends
``Host: testserver``; the strict-mode suites that drive an unclaimed install from a loopback
peer use this to address it as a browser on this machine does (``localhost``)."""
from __future__ import annotations


def address_loopback_as_localhost(world, monkeypatch) -> None:
    """Make ``world.client()`` with a loopback peer send ``Host: localhost`` (and a matching
    Origin). Clients with any other peer are unchanged."""
    orig = world.client

    def client(addr=None, *, origin: bool = True):
        c = orig(addr, origin=origin)
        peer = (addr or world.LOOPBACK)[0]
        if peer.startswith("127.") or peer == "::1":
            c.headers["Host"] = "localhost"
            if origin:
                c.headers["Origin"] = "http://localhost"
        return c

    monkeypatch.setattr(world, "client", client)
