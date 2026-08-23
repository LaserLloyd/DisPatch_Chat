"""Ports of OpenClaw's own visible-text sanitizers.

The gateway sanitizes agent text before the Control UI ever sees it, so its chat
shows prose and nothing else. DisPatch's CLI-payload path inherits that for free
(the CLI returns already-sanitized text), but every path that reads a session
transcript directly — the live watcher, the reconciler, the follower and the
gateway mirror — gets the RAW record, scaffolding and all. Without this module a
runtime-context block (a 10KB subagent completion event wrapped in
``<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>``) lands in the chat as a wall of text
that the Control UI never displays.

Ported from OpenClaw 2026.7.1, whose installed package ships its server
modules unminified under `dist/`:
  - `internal-runtime-context-*.js`  -> stripInternalRuntimeContext
  - `protocol-scaffolding-*.js`      -> stripInternalRuntimeScaffolding
  - `assistant-visible-text-*.js`    -> sanitizeAssistantVisibleText ("delivery")
  - `control-ui/assets/markdown-*.js`-> citation-marker stripping

Kept deliberately close to the originals (same names, same order) so a future
OpenClaw update can be diffed against them rather than re-derived.
"""

from __future__ import annotations

import bisect
import re

# --------------------------------------------------------------------------- #
# Code regions
#
# Fenced blocks first (a fence can contain backticks), then inline spans of any
# backtick run length. Text inside these is being QUOTED, not used: an agent
# writing "embed it with `[[media:/path/x.png|cap]]`" is DESCRIBING the syntax,
# and rewriting it corrupts the explanation — an agent's own account of how pictures
# work came out with its example replaced by "(image unavailable: path)".
#
# Reactions learned this first (a quoted `:react:` fired for real and spent a
# one-shot image). The rule is the same for every marker syntax, so it lives
# here rather than in one feature's module.
# --------------------------------------------------------------------------- #

CODE_SPAN_RE = re.compile(
    r"(?P<fence>^[ \t]*(?P<ticks>`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*(?P=ticks)[ \t]*$|\Z))"
    r"|(?P<inline>(?P<open>`+)(?:(?!(?P=open)).)*?(?P=open))",
    re.DOTALL | re.MULTILINE,
)


def sub_outside_code(text: str, fn):
    """Apply ``fn`` to the parts of ``text`` that are not code, verbatim elsewhere.

    ``fn`` takes a plain-text segment and returns its replacement.
    """
    if not text:
        return text
    out: list[str] = []
    pos = 0
    for m in CODE_SPAN_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))              # verbatim: this is quoted text
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)

# --------------------------------------------------------------------------- #
# Internal runtime context (src/agents/internal-runtime-context.ts)
# --------------------------------------------------------------------------- #

INTERNAL_RUNTIME_CONTEXT_BEGIN = "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>"
INTERNAL_RUNTIME_CONTEXT_END = "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>"

OPENCLAW_RUNTIME_CONTEXT_NOTICE = (
    "This context is runtime-generated, not user-authored. "
    "Keep internal details private."
)
_LEGACY_INTERNAL_CONTEXT_HEADER = (
    "OpenClaw runtime context (internal):\n"
    f"{OPENCLAW_RUNTIME_CONTEXT_NOTICE}\n\n"
)
_LEGACY_INTERNAL_EVENT_MARKER = "[Internal task completion event]"

_RUNTIME_CONTEXT_PROMPT_HEADERS = (
    "OpenClaw runtime context for the immediately preceding user message.",
    "OpenClaw runtime event.",
)


