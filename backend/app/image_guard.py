"""Hard-line deny check for image prompts.

This is NOT a content filter. DisPatch is unrestricted by default; image
generation has exactly two hard lines, and this module enforces only those:

1. Depictions are adult-only: nothing sexual involving anyone under 18.
2. No sexual imagery of a real, identifiable person.

A prompt is refused only when a sexual term appears together with (1) an
unambiguous minor term, an age under 18, or an ambiguous youth term with no
adult age stated; or with (2) a real-person marker (a named person, a
celebrity, "my ex", deepfake / face-swap wording). Anything else passes:
violence, horror, adult nudity of original characters, fiction of every kind.

It is a text heuristic on the prompt, so it errs toward letting prose through
and is one layer of the policy (provider-side moderation is another). It never
logs prompt text.

Entry points: :func:`check_prompt` (raises :class:`ImageGuardError`) and
:func:`violation` (returns the error or ``None``). ``ImageSpec`` calls the
former on construction, so the inline ``[[pic:]]`` marker, the
``/api/image-jobs`` route, queued-job rereads and the Image Studio all share
one rule. ``ImageGuardError`` subclasses ``ValueError`` so every existing
``except ValueError`` around spec construction turns it into the right
user-facing refusal.
"""
from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger("dispatch.image_guard")

CODE_MINOR = "image_minor_sexual"
CODE_REAL_PERSON = "image_real_person_sexual"

MSG_MINOR = ("That picture can't be made: images show adults only, and nothing "
             "sexual can involve anyone under 18. Rewrite the request with "
             "adult characters.")
MSG_REAL_PERSON = ("That picture can't be made: sexual images of real, "
                   "identifiable people aren't allowed. Remove the real "
                   "person's name or likeness, or describe an original "
                   "character instead.")


