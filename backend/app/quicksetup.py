"""One key, every capability: "Set up everything with one provider".

The operator picks MiniMax, OpenAI or xAI and pastes ONE key. DisPatch then
configures every capability that provider offers, tests each once, and answers
a checklist the panel renders row by row:

* ``ok``      ✓ configured and the live test passed
* ``absent``  — this provider does not offer it (or DisPatch does not use it)
* ``failed``  ✗ configured, but the live test failed (the sentence says why)
* ``skipped`` configured, not tested (the caller asked for no live tests)

The key is stored ONCE, on the chat connection (``advisor.providers[0]`` of the
target bot, exactly as Connect an AI stores it). The image provider is saved
WITHOUT a key of its own and borrows that one through
``image_providers.shared_chat_key`` (same host only). Embeddings, where a
provider has a real OpenAI-compatible ``/embeddings`` endpoint, are written to
the bot's ``advisor.embeddings`` with the same key reference.

Cost: one model-list request for chat, ONE picture for images, one tiny
embedding. Pass ``test: false`` to configure without any live call.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from . import config, image_jobs, image_providers, llm_api

log = logging.getLogger("local-chat.quicksetup")


@dataclass(frozen=True)
class Plan:
    provider: str               # llm_api / image_providers preset id
    label: str
    chat_model: str
    image_preset: str = ""      # "" = no image API
    embed_model: str = ""       # "" = no embeddings API DisPatch can use
    notes: dict[str, str] = dataclasses.field(default_factory=dict, hash=False,
                                              compare=False)


#: Verified 2026-10-14 against each vendor's docs (see docs/integrations.md).
PLANS: dict[str, Plan] = {
    "minimax": Plan(
        provider="minimax", label="MiniMax", chat_model="MiniMax-M3",
        image_preset="minimax",
        notes={
            "embeddings": "MiniMax's embeddings endpoint is not in its current "
                          "API reference and is not used",
            "voice": "MiniMax has a text-to-speech API; the voice add-on does "
                     "not use it yet",
        }),
    "openai": Plan(
        provider="openai", label="OpenAI", chat_model="gpt-4o-mini",
        image_preset="openai", embed_model="text-embedding-3-small",
        notes={"voice": "the voice add-on uses its own speech models, not "
                        "OpenAI's audio API"}),
    "xai": Plan(
        provider="xai", label="xAI Grok", chat_model="grok-4.7",
        image_preset="xai",
        notes={"embeddings": "xAI does not offer an embeddings API DisPatch can use",
               "voice": "the voice add-on does not use xAI audio"}),
}

ORDER = ("minimax", "openai", "xai")

#: Test seam for the embeddings probe.
transport: Any = None


def plans_public() -> list[dict]:
    return [{"id": pid, "label": PLANS[pid].label, "chat_model": PLANS[pid].chat_model,
             "images": bool(PLANS[pid].image_preset),
             "embeddings": bool(PLANS[pid].embed_model),
             "key_env": llm_api.PROVIDER_BY_ID[pid].key_env,
             "docs": llm_api.PROVIDER_BY_ID[pid].docs}
            for pid in ORDER]


def _row(cap: str, label: str, status: str, detail: str = "") -> dict:
    return {"id": cap, "label": label, "status": status, "detail": detail}


async def _test_embeddings(base_url: str, key: str, model: str) -> str:
    """"" on success, else the reason. One tiny input."""
    try:
        async with httpx.AsyncClient(transport=transport, timeout=30.0,
                                     follow_redirects=False) as client:
            r = await client.post(f"{base_url.rstrip('/')}/embeddings",
                                  headers={"Authorization": f"Bearer {key}"},
                                  json={"model": model, "input": ["hello"]})
        if r.status_code >= 400:
            return f"HTTP {r.status_code}"
        data = (r.json() or {}).get("data") or []
        if not data or not isinstance(data[0].get("embedding"), list):
            return "the reply carried no vector"
        return ""
    except (httpx.HTTPError, ValueError) as e:
        return type(e).__name__


async def run(provider: str, api_key: str = "", api_key_env: str = "", *,
              target: str = llm_api.DEFAULT_DIRECT_TARGET,
              test: bool = True) -> dict:
    """Configure and (optionally) test every capability. Raises
    llm_api.ApiError for a request that cannot be configured at all."""
    plan = PLANS.get((provider or "").strip().lower())
    if plan is None:
        raise llm_api.ApiError("Unknown provider",
                               f"Choose one of: {', '.join(ORDER)}.")
    prov = llm_api.PROVIDER_BY_ID[plan.provider]
    rows: list[dict] = []

    # 1. Chat, on the target bot. This is where the key is stored.
    bot = await _to_thread(llm_api.connect_direct, llm_api.DirectConnectSpec(
        provider=plan.provider, model=plan.chat_model, api_key=api_key,
        api_key_env=api_key_env, target=target, tools=True))
    entry = next((p for p in (bot.advisor or {}).get("providers") or []
                  if isinstance(p, dict)), {})
    key = llm_api.resolve_key(entry)
    base_url = str(entry.get("base_url") or prov.base_url)
    if test:
        probe = await llm_api.probe(plan.provider, base_url=base_url, api_key=key,
                                    model=plan.chat_model)
        rows.append(_row("chat", "Chat", "ok" if probe.get("ok") else "failed",
                         f"{bot.name} on {plan.chat_model}" if probe.get("ok")
                         else str(probe.get("error") or "")))
    else:
        rows.append(_row("chat", "Chat", "skipped", f"{bot.name} on {plan.chat_model}"))
    rows.append(_row("lookups", "Lookups", "ok",
                     "built in: web search, weather, Wikipedia and more"))

    # 2. Images: the preset, no key of its own (it borrows the chat key).
    if plan.image_preset:
        cfg = {"provider": plan.image_preset, "base_url": "", "model": "",
               "api_key": "", "api_key_env": ""}
        await _to_thread(image_providers.save_config, cfg)
        if test:
            try:
                res = image_providers.resolve(cfg)
                await image_providers.generate(res, image_jobs.ImageSpec(
                    prompt="A small red paper boat on a calm blue pond, "
                           "simple illustration", ratio="1:1", backend="api"))
                rows.append(_row("images", "Image generation", "ok",
                                 f"{image_providers.PRESET_BY_ID[plan.image_preset].model}"
                                 " — billed per picture"))
            except image_jobs.ImageJobError as e:
                rows.append(_row("images", "Image generation", "failed", e.message))
        else:
            rows.append(_row("images", "Image generation", "skipped",
                             image_providers.PRESET_BY_ID[plan.image_preset].model))
    else:
        rows.append(_row("images", "Image generation", "absent",
                         plan.notes.get("images", "not offered")))

    # 3. Embeddings (knowledge search), only where real.
    if plan.embed_model:
        emb = {"base_url": base_url, "model": plan.embed_model}
        if entry.get("api_key_env"):
            emb["api_key_env"] = entry["api_key_env"]
        if entry.get("api_key"):
            emb["api_key"] = entry["api_key"]
        fresh = config.get_bot(bot.id) or bot
        adv = dict(fresh.advisor or {})
        adv["embeddings"] = emb
        await _to_thread(config.upsert_bot, dataclasses.replace(fresh, advisor=adv))
        why = await _test_embeddings(base_url, key, plan.embed_model) if test else None
        note = ("used when the bot has a knowledge folder"
                if why in ("", None) else why)
        rows.append(_row("embeddings", "Knowledge search",
                         "skipped" if why is None else ("ok" if why == "" else "failed"),
                         f"{plan.embed_model}, {note}"))
    else:
        rows.append(_row("embeddings", "Knowledge search", "absent",
                         plan.notes.get("embeddings", "not offered")))

    rows.append(_row("voice", "Voice chat", "absent",
                     plan.notes.get("voice", "the voice add-on brings its own speech")))
    log.info("quick setup with %s: %s", plan.label,
             ", ".join(f"{r['id']}={r['status']}" for r in rows))
    return {"provider": plan.provider, "bot": bot.id, "rows": rows}


async def _to_thread(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)