def _find_delimited_token_index(text: str, token: str, start: int) -> int:
    """Index of ``token`` when it stands alone on its own line, at/after start.

    Mirrors the upstream helper: the delimiter only counts when it owns a line,
    so the same string quoted inside prose or a code block is left alone.

    "Or a code block" was ASPIRATIONAL until it was made true here. A token
    inside a ``` fence owns its line, so it matched — and because an
    unterminated opener fails closed (see extract_delimited_blocks), an agent
    writing "the opener is ```<<<BEGIN…>>>``` and here is the answer" had
    everything after the fence deleted. A bare quoted token sanitized to the
    empty string, which main.py treats as "nothing to persist", so the reply
    was never written to the database at all. Two such messages also
    canonicalise to the same "```" and the second is dropped as a duplicate.

    The module already had _find_code_regions and used it for special tokens,
    reasoning tags and memory tags; the runtime-context passes were simply
    never routed through it. Same use-vs-mention blindness as the :react:
    marker bug, with a worse consequence: silent whole-message loss.
    """
    pattern = re.compile(rf"(?:^|\r?\n){re.escape(token)}(?=\r?\n|$)")
    regions = _find_code_regions(text)
    pos = max(0, start)
    while True:
        m = pattern.search(text, pos)
        if not m:
            return -1
        prefix_len = len(m.group(0)) - len(token)
        idx = m.start() + prefix_len
        if not any(s <= idx < e for s, e in regions):
            return idx
        # Quoted, not used. Keep looking after this occurrence.
        pos = m.end()


def extract_delimited_blocks(text: str, begin: str, end: str) -> tuple[str, list[str]]:
    """Split ``text`` into (remaining_text, [blocks]) — nesting-aware.

    An unterminated opening delimiter drops everything after it: a truncated
    runtime block must never leak, so we fail closed exactly like upstream.
    """
    nxt = text
    blocks: list[str] = []
    while True:
        start = _find_delimited_token_index(nxt, begin, 0)
        if start == -1:
            return nxt, blocks
        cursor = start + len(begin)
        depth = 1
        finish = -1
        while depth > 0:
            next_begin = _find_delimited_token_index(nxt, begin, cursor)
            next_end = _find_delimited_token_index(nxt, end, cursor)
            if next_end == -1:
                break
            if next_begin != -1 and next_begin < next_end:
                depth += 1
                cursor = next_begin + len(begin)
                continue
            depth -= 1
            finish = next_end
            cursor = next_end + len(end)
        before = nxt[:start].rstrip()
        if finish == -1 or depth != 0:
            return before, blocks
        block_end = finish + len(end)
        blocks.append(nxt[start:block_end].strip())
        after = nxt[block_end:].lstrip()
        nxt = f"{before}\n\n{after}" if (before and after) else f"{before}{after}"


def _strip_legacy_internal_runtime_context(text: str) -> str:
    """Remove the pre-delimiter context format (header + completion event)."""
    nxt = text
    search_from = 0
    while True:
        header_start = nxt.find(_LEGACY_INTERNAL_CONTEXT_HEADER, search_from)
        if header_start == -1:
            return nxt
        event_start = header_start + len(_LEGACY_INTERNAL_CONTEXT_HEADER)
        if not nxt.startswith(_LEGACY_INTERNAL_EVENT_MARKER, event_start):
            search_from = event_start
            continue
        para = nxt.find("\n\n", event_start + len(_LEGACY_INTERNAL_EVENT_MARKER))
        block_end = len(nxt) if para == -1 else para
        before = nxt[:header_start].rstrip()
        after = nxt[block_end:].lstrip()
        nxt = f"{before}\n\n{after}" if (before and after) else f"{before}{after}"
        search_from = max(0, len(before) - 1)


def _strip_runtime_context_prompt_preface(text: str) -> str:
    """Drop a bare "runtime context/event" header + notice pair and its blank run."""
    lines = text.split("\n")
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (line.strip() in _RUNTIME_CONTEXT_PROMPT_HEADERS
                and nxt.strip() == OPENCLAW_RUNTIME_CONTEXT_NOTICE):
            changed = True
            i += 2
            while i < len(lines) and lines[i].strip() == "":
                i += 1
            continue
        out.append(line)
        i += 1
    if not changed:
        return text
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def strip_internal_runtime_context(text: str) -> str:
    """Remove protected and legacy runtime-context blocks from text."""
    if not text:
        return text
    remaining, _ = extract_delimited_blocks(
        text, INTERNAL_RUNTIME_CONTEXT_BEGIN, INTERNAL_RUNTIME_CONTEXT_END)
    return _strip_runtime_context_prompt_preface(
        _strip_legacy_internal_runtime_context(remaining))