class ImageGuardError(ValueError):
    """A prompt crossed one of the two hard lines. ``str(e)`` is user-facing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- normalisation ---------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH)
    return t.replace("_", " ")


# Negated phrases ("no nudity", "not nsfw", "sfw") are dropped before matching
# so a request that asks for the opposite is not refused.
_NEGATED = re.compile(
    r"\b(?:no|not|non|without|never|avoid|zero)[\s-]+"
    r"(?:nsfw|nud\w*|naked|sexual\w*|sex|erotic\w*|explicit)\b|\bsfw\b", re.I)

# --- sexual terms ----------------------------------------------------------

_SEXUAL = re.compile(
    r"\b(?:nsfw|nude|nudes|nudity|naked|topless|bottomless|undressed|unclothed"
    r"|sex|sexual\w*|sexy|sexed|erotic\w*|porn\w*|hentai|lewd|fetish\w*"
    r"|genital\w*|vagina\w*|penis\w*|nipples?|lingerie|seductive\w*"
    r"|sexually\s+explicit|explicit\s+(?:content|scene|image)s?"
    r"|having\s+sex|orgasm\w*|masturbat\w*|blowjob|cum(?:shot)?|bdsm)\b", re.I)
# "nude" as a colour/make-up word is not nudity.
_NUDE_COLOUR = re.compile(
    r"\bnude[\s-]+(?:colou?r\w*|lip\w*|tone\w*|shade\w*|make-?up|polish|nails?"
    r"|heels?|pumps?|palette|stockings?|tights|eyeshadow)\b", re.I)

# --- minors ----------------------------------------------------------------

_MINOR_CLEAR = re.compile(
    r"\b(?:child|children|childlike|child-like|kid|kids|toddlers?|infants?"
    r"|newborns?|minors?|under-?age\w*|pre-?teens?|tweens?|juveniles?"
    r"|(?:little|young|small)\s+(?:girl|boy)s?|lolis?|lolicon\w*|shota\w*"
    r"|loli-?\w*|pre-?pubescent|prepubescent|under\s*18|underaged)\b", re.I)
_BABY = re.compile(
    r"\bbab(?:y|ies)\b(?!\s+(?:oil|powder|blue|pink|doll|shark|yoda|bump"
    r"|carrots?|spinach|face|bird|goat|steps))", re.I)
# Words that may be adults (cosplay, uniforms): minor only without a stated
# adult age.
_MINOR_AMBIGUOUS = re.compile(
    r"\b(?:teens?|teenage\w*|adolescen\w*|school\s*girls?|school\s*boys?"
    r"|high\s+school\w*|middle\s+school\w*|junior\s+high|elementary)\b", re.I)
_AGE = re.compile(
    r"\b(\d{1,2})[\s-]*(?:years?|yrs?|y)[\s-]*(?:old|o)\b"
    r"|\bage[ds]?\s*(?:of\s*)?(\d{1,2})\b|\b(\d{1,2})\s*(?:yo|y/o)\b", re.I)
_ADULT_MARK = re.compile(r"\b(?:adults?|grown[\s-]?up|18\s*\+|over\s*18|"
                         r"of\s+legal\s+age)\b", re.I)


def _ages(t: str) -> list[int]:
    out = []
    for m in _AGE.finditer(t):
        g = next((x for x in m.groups() if x), None)
        if g and g.isdigit():
            out.append(int(g))
    return out


def _has_minor(t: str) -> bool:
    ages = _ages(t)
    if any(a < 18 for a in ages):
        return True
    if _MINOR_CLEAR.search(t) or _BABY.search(t):
        return True
    if _MINOR_AMBIGUOUS.search(t):
        adult = any(a >= 18 for a in ages) or _ADULT_MARK.search(t)
        return not adult
    return False


# --- real, identifiable people --------------------------------------------

_REAL_MARKERS = re.compile(
    r"\b(?:deep-?fakes?|face[\s-]?swap\w*|nudif\w*|undress(?:ed|ing)?\s+"
    r"(?:her|him|them|this|the|my)\b|remove\s+(?:her|his|their)\s+cloth\w*"
    r"|celebrit\w*|real\s+(?:person|people|woman|women|man|men|life)"
    r"|(?:famous|real)\s+(?:actress|actor|singer|model|politician|streamer"
    r"|youtuber|athlete|influencer)\w*"
    r"|lookalike|look-?alike|likeness\s+of|photos?\s+of\s+my"
    r"|(?:my|your|our)\s+(?:ex|exes|wife|husband|girlfriend|boyfriend|neighbou?r"
    r"|co-?worker|colleague|boss|teacher|classmate|sister|brother|mom|mum|mother"
    r"|dad|father|aunt|uncle|cousin|friend|roommate|landlord)s?)\b", re.I)

# A well-known first name followed by a capitalised surname. Matched on the
# ORIGINAL casing so "taylor swift" in lower case still needs the marker rules.
_FIRST_NAMES = (
    "Aaron Adam Adele Alexandra Alicia Amanda Amber Amy Andrew Angelina Anna "
    "Anne Ariana Ashley Barack Beyonce Billie Brad Brittany Britney Bruce "
    "Camila Cardi Charlize Chris Christina Cristiano Daisy Dakota Dave David "
    "Demi Donald Dua Elon Elizabeth Ellen Emily Emma Eminem Ethan Gal Gigi "
    "Hailey Halle Harrison Hillary Hugh Jack Jackie James Jamie Jason "
    "Jeff Jennifer Jessica Jim Joe John Johnny Jordan Julia Justin Kamala "
    "Kanye Kate Katy Keanu Kendall Kevin Kim Kourtney Kylie Lady Lana Leonardo "
    "Lionel Lisa Lizzo Madison Margot Mark Matt Megan Meghan Mila Miley Natalie "
    "Nicki Nicole Olivia Oprah Paris Patrick Paul Peter Rihanna Robert Ryan "
    "Sandra Sarah Scarlett Selena Serena Shakira Sofia Sophie Sydney Taylor "
    "Tom Tyler Vladimir Will Zendaya Zoe Zac Zayn").split()
_NAME = re.compile(
    r"\b(?:" + "|".join(_FIRST_NAMES) + r")\s+(?:[A-Z][a-z]{2,}|[A-Z]{3,})\b")
# "of Firstname Lastname" / "photo of Name" for names outside the list is not
# attempted: unlisted names pass, which keeps false positives down.


def _has_real_person(raw: str, t: str) -> bool:
    return bool(_REAL_MARKERS.search(t) or _NAME.search(raw))


# --- public API ------------------------------------------------------------

def violation(prompt: str) -> ImageGuardError | None:
    """The hard line ``prompt`` crosses, or ``None``. Never raises."""
    if not prompt:
        return None
    raw = unicodedata.normalize("NFKC", prompt).translate(_ZERO_WIDTH)
    t = _NEGATED.sub(" ", _norm(prompt))
    t = _NUDE_COLOUR.sub(" ", t)
    if not _SEXUAL.search(t):
        # Wording that is itself the sexualising act on a real person.
        if re.search(r"\b(?:nudif\w*|undress(?:ed|ing)?\s+(?:her|him|them|this"
                     r"|the|my)\b|remove\s+(?:her|his|their)\s+cloth\w*)", t, re.I):
            return ImageGuardError(CODE_REAL_PERSON, MSG_REAL_PERSON)
        return None
    if _has_minor(t):
        return ImageGuardError(CODE_MINOR, MSG_MINOR)
    if _has_real_person(raw, t):
        return ImageGuardError(CODE_REAL_PERSON, MSG_REAL_PERSON)
    return None


def check_prompt(prompt: str) -> None:
    """Raise :class:`ImageGuardError` if ``prompt`` crosses a hard line."""
    err = violation(prompt)
    if err is not None:
        log.info("image prompt refused (%s)", err.code)   # never the text
        raise err
