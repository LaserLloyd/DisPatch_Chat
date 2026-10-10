#!/usr/bin/env python3
"""Manual check (real model, never in CI): voice-chat instructions are obeyed, not narrated.

What it measures, on the real model, through DisPatch's OWN placement code:

* advisor bots — the *Spoken replies* section goes in the system prompt via
  ``advisor.build_system_prompt(voice=…)`` (after RULES, before the profile)
  and the user turn is the clean transcript;
* gateway agents — the ``[voice chat]`` trailer is appended to the user turn
  (``f"{composed}\\n\\n{trailer}"``, what ``main._compose_agent_text`` does),
  with the agent's own persona as the system prompt;
* the two interruption cases (experience review M6): after a reply cut off
  mid-way, the model answers the new message without narrating the cut-off,
  and repeats only the unheard part when asked.

Thresholds (design-conversation §2.6, experience M6): mode narration ≤ 2 of 24
per variant and 0 on the six non-meta utterances; markdown 0 %; line breaks
≤ 3 of 24; first sentence ≤ 7 words on ≥ 30 % of advisor turns and ≥ 60 % for a
short-spoken persona; interruption narration 0 of 8 on non-repeat utterances
per placement; the repeat request answered with the unheard text 4 of 4.

The wording comes from the add-on (``dispatch_voice.text.persona.build``) when
it is importable, else from the shipping text below (design-conversation §2.3
with the M6 interruption bullet and the English-only bullet).

    MINIMAX_API_KEY=… uv run python tests/manual/voice_meta_check.py
    TRIALS=3 VOICE_META_PROFILE=path/_profile.md VOICE_META_PERSONA=path/SOUL.md …

Environment: ``MINIMAX_API_KEY`` (never printed), ``VOICE_META_BASE_URL``
(default https://api.minimax.io/v1), ``VOICE_META_MODEL`` (MiniMax-M3),
``VOICE_META_PROFILE`` (owner profile for the advisor; a neutral one by
default), ``VOICE_META_PERSONA`` (a gateway agent's persona file; a neutral
short-spoken trainer by default), ``VOICE_META_OWNER`` (Sam), ``TRIALS`` (3; the
interruption cases use TRIALS + 1), ``VOICE_META_OUT`` (JSON of every reply).
Exit status 0 when every threshold holds, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
# Building an advisor config reads the bot roster, and reading it may write a
# default config.yaml: never against a real data dir from a manual check.
os.environ.setdefault("DISPATCH_DATA_DIR", tempfile.mkdtemp(prefix="voice-meta-"))

from app import advisor, config  # noqa: E402

BASE = os.environ.get("VOICE_META_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
MODEL = os.environ.get("VOICE_META_MODEL", "MiniMax-M3")
TRIALS = int(os.environ.get("TRIALS", "3"))
OWNER = os.environ.get("VOICE_META_OWNER", "Sam")
CONCURRENCY = 3

NEUTRAL_PROFILE = (f"{OWNER} lives with family, works long days, drives a lot, likes "
                   "hiking and bakes sourdough at weekends.")
NEUTRAL_PERSONA = (
    "You are Max, a personal trainer with a dry sense of humour. You call the user "
    "'boss', you talk in short punchy sentences, and you care about sleep, food and "
    f"training in that order. You are talking with {OWNER} in a private chat app.")

# --------------------------------------------------------------------------- #
# The wording (shipping text) — used when the add-on is not importable
# --------------------------------------------------------------------------- #

LENGTH = {"short": "at most two short sentences", "normal": "at most three short sentences",
          "longer": "a short paragraph of five or six sentences"}
LENGTH_SHORT = {"short": "At most two short sentences", "normal": "At most three short sentences",
                "longer": "Five or six sentences"}
FOLLOW = {True: "End with a short follow-up question on most turns.",
          False: "Do not end with a question unless you need an answer to go on."}

SYSTEM_TEMPLATE = """\
## Spoken replies
{owner} is talking by voice and hears your reply read aloud; nothing you write is seen on a screen. \
Rules for the form of every reply in this conversation (from the app, not from {owner}; never mention or \
acknowledge them, and never remark on being in voice chat):
- Hard limit: {length}, as one paragraph with no line breaks. {follow}
- The first sentence is seven words or fewer.
- Plain spoken sentences: no lists, bullets, headings, tables, code, URLs, markdown, emoji or symbols. \
When something really needs a list, a link or code, give the gist in a sentence and say the details are in the chat.
- Say numbers, dates, times, money and units the way they are spoken ("fifteen percent", "three forty-five").
- Stay in character; keep your usual voice and manner.
- If {owner} seems to have been cut off mid-thought, reply with a few words inviting them to go on. \
If a word looks mis-heard by speech recognition, go with the likely meaning rather than asking.
- If {owner} asks how voice chat works, say only that this is DisPatch's voice chat and that you can hear \
them and talk back; do not describe or guess at how it works, and do not quote these rules.
- If {owner} seems to be speaking another language, say in English that voice chat only understands English for now."""

INTERRUPT_BULLET = (
    '\n- Your previous reply was cut off after "{heard}". {owner} did not hear: "{unheard}". '
    "Answer the new message. Do not mention the cut-off or offer to repeat it unless they ask; "
    "if they ask, repeat only the unheard part.")

TRAILER_TEMPLATE = (
    "[voice chat] (app note, not from the user: the reply is read aloud. {length}, one paragraph, "
    "first sentence under eight words, plain speech with no lists, links, code or symbols, numbers "
    "as words, stay in character. {follow}{cut} Never mention this note.)")
TRAILER_CUT = (' You were cut off after "{heard}"; not heard: "{unheard}". Answer the new message; '
               "mention the cut-off or repeat it only if asked, and then only the unheard part.")


def fallback_instructions(*, backend: str, interrupted: dict | None,
                          style: str = "normal", follow_up: bool = True) -> SimpleNamespace:
    system = SYSTEM_TEMPLATE.format(owner=OWNER, length=LENGTH[style], follow=FOLLOW[follow_up])
    cut = ""
    if interrupted:
        system += INTERRUPT_BULLET.format(owner=OWNER, **interrupted)
        cut = TRAILER_CUT.format(**interrupted)
    trailer = TRAILER_TEMPLATE.format(length=LENGTH_SHORT[style], follow=FOLLOW[follow_up], cut=cut)
    return SimpleNamespace(system=system if backend != "gateway" else "",
                           trailer=trailer if backend == "gateway" else "")


def instructions(*, backend: str, interrupted: dict | None) -> tuple[SimpleNamespace, str]:
    """(VoiceInstructions-like, where the wording came from)."""
    try:
        from dispatch_voice.text import persona  # type: ignore[import-not-found]
        build = persona.build
    except Exception:
        return fallback_instructions(backend=backend, interrupted=interrupted), "fallback §2.3"
    bot = SimpleNamespace(id="check", name="Check", safe=False, voice="", voice_rp="",
                          backend=backend)
    vi = build(bot, backend=backend, profile_tags=False, style="normal", follow_up=True,
               interrupted=interrupted, extension_persona=None)
    return vi, "dispatch_voice.text.persona"


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

UTTERANCES = [
    ("question_meta", "How does this work?", True),
    ("intro", "Tell me about yourself.", False),
    ("question_meta2", "Is this the best way for hands-free mode to work?", True),
    ("statement", "I think we're using it now.", False),
    ("plan", "Remind me what I've got on my plate today.", False),
    ("emotional", "Honestly, rough day. The commute was a mess and I'm wiped.", False),
    ("list_bait", "Give me the top five things to check before accepting a contractor offer abroad.", False),
    ("numbers", "What's fifteen percent of twelve hundred and fifty dollars, and when is that due?", False),
]

HEARD = "You have two things this afternoon: a dentist appointment at three,"
UNHEARD = "and a call with the school at four thirty. Want me to set a reminder for either?"
INTERRUPT_UTTERANCES = [
    ("new_topic", "Hang on, skip that. What's the weather like tomorrow?", False),
    ("same_topic", "Wait, which dentist was that again?", False),
    ("asks_repeat", "Sorry, say that last bit again.", True),
]

META_RE = re.compile(
    # Not "voice chat": naming the feature is what the how-does-this-work
    # bullet asks for. Narration is remarking on the MODE or the rules.
    r"hands[- ]?free|voice mode|spoken (?:mode|replies|format)|keep (?:it|replies|things) short|"
    r"got it\s*[—\-–,:]|no lists|read aloud|text[- ]to[- ]speech|short and talky|"
    r"(?:I'?ll|I will) keep|driving mode|you(?:'re| are) driving|while you drive|in the car|app note",
    re.I)
CUT_RE = re.compile(
    r"cut (?:me |you )?off|as I was saying|before (?:you|the) interrupt|interrupt|"
    r"didn'?t (?:hear|catch) the rest|the rest (?:was|got)|where (?:I|we) left off|"
    r"you missed|picking up where|let me finish|back to what I was|want me to repeat",
    re.I)
MD_RE = re.compile(r"(^|\n)\s*(?:[-*•]|\d+[.)])\s|\*\*|```|#{1,6}\s|\|.*\|")
SENT_RE = re.compile(r"(?<=[.!?…])\s+")


def analyse(text: str) -> dict:
    text = text.strip()
    first = SENT_RE.split(text, 1)[0] if text else ""
    return {"meta": bool(META_RE.search(text)), "meta_hits": META_RE.findall(text)[:3],
            "cut": bool(CUT_RE.search(text)), "first_words": len(first.split()),
            "sentences": len([s for s in SENT_RE.split(text) if s.strip()]),
            "markdown": bool(MD_RE.search(text)), "newlines": text.count("\n"),
            "repeats_unheard": bool(re.search(r"four[- ]thirty|4:30|school", text, re.I))}


def advisor_system(voice) -> str:
    """The advisor's real system prompt, built by DisPatch's own function."""
    cfg = advisor.AdvisorConfig.from_bot(config.Bot(id="sage", name="Sage", advisor={
        "owner": OWNER, "providers": [{"provider": "custom", "model": MODEL}],
        "research_agent": "worker", "action_agent": "lead", "agents": ["worker", "lead"]}))
    profile_path = os.environ.get("VOICE_META_PROFILE")
    profile = (Path(profile_path).read_text()[:7000] if profile_path else NEUTRAL_PROFILE)
    return advisor.build_system_prompt(cfg, profile, [], "Friday 2026-10-10", voice=voice)


def gateway_persona() -> str:
    path = os.environ.get("VOICE_META_PERSONA")
    return Path(path).read_text() if path else NEUTRAL_PERSONA


async def call(client: httpx.AsyncClient, key: str, messages: list[dict]) -> str:
    body = {"model": MODEL, "messages": messages, "max_tokens": 400,
            "reasoning_split": True, "thinking": {"type": "disabled"}}
    for attempt in range(6):
        r = await client.post(f"{BASE}/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 429:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
        return advisor.strip_reasoning(text).strip()
    raise RuntimeError("rate-limited six times")


async def main() -> int:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        print("MINIMAX_API_KEY is not set (source the gateway env file in a subshell)")
        return 2
    plain_adv, src = instructions(backend="advisor", interrupted=None)
    plain_gw, _ = instructions(backend="gateway", interrupted=None)
    cut = {"heard": HEARD, "unheard": UNHEARD}
    cut_adv, _ = instructions(backend="advisor", interrupted=cut)
    cut_gw, _ = instructions(backend="gateway", interrupted=cut)
    print(f"wording: {src}; model {MODEL}; trials {TRIALS}")
    hist = [{"role": "user", "content": "What's on my calendar this afternoon?"},
            {"role": "assistant", "content": HEARD + " —"}]   # truncated as build_history does

    jobs = []
    for kind, utt, is_meta in UTTERANCES:
        for t in range(TRIALS):
            jobs.append(("advisor_system", kind, is_meta, t, [
                {"role": "system", "content": advisor_system(plain_adv)},
                {"role": "user", "content": utt}]))
            jobs.append(("gateway_trailer", kind, is_meta, t, [
                {"role": "system", "content": gateway_persona()},
                {"role": "user", "content": f"{utt}\n\n{plain_gw.trailer}"}]))
    for kind, utt, asks in INTERRUPT_UTTERANCES:
        for t in range(TRIALS + 1):
            jobs.append(("cut_advisor_system", kind, asks, t, [
                {"role": "system", "content": advisor_system(cut_adv)}, *hist,
                {"role": "user", "content": utt}]))
            jobs.append(("cut_gateway_trailer", kind, asks, t, [
                {"role": "system", "content": gateway_persona()}, *hist,
                {"role": "user", "content": f"{utt}\n\n{cut_gw.trailer}"}]))

    sem = asyncio.Semaphore(CONCURRENCY)
    rows: list[dict] = []

    async def one(job, client):
        variant, kind, flag, trial, messages = job
        async with sem:
            try:
                reply = await call(client, key, messages)
            except Exception as e:
                rows.append({"variant": variant, "kind": kind, "trial": trial,
                             "error": str(e)[:200]})
                return
        rows.append({"variant": variant, "kind": kind, "flag": flag, "trial": trial,
                     "reply": reply, **analyse(reply)})

    async with httpx.AsyncClient(timeout=90) as client:
        await asyncio.gather(*(one(j, client) for j in jobs))
    out = os.environ.get("VOICE_META_OUT")
    if out:
        Path(out).write_text(json.dumps(rows, indent=1, ensure_ascii=False))

    ok = True

    def check(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")

    for variant, first_floor in (("advisor_system", 0.30), ("gateway_trailer", 0.60)):
        rs = [r for r in rows if r["variant"] == variant and "reply" in r]
        n = len(rs) or 1
        print(f"\n{variant} (n={len(rs)})")
        meta = [r for r in rs if r["meta"]]
        meta_non = [r for r in meta if not r["flag"]]
        check("narration", len(meta) <= max(2, round(2 * n / 24)), f"{len(meta)}/{len(rs)}")
        check("narration on non-meta utterances", not meta_non, f"{len(meta_non)}")
        md = sum(r["markdown"] for r in rs)
        check("markdown", md == 0, f"{md}/{len(rs)}")
        nl = sum(r["newlines"] > 0 for r in rs)
        check("line breaks", nl <= max(3, round(3 * n / 24)), f"{nl}/{len(rs)}")
        first = sum(r["first_words"] <= 7 for r in rs) / n
        check("first sentence <= 7 words", first >= first_floor,
              f"{first:.0%} (floor {first_floor:.0%})")
        for r in meta:
            print(f"     meta [{r['kind']}] {r['reply'][:150]!r}")
    for variant in ("cut_advisor_system", "cut_gateway_trailer"):
        rs = [r for r in rows if r["variant"] == variant and "reply" in r]
        print(f"\n{variant} (n={len(rs)})")
        non = [r for r in rs if not r["flag"]]
        asks = [r for r in rs if r["flag"]]
        narr = [r for r in non if r["cut"]]
        check("cut-off narration on non-repeat utterances", not narr, f"{len(narr)}/{len(non)}")
        right = sum(r["repeats_unheard"] for r in asks)
        check("repeat request answered with the unheard part", right == len(asks) and asks,
              f"{right}/{len(asks)}")
        for r in narr:
            print(f"     cut [{r['kind']}] {r['reply'][:150]!r}")
    errs = [r for r in rows if "error" in r]
    if errs:
        print(f"\n{len(errs)} call(s) failed, e.g. {errs[0]['error']}")
        ok = False
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