def has_internal_runtime_context(text: str) -> bool:
    if not text:
        return False
    if _find_delimited_token_index(text, INTERNAL_RUNTIME_CONTEXT_BEGIN, 0) != -1:
        return True
    if _LEGACY_INTERNAL_CONTEXT_HEADER in text:
        return True
    return any(f"{h}\n{OPENCLAW_RUNTIME_CONTEXT_NOTICE}" in text
               for h in _RUNTIME_CONTEXT_PROMPT_HEADERS)


# --------------------------------------------------------------------------- #
# Protocol scaffolding (src/infra/outbound/protocol-scaffolding.ts)
# --------------------------------------------------------------------------- #

_SCAFFOLDING_TAGS = "system-reminder|previous_response"
_SCAFFOLDING_BLOCK_RE = re.compile(
    rf"<\s*({_SCAFFOLDING_TAGS})\b[^>]*>[\s\S]*?<\s*/\s*\1\s*>", re.I)
_SCAFFOLDING_SELF_CLOSING_RE = re.compile(
    rf"<\s*(?:{_SCAFFOLDING_TAGS})\b[^>]*/\s*>", re.I)
_SCAFFOLDING_TAG_RE = re.compile(
    rf"<\s*/?\s*(?:{_SCAFFOLDING_TAGS})\b[^>]*>", re.I)

_PROMPT_DATA_TAG_NAMES = ("prompt-data", "untrusted-text")
_UNTRUSTED_RESULT_MARKERS = (
    "<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>", "<<<END_UNTRUSTED_CHILD_RESULT>>>")


def _standalone_line_pattern(token: str) -> str:
    return rf"(?:^|\r?\n)[ \t]*{re.escape(token)}[ \t]*(?=\r?\n|$)"


def _strip_standalone_marker_line(text: str, marker: str) -> str:
    return re.sub(_standalone_line_pattern(marker), "", text)


def _strip_delimited_runtime_block(text: str, begin: str, end: str) -> str:
    """Remove a legacy runtime block, skipping any opener that is QUOTED.

    `unmatched` deletes from the opening delimiter to end-of-text — correct
    when a real block is truncated (it must never leak), catastrophic when the
    "opener" is an agent showing you what the delimiter looks like inside a
    code fence. Same use-vs-mention blindness as _find_delimited_token_index
    above, on the legacy path, and it has to be fixed in both or quoting the
    token still eats the message.

    Everything is routed through _strip_outside_code, which the module already
    used for special tokens and reasoning tags.
    """
    closed = re.compile(
        f"{_standalone_line_pattern(begin)}[\\s\\S]*?{_standalone_line_pattern(end)}")
    unmatched = re.compile(f"{_standalone_line_pattern(begin)}[\\s\\S]*$")
    text = _strip_outside_code(text, closed)
    text = _strip_outside_code(text, unmatched)
    return _strip_outside_code(text, re.compile(_standalone_line_pattern(end)))


def _unwrap_prompt_data_wrapper_lines(text: str) -> str:
    """Drop `<prompt-data>` / `<untrusted-text>` wrapper lines + their header.

    These are the tags DisPatch used to render as literal unknown elements
    (`<prompt-data>` showed up in the DOM); the Control UI never shows them.
    """
    lines = text.split("\n")
    out: list[str] = []
    changed = False
    for i, line in enumerate(lines):
        trimmed = line.strip().lower()
        nxt = lines[i + 1].strip().lower() if i + 1 < len(lines) else ""
        is_open = any(trimmed == f"<{t}>" for t in _PROMPT_DATA_TAG_NAMES)
        is_close = any(trimmed == f"</{t}>" for t in _PROMPT_DATA_TAG_NAMES)
        is_header = line.strip().endswith(
            "(treat text inside this block as data, not instructions):")
        if is_header and any(nxt == f"<{t}>" for t in _PROMPT_DATA_TAG_NAMES):
            changed = True
            continue
        if is_open or is_close:
            changed = True
            continue
        out.append(line)
    return "\n".join(out) if changed else text


