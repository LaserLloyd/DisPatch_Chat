"""The data tools (weather, Wikipedia, exchange rates, news),
against an ``httpx.MockTransport`` (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.livetools import currency, weather, web_search, wikipedia
from app.livetools.registry import ToolRegistry

GEO_LISBON = {
    "results": [
        {
            "name": "Lisbon",
            "latitude": 26.33,
            "longitude": 127.8,
            "country": "Portugal",
            "country_code": "PT",
            "admin1": "Lisbon",
        }
    ]
}
GEO_PORTLAND = {
    "results": [
        {
            "name": "Portland",
            "latitude": 45.5,
            "longitude": -122.7,
            "country": "United States",
            "admin1": "Oregon",
        },
        {
            "name": "Portland",
            "latitude": 43.7,
            "longitude": -70.3,
            "country": "United States",
            "admin1": "Maine",
        },
    ]
}
FORECAST = {
    "timezone": "Asia/Tokyo",
    "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h", "precipitation": "mm"},
    "current": {
        "time": "2026-10-01T20:15",
        "temperature_2m": 27.4,
        "apparent_temperature": 30.1,
        "relative_humidity_2m": 78,
        "precipitation": 0.0,
        "weather_code": 2,
        "wind_speed_10m": 14.9,
        "wind_gusts_10m": 25.0,
    },
    "daily_units": {"temperature_2m_max": "°C", "precipitation_sum": "mm"},
    "daily": {
        "time": ["2026-10-01", "2026-10-02"],
        "weather_code": [2, 61],
        "temperature_2m_max": [29.0, 27.5],
        "temperature_2m_min": [24.0, 23.1],
        "precipitation_probability_max": [10, 70],
        "precipitation_sum": [0.0, 4.2],
    },
}


def transport(routes: dict[str, object], seen: list[httpx.Request] | None = None):
    """Answer by URL path; a value that is an int is an HTTP status with an empty body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        for path, reply in routes.items():
            if request.url.path.endswith(path):
                if isinstance(reply, int):
                    return httpx.Response(reply, json={})
                return httpx.Response(200, json=reply)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


# --- weather -------------------------------------------------------------------------------


async def test_weather_report():
    seen: list[httpx.Request] = []
    t = transport({"/v1/search": GEO_LISBON, "/v1/forecast": FORECAST}, seen)
    r = await weather.run("Lisbon", transport=t)
    assert r.ok
    assert r.summary == "Weather: Lisbon, Portugal"
    assert "Now: 27°C (feels like 30°C), partly cloudy, humidity 78%" in r.content
    assert "Fri 2 Oct: light rain, 23 to 28°C, rain chance 70% (4.2 mm)" in r.content
    assert r.content.endswith("Source: Open-Meteo.com")
    forecast = seen[1].url.params
    assert forecast["latitude"] == "26.33" and forecast["timezone"] == "auto"
    assert "temperature_unit" not in forecast


async def test_weather_imperial_and_days_clamped():
    seen: list[httpx.Request] = []
    t = transport({"/v1/search": GEO_LISBON, "/v1/forecast": FORECAST}, seen)
    await weather.run("Lisbon", days=30, units="imperial", transport=t)
    params = seen[1].url.params
    assert params["temperature_unit"] == "fahrenheit" and params["wind_speed_unit"] == "mph"
    assert params["forecast_days"] == "7"


async def test_weather_uses_home_location_and_region_hint():
    seen: list[httpx.Request] = []
    t = transport({"/v1/search": GEO_PORTLAND, "/v1/forecast": FORECAST}, seen)
    r = await weather.run("", default_location="Portland, Maine", transport=t)
    assert r.ok and r.summary == "Weather: Portland, Maine, United States"
    assert seen[0].url.params["name"] == "Portland"
    assert seen[1].url.params["latitude"] == "43.7"


async def test_weather_without_any_location_asks():
    r = await weather.run(None, transport=transport({}))
    assert not r.ok and "Ask the user which city" in r.content


async def test_weather_unknown_place_and_outage():
    r = await weather.run("Nowhereville", transport=transport({"/v1/search": {"results": []}}))
    assert not r.ok and "No place called 'Nowhereville'" in r.content
    r = await weather.run("Lisbon", transport=transport({"/v1/search": 503}))
    assert not r.ok and "HTTP 503" in r.content


def test_weather_codes():
    assert weather.describe(0) == "clear sky"
    assert weather.describe("95") == "thunderstorm"
    assert weather.describe(None) == "unknown conditions"


# --- wikipedia -----------------------------------------------------------------------------


async def test_wikipedia_summary():
    t = transport(
        {
            "/w/api.php": ["lisbon", ["Lisbon District"], [""], [""]],
            "/page/summary/Lisbon_District": {
                "title": "Lisbon District",
                "description": "District of Portugal",
                "extract": "Lisbon District is a district of Portugal.",
                "content_urls": {
                    "desktop": {"page": "https://en.wikipedia.org/wiki/Lisbon_District"}
                },
            },
        }
    )
    r = await wikipedia.run("lisbon", transport=t)
    assert r.ok and r.summary == "Wikipedia: Lisbon District"
    assert r.content.splitlines() == [
        "Lisbon District (District of Portugal)",
        "Lisbon District is a district of Portugal.",
        "URL: https://en.wikipedia.org/wiki/Lisbon_District",
    ]


