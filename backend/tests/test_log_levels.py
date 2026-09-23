"""Two ERROR lines that were not errors (C21).

* "gateway-ws: no connection after 20s" fired on every start: the gateway
  needs ~70 s to come up and local-chat is only ordered after it.
* A client dropping mid-exchange raised Starlette's "WebSocket is not
  connected" RuntimeError and was logged as a full traceback.
"""
from __future__ import annotations

from types import SimpleNamespace

from starlette.websockets import WebSocketState

from app import main


def test_a_receive_on_a_socket_that_already_closed_is_not_an_error():
    gone = SimpleNamespace(application_state=WebSocketState.DISCONNECTED,
                           client_state=WebSocketState.CONNECTED)
    live = SimpleNamespace(application_state=WebSocketState.CONNECTED,
                           client_state=WebSocketState.CONNECTED)
    err = RuntimeError('WebSocket is not connected. Need to call "accept" first.')
    assert main._ws_already_gone(gone, err)
    assert not main._ws_already_gone(live, err)
    assert not main._ws_already_gone(gone, ValueError("bad frame"))


def test_the_gateway_notice_is_a_warning_while_the_process_is_young():
    import inspect
    src = inspect.getsource(main._gateway_ws_start)
    assert "log.warning if young else log.error" in src