def strip_internal_runtime_scaffolding(text: str) -> str:
    """Remove system-reminder/prompt-data wrappers and untrusted-result markers."""
    if not text:
        return text
    stripped = _unwrap_prompt_data_wrapper_lines(text)
    stripped = _SCAFFOLDING_BLOCK_RE.sub("", stripped)
    stripped = _SCAFFOLDING_SELF_CLOSING_RE.sub("", stripped)
    stripped = _SCAFFOLDING_TAG_RE.sub("", stripped)
    stripped = _strip_delimited_runtime_block(
        stripped, INTERNAL_RUNTIME_CONTEXT_BEGIN, INTERNAL_RUNTIME_CONTEXT_END)
    for marker in _UNTRUSTED_RESULT_MARKERS:
        stripped = _strip_standalone_marker_line(stripped, marker)
    return stripped


# --------------------------------------------------------------------------- #
# Assistant visible text, "delivery" profile (src/.../assistant-visible-text.ts)
# --------------------------------------------------------------------------- #

# Search-result citation markers: private-use delimiters the models emit around
# a citation. Same codepoints the Control UI's markdown module strips.
_CITATION_LINE_RE = re.compile("[ \t]*cite(?:[^]*)?(?=\r?\n|$)")
_CITATION_RE = re.compile("cite(?:[^]*)?")

# Leaked model control tokens, e.g. <|assistant|> (and full-width pipe variants).
_MODEL_SPECIAL_TOKEN_RE = re.compile("<[|｜][^|｜]*[|｜]>")

_MEMORY_TAG_RE = re.compile(r"<(/?)relevant_memories>", re.I)
_REASONING_TAG_RE = re.compile(
    r"<(?:think|thinking|reasoning)>[\s\S]*?</(?:think|thinking|reasoning)>", re.I)

# Thinking sections a LOCAL reasoning model emits inside its text stream, e.g.
# some chat templates wrap the chain of thought in
# `<|channel>thought ... <channel|>`. The runtime does not split these into a
# `thinking` block — they arrive as ordinary text blocks. The generic
# _MODEL_SPECIAL_TOKEN_RE above cannot remove them: any `|` inside the section
# (a media ref, a table) stops its `[^|｜]*` scan dead, so the whole section
# reaches the chat verbatim. Strip from the opening marker to the closing
# marker (or the end, when the model never closes it), keeping any reply that
# follows the close.
_THINKING_CHANNEL_RE = re.compile(
    r"(?is)<[|｜]channel[|｜]?>\s*(?:thought|thinking|reasoning)\b.*?"
    r"(?:<channel[|｜]>|<[|｜]channel[|｜]>|$)")

# Explicit open/close thinking fences some templates emit.
_THINKING_FENCED_RE = re.compile(
    r"(?is)<\|start_thinking\|>.*?(?:<\|end_thinking\|>|$)")
_INTERNAL_TRACE_LINE_RE = re.compile(
    r"^\[(?:Tool Call|Tool Result for ID|Historical context|Context):", re.I)