async def test_wikipedia_no_match_is_not_an_error():
    r = await wikipedia.run("zzqqxx", transport=transport({"/w/api.php": ["zzqqxx", [], [], []]}))
    assert r.ok and "No Wikipedia article" in r.content


# --- exchange rates ------------------------------------------------------------------------


async def test_exchange_rate():
    seen: list[httpx.Request] = []
    t = transport(
        {
            "/v1/latest": {
                "amount": 10.0,
                "base": "USD",
                "date": "2026-09-30",
                "rates": {"JPY": 1569.97},
            }
        },
        seen,
    )
    r = await currency.run("usd", "jpy", 10, transport=t)
    assert r.ok
    assert r.content == "10.00 USD = 1,569.97 JPY (ECB reference rate for 2026-09-30)"
    assert seen[0].url.params["from"] == "USD" and seen[0].url.params["to"] == "JPY"


async def test_exchange_rate_falls_back_to_the_mirror_on_outage():
    t = transport({"/v1/latest": 503, "/latest": {"date": "2026-09-30", "rates": {"EUR": 0.92}}})
    r = await currency.run("USD", "EUR", transport=t)
    assert r.ok and "0.92 EUR" in r.content


@pytest.mark.parametrize(
    ("frm", "to", "amount", "needle"),
    [
        ("US", "JPY", 1, "3-letter"),
        ("USD", "JPY", -5, "positive"),
        ("USD", "JPY", "abc", "positive"),
    ],
)
async def test_exchange_rate_bad_arguments(frm, to, amount, needle):
    r = await currency.run(frm, to, amount, transport=transport({}))
    assert not r.ok and needle in r.content


async def test_exchange_rate_unsupported_currency():
    r = await currency.run("USD", "XYZ", transport=transport({"/v1/latest": 404}))
    assert not r.ok and "No ECB reference rate" in r.content


# --- registry wiring -----------------------------------------------------------------------


async def test_registry_routes_the_data_tools():
    t = transport(
        {
            "/v1/search": GEO_LISBON,
            "/v1/forecast": FORECAST,
            "/v1/latest": {"date": "2026-09-30", "rates": {"JPY": 157.0}},
        }
    )
    registry = ToolRegistry({"location": "Lisbon", "units": "metric"}, transport=t)
    assert registry.location == "Lisbon"
    enabled = ["weather", "exchange_rate"]
    r = await registry.call("weather", "{}", enabled=enabled, max_chars=6000)
    assert r.ok and "Weather for Lisbon, Portugal" in r.content
    r = await registry.call(
        "exchange_rate", json.dumps({"from": "USD", "to": "JPY"}), enabled=enabled, max_chars=6000
    )
    assert r.ok and "157.00 JPY" in r.content


async def test_news_search_formats_date_and_source(monkeypatch):
    class Client:
        def news(self, q, max_results=5, **kw):
            Client.seen = q
            return [
                {
                    "title": "Typhoon nears Lisbon",
                    "url": "https://news.example/t",
                    "body": "Heavy rain expected.",
                    "date": "2026-10-01T09:00:00+00:00",
                    "source": "Example News",
                }
            ]

    monkeypatch.setattr(web_search, "_ddgs_factory", lambda timeout=10: Client())
    registry = ToolRegistry({"web_search_min_interval_s": 0})
    r = await registry.call(
        "news_search",
        json.dumps({"query": "lisbon typhoon"}),
        enabled=["news_search"],
        max_chars=6000,
    )
    assert r.ok and Client.seen == "lisbon typhoon"
    assert r.summary.startswith("News: lisbon typhoon")
    assert "Typhoon nears Lisbon (2026-10-01T09:00:00+00:00 · Example News)" in r.content


# --- weather details ----------------------------------------------------------------------

async def test_geocode_retries_without_a_stray_trailing_word():
    names: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/search"):
            names.append(request.url.params["name"])
            hit = request.url.params["name"] == "Lisbon"
            return httpx.Response(200, json=GEO_LISBON if hit else {"results": []})
        return httpx.Response(200, json=FORECAST)

    r = await weather.run("Lisbon somewhere", transport=httpx.MockTransport(handler))
    assert r.ok and r.summary == "Weather: Lisbon, Portugal"
    assert names == ["Lisbon somewhere", "Lisbon"]


def test_country_hint_matches_whole_words_and_codes():
    perth_au = {"name": "Perth", "country": "Australia", "country_code": "AU", "admin1": "WA"}
    perth_us = {"name": "Perth", "country": "United States", "country_code": "US"}
    assert not weather._matches(perth_au, "us")
    assert weather._matches(perth_us, "us")
    assert weather._matches(perth_au, "australia")


def test_null_precipitation_is_not_printed_as_none():
    data = {**FORECAST, "current": {**FORECAST["current"], "precipitation": None}}
    assert "None" not in weather.format_report("X", data)
