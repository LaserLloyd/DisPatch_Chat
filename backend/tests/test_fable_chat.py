"""4.3 Fable review (chat agent): Image Studio's cost note and provider error wording."""
from __future__ import annotations

import httpx

from app import image_providers


def _resp(status, body=None):
    req = httpx.Request("POST", "http://127.0.0.1:9/v1/images/generations")
    if body is None:
        return httpx.Response(status, text="", request=req)
    return httpx.Response(status, json=body, request=req)


def test_custom_provider_has_no_price_note_so_it_is_never_called_metered():
    assert image_providers.PRESET_BY_ID["custom"].price_note == ""
    assert image_providers.PRESET_BY_ID["openai"].price_note  # real prices still shown


def test_a_custom_server_error_reads_as_a_plain_sentence():
    e = image_providers._error_from_status(_resp(404), "Custom (OpenAI-compatible)")
    assert e.message == "The picture server answered: not found"
    e = image_providers._error_from_status(_resp(400, {"error": {"message": "bad size"}}), "OpenAI")
    assert e.message == "OpenAI answered: bad size"
    assert "HTTP" not in e.message