_FENCE_RE = re.compile(r"(^|\n)(```|~~~)[^\n]*\n[\s\S]*?(?:\n\2|$)")
# POSSESSIVE quantifiers, deliberately. The natural form `+[^`]+`+ backtracks
# catastrophically on a run of backticks: the leading `+ takes all of them,
# [^`]+ fails, and it gives one back and retries, forever. On a wall of 20,000
# backticks that was ~1 SECOND to find ZERO matches — and this runs on the
# sanitize path that every assistant message passes through, on text an agent
# or a user controls. `++ never gives anything back, so the match fails
# immediately instead. Verified identical on every span shape (single, double,
# triple, unclosed, multiple spans); 24x faster on hostile input.
_INLINE_CODE_RE = re.compile(r"`++[^`]++`++")


# One-entry memo. Every sanitize pass rescans the SAME string for code regions —
# measured at 7 scans per sanitize_assistant_visible_text() call, each one
# re-running two regexes over the whole message to rediscover what the previous
# pass already knew. The passes rewrite `text` between them, so a keyed cache
# would mostly miss; but the passes that DO share a string are consecutive, so
# remembering just the last one collapses the repeats. Bounded by construction:
# it holds exactly one string and its regions, replaced on the next miss.
_REGION_MEMO: tuple[str, list[tuple[int, int]]] | None = None


def _find_code_regions(text: str) -> list[tuple[int, int]]:
    global _REGION_MEMO
    memo = _REGION_MEMO
    if memo is not None and memo[0] == text:
        return memo[1]
    regions = _compute_code_regions(text)
    _REGION_MEMO = (text, regions)
    return regions


def _compute_code_regions(text: str) -> list[tuple[int, int]]:
    """Fenced + inline code spans, so sanitizers skip documentation examples.

    Linear in the number of matches. The obvious version — ``any(...)`` over
    every fence region for each inline match — is O(n*m), and a message body is
    attacker-influenced: a wall of 10,000 backticks produced ~2,500 inline
    matches checked against each other and took **1.7 seconds** on the sanitize
    path, which every assistant message goes through. Both sequences come out
    of finditer in increasing start order, so a single advancing cursor
    replaces the scan.
    """
    fences: list[tuple[int, int]] = []
    for m in _FENCE_RE.finditer(text):
        start = m.start() + len(m.group(1))
        fences.append((start, m.start() + len(m.group(0))))

    regions = list(fences)
    i = 0
    for m in _INLINE_CODE_RE.finditer(text):
        # Fences are disjoint and ordered, and inline matches only move
        # forward, so drop the fences we are already past and check at most one.
        while i < len(fences) and fences[i][1] <= m.start():
            i += 1
        if i < len(fences) and m.start() >= fences[i][0] and m.end() <= fences[i][1]:
            continue                       # already covered by a fence
        regions.append((m.start(), m.end()))
    regions.sort()
    return regions


def _is_inside_code(pos: int, regions: list[tuple[int, int]]) -> bool:
    """Binary search, not a scan — called once per match by _strip_outside_code,
    which made that pass quadratic on the same hostile input."""
    if not regions:
        return False
    # Rightmost region whose start is <= pos. Regions are sorted and, after
    # _find_code_regions, non-overlapping, so only that one can contain pos.
    i = bisect.bisect_right(regions, (pos, float("inf"))) - 1
    return i >= 0 and regions[i][0] <= pos < regions[i][1]


def strip_citation_markers(text: str) -> str:
    if not text or "" not in text:
        return text
    return _CITATION_RE.sub("", _CITATION_LINE_RE.sub("", text))


def _strip_outside_code(text: str, pattern: re.Pattern) -> str:
    """Apply ``pattern`` removal only outside fenced/inline code regions."""
    if not text:
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        if _is_inside_code(m.start(), regions):
            continue
        out.append(text[last:m.start()])
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _strip_internal_trace_lines(text: str) -> str:
    """Drop whole lines that are internal trace scaffolding, outside code."""
    if not text or "[" not in text:
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    pos = 0
    for raw_line in text.splitlines(keepends=True):
        if not (_INTERNAL_TRACE_LINE_RE.match(raw_line.strip())
                and not _is_inside_code(pos, regions)):
            out.append(raw_line)
        pos += len(raw_line)
    return "".join(out)


