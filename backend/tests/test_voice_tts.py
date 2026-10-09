"""Drive mode: what gets spoken, and when a sentence is ready to speak."""
from __future__ import annotations

import asyncio

import numpy as np

from app.voice.tts import SentenceSplitter, speak, speakable, to_pcm16


def stream(text: str, step: int = 3) -> list[str]:
    """Feed `text` a few characters at a time, like a token stream."""
    sp = SentenceSplitter(min_words=1)
    out: list[str] = []
    for i in range(0, len(text), step):
        out += sp.push(text[i:i + step])
    return out + sp.flush()


# ---- speakable ----------------------------------------------------------- #

def test_plain_text_passes_through():
    assert speakable("Turn left in two hundred metres.") == "Turn left in two hundred metres."


def test_code_block_becomes_a_pointer_to_the_screen():
    s = speakable("Here you go:\n```python\nprint('x')\n```\nDone.")
    assert "print" not in s and "The code is on the screen." in s and s.endswith("Done.")


def test_markdown_links_urls_and_emphasis():
    s = speakable("See **the docs** at [the guide](https://example.com/x) or https://example.org.")
    assert "https" not in s and "*" not in s and "the guide" in s


def test_directives_reactions_and_emoji_are_dropped():
    s = speakable("Nice! :react:happy: 🎉 [[media:/tmp/x.png|cap]] ![a](/media/1)")
    assert s == "Nice!"


def test_all_emoji_reply_says_nothing():
    assert speakable("👍🎉") == ""
    assert speakable("") == ""


def test_lists_and_headings_are_flattened():
    s = speakable("## Plan\n- milk\n- eggs\n1. call mum")
    assert s == "Plan milk eggs call mum"


# ---- SentenceSplitter ------------------------------------------------------ #

def test_sentences_come_out_as_soon_as_complete():
    sp = SentenceSplitter(min_words=1)
    assert sp.push("Sure. I can") == ["Sure."]
    assert sp.push(" do that! Next") == ["I can do that!"]
    assert sp.flush() == ["Next"]


def test_streamed_and_whole_text_split_identically():
    text = "First one. Second one? Third one! And a tail"
    assert stream(text, 1) == ["First one.", "Second one?", "Third one!", "And a tail"]
    assert stream(text, 5) == ["First one.", "Second one?", "Third one!", "And a tail"]


def test_abbreviations_and_initials_do_not_end_a_sentence():
    assert stream("Ask Dr. Smith about it e.g. tomorrow. Then rest.") == [
        "Ask Dr. Smith about it e.g. tomorrow.", "Then rest."]
    assert stream("J. R. R. Tolkien wrote it. Yes.") == ["J. R. R. Tolkien wrote it.", "Yes."]


def test_decimals_stay_whole():
    assert stream("It costs 3.50 today. Okay.") == ["It costs 3.50 today.", "Okay."]


def test_open_code_fence_is_held_back():
    sp = SentenceSplitter(min_words=1)
    assert sp.push("Run this. ```\nx = 1. y = 2. ") == ["Run this."]
    # Nothing inside the open fence is released...
    assert sp.push("z = 3.\n") == []
    # ...until it closes.
    assert sp.push("```\nThat is all. ") == ["```\nx = 1. y = 2. z = 3.\n```\nThat is all."]


def test_open_directive_is_held_back():
    sp = SentenceSplitter(min_words=1)
    assert sp.push("Look. [[media:/a. b") == ["Look."]
    assert sp.push(".png|x]] Here it is. ") == ["[[media:/a. b.png|x]] Here it is."]


def test_long_first_clause_is_cut_at_a_comma_for_latency():
    text = ("Well, I looked at the calendar for the whole week and the traffic report too, "
            "and everything is fine")
    sp = SentenceSplitter(first_soft_chars=80)
    first = sp.push(text)
    assert first and first[0].endswith(",") and len(first[0]) <= 80


def test_paragraph_and_list_breaks_end_sentences():
    assert stream("Shopping list\n\nmilk\n- eggs\n- bread") == [
        "Shopping list", "milk", "- eggs", "- bread"]


def test_tiny_fragments_wait_for_the_next_sentence():
    # Prosody: "Sure." alone sounds clipped; it rides with the next sentence.
    sp = SentenceSplitter()            # min_words=4
    assert sp.push("Sure. ") == []
    assert sp.push("I can do that today. Ok") == ["Sure. I can do that today."]
    assert sp.flush() == ["Ok"]


def test_a_short_whole_reply_still_comes_out_on_flush():
    sp = SentenceSplitter()
    assert sp.push("Yes.") == [] and sp.flush() == ["Yes."]


def test_flush_on_empty_is_empty():
    assert SentenceSplitter().flush() == []


# ---- engine plumbing ----------------------------------------------------- #

class FakeEngine:
    sample_rate = 24000
    sample_format = "pcm_s16le"
    channels = 1
    streaming = True

    def __init__(self):
        self.said = []

    def stream(self, text, profile_id=None, stop=None):
        self.said.append((text, profile_id))
        yield np.zeros(120, "<i2")
        yield np.zeros(120, "<i2")


def test_speak_seam_streams_frames_in_the_profiles_voice():
    eng = FakeEngine()

    async def go():
        async def chunks():
            for c in ["Hello there. How", " are you? 👍", "```x```"]:
                yield c
        return [f async for f in speak(chunks(), "host-voice", engine=eng)]

    frames = asyncio.run(go())
    assert [t for t, _ in eng.said] == ["Hello there. How are you?", "The code is on the screen."]
    assert {p for _, p in eng.said} == {"host-voice"}
    assert len(frames) == 4 and all(isinstance(f, bytes) and len(f) == 240 for f in frames)


def test_to_pcm16_clips():
    a = to_pcm16(np.array([2.0, -2.0, 0.0], np.float32))
    assert a.tolist() == [32767, -32767, 0]
