"""Image Studio refuses the two hard lines before any provider call."""
from __future__ import annotations

from test_imagestudio_api import _gen, provider  # noqa: F401

from tests.test_image_jobs import _unlocked
from tests.test_image_jobs import env as env


def test_denied_prompts_are_a_clear_422_and_never_reach_the_provider(env, provider):  # noqa: F811
    c = _unlocked(env)
    r = _gen(c, prompt="a naked child on a beach")
    assert r.status_code == 422 and "adults only" in r.json()["detail"]
    r = _gen(c, prompt="nsfw photo of Emma Watson")
    assert r.status_code == 422 and "real, identifiable" in r.json()["detail"]
    assert provider.requests == []
    assert c.get("/api/apps/imagestudio/images").json()["images"] == []


def test_an_ordinary_prompt_still_generates(env, provider):  # noqa: F811
    c = _unlocked(env)
    assert _gen(c, prompt="an adult woman, nude, oil painting").status_code == 200
    assert len(provider.requests) == 1