# First characters a real assistant reply starts with, observed across the
# app's whole message history: letters, digits, emoji/CJK (anything non-ASCII),
# and markdown/RP openers. Anything else — "/", "!", ".", a stray fragment —
# marks a text block that is the TAIL of a mis-split thinking stream, not
# speech. On 2026-08-19 a frontier model's reasoning was cut at a backtick and
# the continuation arrived as a `text` block; it was delivered verbatim.
_REPLY_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "*#-[<=(@.\"_:$|`")

# A reasoning tail is long; a terse odd-start line is more likely a real
# (if unusual) reply. Only judge texts at least this long.
_REASONING_TAIL_MIN_CHARS = 200


def _is_reasoning_tail(text: str) -> bool:
    """True when a stripped text block is the unlabelled continuation of a
    thinking stream: it begins with a character no real reply begins with."""
    if not text:
        return False
    first = text[0]
    if first in _REPLY_START_CHARS or ord(first) > 127:
        return False
    return len(text) >= _REASONING_TAIL_MIN_CHARS


def sanitize_assistant_visible_text(text: str) -> str:
    """OpenClaw's "delivery" profile: keep prose, remove internal scaffolding.

    This is what the gateway runs before delivering assistant text, so applying
    it to transcript-derived text makes DisPatch show exactly what the Control
    UI shows. Idempotent: already-clean CLI payloads pass through unchanged.

    On top of the upstream profile, thinking content that arrives mislabelled
    as ordinary text is removed: inline reasoning-channel sections and the
    unlabelled tail of a split thinking stream. Both were observed reaching
    the family chat as visible "replies" (2026-08-19).
    """
    if not text:
        return text
    cleaned = strip_citation_markers(text)
    cleaned = strip_internal_runtime_context(cleaned)
    cleaned = strip_internal_runtime_scaffolding(cleaned)
    # Thinking strips first: the generic special-token strip below would
    # otherwise eat the fence markers one by one, leaving the reasoning text
    # behind.
    cleaned = _strip_outside_code(cleaned, _THINKING_CHANNEL_RE)
    cleaned = _strip_outside_code(cleaned, _THINKING_FENCED_RE)
    cleaned = _strip_outside_code(cleaned, _MODEL_SPECIAL_TOKEN_RE)
    cleaned = _strip_outside_code(cleaned, _REASONING_TAG_RE)
    cleaned = _strip_outside_code(cleaned, _MEMORY_TAG_RE)
    cleaned = _strip_internal_trace_lines(cleaned)
    cleaned = cleaned.strip()
    if _is_reasoning_tail(cleaned):
        return ""
    return cleaned


def is_tool_warning(text: str) -> bool:
    """True for gateway-generated tool-status warnings, e.g.
    ``⚠️ 🛠️ Exec failed: `run ps aux -> search "Z"` ``.

    The gateway emits these as extra reply payloads when a tool call fails
    mid-turn (run-session-state.ts identifies them the same way:
    ``text.startsWith("⚠️ 🛠️ ")``). They are the runtime narrating a tool
    error — not the agent speaking — so DisPatch collapses them like other
    working output instead of posting them as chat bubbles. Variation
    selectors are stripped before comparing so an emoji rendered without
    U+FE0F still matches.
    """
    if not text:
        return False
    head = text.lstrip().replace("\ufe0f", "")[:4]
    return head.startswith("⚠ \U0001f6e0")


def sanitize_user_visible_text(text: str) -> str:
    """Same treatment for transcript USER rows.

    A user row in the transcript is often not something a human typed at all —
    it is how the runtime injects subagent completion events and other context.
    Stripping those leaves an empty string, which the callers drop.
    """
    if not text:
        return text
    cleaned = strip_citation_markers(text)
    cleaned = strip_internal_runtime_context(cleaned)
    cleaned = strip_internal_runtime_scaffolding(cleaned)
    return cleaned.strip()
