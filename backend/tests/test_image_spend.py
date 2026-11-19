"""Daily spend caps for paid picture providers (app/image_spend.py).

Run: cd backend && uv run pytest tests/test_image_spend.py
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import config, image_spend, settings_store


@pytest.fixture
def spend(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    settings_store.reset_for_tests()
    image_spend.reset_for_tests()
    clock = {"now": datetime(2026, 11, 19, 10, 0, tzinfo=timezone(timedelta(hours=9)))}
    monkeypatch.setattr(image_spend, "_now", lambda: clock["now"])
    yield clock
    settings_store.reset_for_tests()
    image_spend.reset_for_tests()


def caps(**kw):
    settings_store.set_many({f"connections.image_spend.{k}": v for k, v in kw.items()})


def used(unit="usd", scope=image_spend.INSTALL):
    for ln in image_spend.usage_all()["install" if scope == image_spend.INSTALL else "profiles"]:
        if scope == image_spend.INSTALL and ln["unit"] == unit:
            return ln["used"]
    return 0.0


def test_free_providers_are_never_counted_or_blocked(spend):
    caps(profile_usd_per_day=0.0001, install_usd_per_day=0.0001, profile_pictures_per_day=1)
    for provider in ("workflows", "server", "custom", "something-else"):
        assert image_spend.cost_of(provider, "m", 5) is None
        assert image_spend.reserve("p1", image_spend.cost_of(provider, "m", 5)) is None
    assert used("usd") == 0


def test_prices_come_from_the_provider_notes(spend):
    assert image_spend.cost_of("minimax", "image-01", 4).amount == pytest.approx(0.014)
    assert image_spend.cost_of("minimax", "MiniMax-H3-Image", 1).amount == pytest.approx(0.02)
    assert image_spend.cost_of("minimax", "MiniMax-H3-Image-Highspeed", 1).amount == pytest.approx(0.04)
    assert image_spend.cost_of("minimax", "brand-new", 1).amount == image_spend.MINIMAX_USD_DEFAULT
    c = image_spend.cost_of("civitai", "x", 2, buzz_total=14)
    assert (c.unit, c.amount) == ("buzz", 14)
    assert image_spend.cost_of("civitai", "x", 2).amount == 2 * image_spend.BUZZ_FALLBACK
    assert image_spend.cost_of("openai", "gpt-image-1", 3).unit == "pictures:openai"


def test_profile_cap_refuses_before_anything_and_names_cap_and_reset(spend):
    caps(profile_usd_per_day=0.05, install_usd_per_day=0)
    cost = image_spend.cost_of("minimax", "MiniMax-H3-Image", 1)      # $0.02
    held = [image_spend.reserve("p1", cost) for _ in range(2)]
    assert all(held)
    with pytest.raises(image_spend.SpendRefused) as e:
        image_spend.reserve("p1", cost)
    msg = e.value.message
    assert "$0.05" in msg and "for this profile" in msg and "midnight (2026-11-20 00:00" in msg
    assert len(msg) < 300                                  # survives the chat line's clip
    assert used("usd") == pytest.approx(0.04)              # the refusal reserved nothing
    assert image_spend.reserve("p2", cost) is not None     # another profile has its own cap


def test_install_cap_spans_profiles(spend):
    caps(profile_usd_per_day=0, install_usd_per_day=0.05)
    cost = image_spend.cost_of("minimax", "MiniMax-H3-Image", 1)
    image_spend.reserve("a", cost)
    image_spend.reserve("b", cost)
    with pytest.raises(image_spend.SpendRefused) as e:
        image_spend.reserve("c", cost)
    assert "for the whole household" in e.value.message


def test_failure_releases_and_adjust_trims(spend):
    caps(profile_usd_per_day=0.05, install_usd_per_day=0.05)
    cost = image_spend.cost_of("minimax", "MiniMax-H3-Image", 2)      # $0.04
    h = image_spend.reserve("p", cost)
    assert used() == pytest.approx(0.04)
    h.release()
    h.release()                                                       # twice is safe
    assert used() == pytest.approx(0)
    h = image_spend.reserve("p", cost)
    h.adjust(0.02)                                                    # one of two came back
    h.commit()
    h.release()                                                       # committed: no-op
    assert used() == pytest.approx(0.02)


def test_parallel_jobs_cannot_overshoot(spend):
    caps(profile_usd_per_day=0.10, install_usd_per_day=0.10)
    cost = image_spend.cost_of("minimax", "MiniMax-H3-Image", 1)      # $0.02 -> five fit
    ok, refused = [], []

    def go():
        try:
            ok.append(image_spend.reserve("p", cost))
        except image_spend.SpendRefused:
            refused.append(1)

    threads = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 5 and len(refused) == 15
    assert used() == pytest.approx(0.10)


def test_resets_at_local_midnight(spend):
    caps(profile_usd_per_day=0.02, install_usd_per_day=0.02)
    cost = image_spend.cost_of("minimax", "MiniMax-H3-Image", 1)
    image_spend.reserve("p", cost)
    with pytest.raises(image_spend.SpendRefused):
        image_spend.reserve("p", cost)
    spend["now"] = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(image_spend.SpendRefused):
        image_spend.reserve("p", cost)
    spend["now"] = datetime(2026, 11, 20, 0, 0, 1, tzinfo=timezone(timedelta(hours=9)))
    assert image_spend.reserve("p", cost) is not None
    assert used() == pytest.approx(0.02)                              # only the new day's use


def test_pictures_are_counted_per_provider_when_no_price(spend):
    caps(profile_pictures_per_day=3, install_pictures_per_day=0)
    for _ in range(3):
        image_spend.reserve("p", image_spend.cost_of("openai", "m", 1))
    with pytest.raises(image_spend.SpendRefused) as e:
        image_spend.reserve("p", image_spend.cost_of("openai", "m", 1))
    assert "openai pictures" in e.value.message
    assert image_spend.reserve("p", image_spend.cost_of("xai", "m", 1))   # xAI has its own count


def test_zero_means_no_limit(spend):
    caps(profile_usd_per_day=0, install_usd_per_day=0)
    cost = image_spend.cost_of("minimax", "MiniMax-H3-Image-Highspeed", 9)
    for _ in range(50):
        assert image_spend.reserve("p", cost)


def test_notice_at_eighty_percent_and_usage_view(spend):
    caps(profile_usd_per_day=0.10, install_usd_per_day=0)
    cost = image_spend.cost_of("minimax", "MiniMax-H3-Image", 1)
    notes = [image_spend.reserve("p", cost).notice for _ in range(4)]
    assert notes[:3] == ["", "", ""] and notes[3].startswith("80%") and "midnight" in notes[3]
    mine = image_spend.usage_for("p")
    assert mine["notice"].startswith("80%")
    line = next(ln for ln in mine["lines"] if ln["unit"] == "usd")
    assert line["used"] == pytest.approx(0.08) and line["cap"] == 0.10 and line["near"]
    other = image_spend.usage_for("q")                              # another profile sees only its own
    assert next(ln for ln in other["lines"] if ln["unit"] == "usd")["used"] == 0
    assert other["notice"] == ""


def test_defaults_are_generous_for_normal_use(spend):
    # 100 H3 pictures a day by one profile, 1000 image-01 pictures: all inside the shipped caps
    assert 100 * 0.02 <= image_spend.cap("profile", "usd")
    assert 1000 * 0.0035 <= image_spend.cap("profile", "usd")
    assert image_spend.cap("install", "usd") >= image_spend.cap("profile", "usd")
