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
  - `payload-*.js`                   -> stripPlainTextToolCallBlocks
                                        (see openclaw_tool_calls.py)

Kept deliberately close to the originals (same names, same order) so a future
OpenClaw update can be diffed against them rather than re-derived.

A PORT ROTS. The gateway kept moving and this module did not, and because every
stage here fails OPEN — an unmatched pattern removes nothing and says nothing —
the drift showed up as internal text arriving in the family chat rather than as
an error. GATEWAY_DIST_ANCHORS at the bottom of this file is the tripwire: it
pins every hand-copied pattern to a literal fragment of the dist source, and the
test suite asserts each fragment still exists in the installed gateway.
"""

from __future__ import annotations

import bisect
import re
from typing import NamedTuple

from .openclaw_tool_calls import strip_plain_text_tool_call_blocks

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
    regions = _find_code_regions(text)
    out: list[str] = []
    changed = False
    pos = 0
    for i, line in enumerate(lines):
        # Offset of this line in the original string, so a wrapper tag QUOTED
        # inside a fence (an agent explaining what these tags look like) is
        # left alone like every other marker strip in this module.
        start = pos
        pos += len(line) + 1
        if _is_inside_code(start, regions):
            out.append(line)
            continue
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
    # Outside code only, like every other marker strip here. These three ran
    # over the raw string, so an agent documenting `<system-reminder>` inside a
    # fence had its example deleted out of the explanation — the same
    # use-vs-mention blindness the runtime-context passes had.
    stripped = _strip_outside_code(stripped, _SCAFFOLDING_BLOCK_RE)
    stripped = _strip_outside_code(stripped, _SCAFFOLDING_SELF_CLOSING_RE)
    stripped = _strip_outside_code(stripped, _SCAFFOLDING_TAG_RE)
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

# Memory blocks. The tags wrap RECALLED MEMORY TEXT, so the gateway strips the
# whole block, not the two tags — stripping only the tags publishes the memory
# and keeps the family reading an agent's private recall. DisPatch's port
# matched the bare literals `<relevant_memories>`; upstream matches either
# separator and tolerates attributes, so `<relevant-memories score="0.8">` (a
# shape the gateway itself emits) sailed straight through.
_MEMORY_TAG_RE = re.compile(r"<\s*(/?)\s*relevant[-_]memories\b[^<>]*>", re.I)
_MEMORY_TAG_QUICK_RE = re.compile(r"<\s*/?\s*relevant[-_]memories\b", re.I)

# Reasoning tags. Upstream covers `think`/`thinking`/`thought`/`antthinking`,
# the `antml:` and `mm:` namespace prefixes, and attributes on the tag; DisPatch
# matched three bare names with no attributes. `reasoning` is a DisPatch-local
# addition (observed from a local model) that upstream does not carry — keep it.
_REASONING_TAG_NAME = (
    r"(?:(?:antml:|mm:)?(?:think(?:ing)?|thought)|antthinking|reasoning)")
_REASONING_TAG_RE = re.compile(
    rf"<\s*{_REASONING_TAG_NAME}\b[^<>]*>[\s\S]*?"
    rf"<\s*/\s*{_REASONING_TAG_NAME}\b[^<>]*>", re.I)

# Minimax embeds tool calls as XML inside text blocks instead of emitting
# structured tool calls.
_MINIMAX_QUICK_RE = re.compile(r"minimax:tool_call", re.I)
_MINIMAX_TOOL_XML_RE = re.compile(
    r"<invoke\b[^>]*>[\s\S]*?</invoke>|</?minimax:tool_call>", re.I)

# Legacy `[TOOL_CALL] … [/TOOL_CALL]` blocks.
_LEGACY_BRACKET_QUICK_RE = re.compile(r"\[\s*/?\s*TOOL_(?:CALL|RESULT)\s*\]", re.I)
_LEGACY_BRACKET_OPEN_RE = re.compile(r"\[\s*TOOL_(CALL|RESULT)\s*\]", re.I)
_LEGACY_BRACKET_CLOSE_RE = {
    "CALL": re.compile(r"\[\s*/\s*TOOL_CALL\s*\]", re.I),
    "RESULT": re.compile(r"\[\s*/\s*TOOL_RESULT\s*\]", re.I),
}
_LEGACY_CALL_TOOL_RE = re.compile(
    r"\btool\s*=>\s*[\"'][A-Za-z_][A-Za-z0-9_.:-]{0,119}[\"']", re.I)
_LEGACY_CALL_ARGS_RE = re.compile(r"\bargs\s*=>", re.I)
_LEGACY_RESULT_JSON_RE = re.compile(r"^\s*[{\[]")
_LEGACY_RESULT_FAT_ARROW_RE = re.compile(
    r"\b(?:tool|result|output|content)\s*=>", re.I)
_LEGACY_RESULT_COLON_RE = re.compile(r"\b(?:tool|result|output|content)\s*:", re.I)

# Downgraded tool-call TEXT — how a tool call and its result survive a history
# replay across providers. The result marker carries NO COLON after "ID"
# (`[Tool Result for ID abc123]`), which is why DisPatch's `Tool Result for ID:`
# line rule never fired; and the leak is not the marker line but everything
# AFTER it, up to the next `[Tool ` marker, which the gateway removes with it.
_DOWNGRADED_QUICK_RE = re.compile(r"\[Tool (?:Call|Result)|\[Historical context", re.I)
_DOWNGRADED_TOOL_CALL_RE = re.compile(r"\[Tool Call:[^\]]*\]", re.I)
_DOWNGRADED_TOOL_RESULT_RE = re.compile(
    r"\[Tool Result for ID[^\]]*\]\n?[\s\S]*?(?=\n*\[Tool |\n*\Z)", re.I)
_DOWNGRADED_HISTORICAL_RE = re.compile(r"\[Historical context:[^\]]*\]\n?", re.I)

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
# Internal trace lines — the runtime narrating its own tool use. These are the
# EMOJI-led forms upstream removes; the `[Tool …]` bracket forms it handles in
# stripDowngradedToolCallText instead (see _strip_downgraded_tool_call_text).
_INTERNAL_TRACE_LINE_QUICK_RE = re.compile(
    r"(?:📊|🛠️|📖|📝|🔍|🔎|⚙️|tool[-_ ]?call|tool[-_ ]?result|function[-_ ]?call)", re.I)
_INTERNAL_TRACE_LINE_RE = re.compile(
    r"^(?:>\s*)?(?:⚠️\s*)?(?:📊|🛠️|📖|📝|🔍|🔎|⚙️)\s*"
    r"(?:Session Status|Exec|Read|Edit|Write|Patch|Search|Open|Click|Find"
    r"|Screenshot|Update Plan|Tool Call|Tool Result|Function Call|Shell"
    r"|Command)\s*:", re.I)
_INTERNAL_COMPACT_FAILURE_TRACE_LINE_RE = re.compile(
    r"^(?:>\s*)?⚠️\s*🛠️\s+\S[\s\S]*\s+\(agent\)`{0,2}\s+failed(?:\s*:.*)?\s*$", re.I)
_INTERNAL_COMPACT_COMMAND_TRACE_LINE_RE = re.compile(
    r"^(?:>\s*)?🛠️\s*(?:(?:(?:elevated|pty)\b\s*(?:·|,)\s*)+)?"
    r"(?:`{1,2}\s*\S|(?:run|check|fetch|pull|push|view|show|list|switch|create"
    r"|merge|rebase|stage|restore|reset|stash|search|find|print|copy|move"
    r"|remove|install|start|cd|git|pnpm|npm|yarn|bun|node|python|python3|bash"
    r"|sh)\b)", re.I)
_INTERNAL_CHANNEL_TRACE_LINE_RE = re.compile(
    r"^(?:>\s*)?(?:tool[-_ ]?call|tool[-_ ]?result|function[-_ ]?call)\s*[:=]", re.I)

# DisPatch-local, NOT upstream: a bare `[Context: …]` prefix line, observed on
# this box's transcripts. Kept when the upstream trace rules were re-derived so
# the port is strictly wider than what DisPatch already removed.
_LOCAL_TRACE_LINE_RE = re.compile(r"^\[Context:", re.I)

# --------------------------------------------------------------------------- #
# Tool-call XML tags (stripToolCallXmlTags)
#
# A stateful pass: content is hidden from an opening tag through its matching
# close, or to end-of-string when the stream was truncated mid-block. It is the
# stage that removes `<tool_call>{"name": …}</tool_call>` and the `antml:`
# invoke/parameter shapes that models emit as plain text.
# --------------------------------------------------------------------------- #

_TOOL_CALL_QUICK_RE = re.compile(
    r"<\s*/?\s*(?:antml:)?(?:tool_call|tool_result|function_calls?"
    r"|function_response|function|tool_calls|invoke|parameter)\b", re.I)
_TOOL_CALL_TAG_NAMES = frozenset({
    "tool_call", "tool_result", "function_call", "function_calls",
    "function_response", "function", "tool_calls",
    "antml:invoke", "antml:parameter",
})
_TOOL_CALL_JSON_PAYLOAD_START_RE = re.compile(
    r"(?:\s+[A-Za-z_:][-A-Za-z0-9_:.]*\s*=\s*"
    # `[\[{]`, where upstream writes `[[{]`: JS reads a leading `[` in a class as
    # a literal, Python warns it looks like a nested set. Same two characters.
    r"(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))*\s*(?:\r?\n\s*)?[\[{]")
_TOOL_CALL_XML_PAYLOAD_START_RE = re.compile(
    r"\s*(?:\r?\n\s*)?<(?:antml:)?"
    r"(?:function_call|tool_call|function|invoke|parameters?|arguments?)\b", re.I)
_NESTED_JSON_TOOL_CALL_PAYLOAD_START_RE = re.compile(
    r"\s*(?:\r?\n\s*)?<(?:function_call|tool_call)\b", re.I)
_XML_NAME_START_RE = re.compile(r"[A-Za-z_:]")
_XML_NAME_CHAR_RE = re.compile(r"[A-Za-z0-9_.:-]")
_PARAMETER_TAG_QUICK_RE = re.compile(r"<\s*/?\s*parameter\b", re.I)
_FUNCTION_NAME_ATTR_RE = re.compile(r"\bname\s*=")
# `\Z`, not `$`: a tag body can contain a newline and Python's `$` would match
# before a trailing one, calling `<function\n/>`-shaped prose self-closing.
_TRAILING_SLASH_RE = re.compile(r"/\s*\Z")

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
    if not text or not _INTERNAL_TRACE_LINE_QUICK_RE.search(text):
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    pos = 0
    for raw_line in text.splitlines(keepends=True):
        trimmed = raw_line.strip()
        drop = not _is_inside_code(pos, regions) and (
            _INTERNAL_TRACE_LINE_RE.match(trimmed)
            or _INTERNAL_COMPACT_FAILURE_TRACE_LINE_RE.match(trimmed)
            or _INTERNAL_COMPACT_COMMAND_TRACE_LINE_RE.match(trimmed)
            or _INTERNAL_CHANNEL_TRACE_LINE_RE.match(trimmed)
            or _LOCAL_TRACE_LINE_RE.match(trimmed))
        if not drop:
            out.append(raw_line)
        pos += len(raw_line)
    return "".join(out)


def _strip_relevant_memories_tags(text: str) -> str:
    """Remove `<relevant_memories>` BLOCKS — tags and recalled content alike.

    Fails closed: an opening tag with no close drops everything after it, so a
    truncated recall block cannot leak the way a truncated runtime block can't.
    """
    if not text or not _MEMORY_TAG_QUICK_RE.search(text):
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    last = 0
    in_block = False
    for m in _MEMORY_TAG_RE.finditer(text):
        if _is_inside_code(m.start(), regions):
            continue
        is_close = m.group(1) == "/"
        if not in_block:
            out.append(text[last:m.start()])
            if not is_close:
                in_block = True
        elif is_close:
            in_block = False
        last = m.end()
    if not in_block:
        out.append(text[last:])
    return "".join(out)


def _strip_model_special_tokens(text: str) -> str:
    """Strip leaked control tokens like `<|assistant|>`, keeping words apart.

    The separator matters: upstream drops in a space when the token was welding
    two non-space characters together, so `Reply here.<|assistant|>More.` comes
    out as two sentences rather than `here.More.`. A plain removal ran here for
    as long as this module existed and quietly glued words in every reply a
    local model punctuated with control tokens.
    """
    if not text or not _MODEL_SPECIAL_TOKEN_RE.search(text):
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    cursor = 0
    for m in _MODEL_SPECIAL_TOKEN_RE.finditer(text):
        if _is_inside_code(m.start(), regions):
            continue
        out.append(text[cursor:m.start()])
        before = _at(text, m.start() - 1)
        after = _at(text, m.end())
        if before and after and not before.isspace() and not after.isspace():
            out.append(" ")
        cursor = m.end()
    out.append(text[cursor:])
    return "".join(out)


def _strip_minimax_tool_call_xml(text: str) -> str:
    """Remove the malformed Minimax tool invocations that leak into text."""
    if not text or not _MINIMAX_QUICK_RE.search(text):
        return text
    return _strip_outside_code(text, _MINIMAX_TOOL_XML_RE)


def _is_legacy_bracket_tool_call_payload(value: str) -> bool:
    return bool(_LEGACY_CALL_TOOL_RE.search(value)
                and _LEGACY_CALL_ARGS_RE.search(value))


def _is_legacy_bracket_tool_result_payload(value: str) -> bool:
    return bool(_LEGACY_RESULT_JSON_RE.match(value)
                or _LEGACY_RESULT_FAT_ARROW_RE.search(value)
                or _LEGACY_RESULT_COLON_RE.search(value))


def _strip_legacy_bracket_tool_call_blocks(text: str) -> str:
    """Remove `[TOOL_CALL] … [/TOOL_CALL]` blocks whose payload really is one.

    The payload check is the whole point: `[TOOL_CALL]` written in prose has no
    `tool => "x"` / `args =>` body, so it is left where the agent put it.
    """
    if not text or not _LEGACY_BRACKET_QUICK_RE.search(text):
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        open_match = _LEGACY_BRACKET_OPEN_RE.search(text, cursor)
        if not open_match:
            out.append(text[cursor:])
            break
        block_kind = open_match.group(1).upper()
        open_start = open_match.start()
        payload_start = open_match.end()
        if _is_inside_code(open_start, regions):
            out.append(text[cursor:payload_start])
            cursor = payload_start
            continue
        close_match = _LEGACY_BRACKET_CLOSE_RE[block_kind].search(text, payload_start)
        if close_match and not _is_inside_code(close_match.start(), regions):
            close_start = close_match.start()
        else:
            close_match, close_start = None, -1
        payload_end = close_start if close_start >= 0 else len(text)
        payload = text[payload_start:payload_end]
        is_block = (_is_legacy_bracket_tool_result_payload(payload)
                    if block_kind == "RESULT"
                    else _is_legacy_bracket_tool_call_payload(payload))
        if not is_block:
            out.append(text[cursor:payload_start])
            cursor = payload_start
            continue
        out.append(text[cursor:open_start])
        cursor = close_match.end() if close_match else len(text)
    return "".join(out)


def _consume_jsonish(text: str, start: int, *,
                     allow_leading_newlines: bool = False) -> int | None:
    """End offset of a JSON value, quoted string, or rest-of-line at ``start``."""
    index = start
    while index < len(text):
        char = text[index]
        if char in " \t" or (allow_leading_newlines and char in "\r\n"):
            index += 1
            continue
        break
    if index >= len(text):
        return None
    start_char = text[index]
    if start_char in "{[":
        depth = 0
        in_string = False
        escape = False
        for idx in range(index, len(text)):
            char = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    return idx + 1
        return None
    if start_char == '"':
        escape = False
        for idx in range(index + 1, len(text)):
            char = text[idx]
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                return idx + 1
        return None
    end = index
    while end < len(text) and text[end] not in "\r\n":
        end += 1
    return end


def _strip_downgraded_tool_call_text(text: str) -> str:
    """Remove tool call/result text a cross-provider history replay leaves behind.

    Three shapes: `[Tool Call: name]` (plus an `Arguments:` payload on the next
    line), `[Tool Result for ID …]` AND ITS CONTENT up to the next `[Tool `
    marker, and `[Historical context: …]` headers.

    OUTSIDE CODE, which upstream does NOT do — verified against the installed
    gateway, whose version happily deletes a `[Tool Call: y]` an agent put in a
    fence to explain the format, and empties the inline span around it. Every
    other marker strip in this module skips code regions because doing
    otherwise has repeatedly corrupted agents' own explanations of DisPatch's
    syntax; this stage is held to the same rule. The divergence only ever
    removes LESS, and only inside code, where the runtime never writes.
    """
    if not text or not _DOWNGRADED_QUICK_RE.search(text):
        return text
    return sub_outside_code(text, _strip_downgraded_tool_call_text_segment).strip()


def _strip_downgraded_tool_call_text_segment(text: str) -> str:
    if not text or not _DOWNGRADED_QUICK_RE.search(text):
        return text
    out: list[str] = []
    cursor = 0
    for match in _DOWNGRADED_TOOL_CALL_RE.finditer(text):
        if match.start() < cursor:
            continue
        out.append(text[cursor:match.start()])
        index = match.end()
        while index < len(text) and text[index] in " \t":
            index += 1
        if text[index:index + 1] == "\r":
            index += 1
        if text[index:index + 1] == "\n":
            index += 1
        while index < len(text) and text[index] in " \t":
            index += 1
        if text[index:index + 9].lower() == "arguments":
            index += 9
            if text[index:index + 1] == ":":
                index += 1
            if text[index:index + 1] == " ":
                index += 1
            end = _consume_jsonish(text, index, allow_leading_newlines=True)
            if end is not None:
                index = end
        joined = "".join(out)
        if (text[index:index + 1] in ("\n", "\r")
                and (not joined or joined.endswith(("\n", "\r")))):
            if text[index:index + 1] == "\r":
                index += 1
            if text[index:index + 1] == "\n":
                index += 1
        cursor = index
    out.append(text[cursor:])
    cleaned = "".join(out)
    cleaned = _DOWNGRADED_TOOL_RESULT_RE.sub("", cleaned)
    return _DOWNGRADED_HISTORICAL_RE.sub("", cleaned)


class _XmlTag(NamedTuple):
    content_start: int
    end: int
    is_close: bool
    is_self_closing: bool
    tag_name: str
    is_truncated: bool


def _at(text: str, index: int) -> str:
    """The character at ``index``, or "" past either end — JS indexing."""
    return text[index] if 0 <= index < len(text) else ""


def _ends_inside_quoted_string(text: str, start: int, end: int) -> bool:
    quote_char = None
    is_escaped = False
    for idx in range(start, end):
        char = text[idx]
        if quote_char is None:
            if char in "\"'":
                quote_char = char
            continue
        if is_escaped:
            is_escaped = False
            continue
        if char == "\\":
            is_escaped = True
            continue
        if char == quote_char:
            quote_char = None
    return quote_char is not None


def _is_tool_call_boundary(char: str) -> bool:
    return not char or char.isspace() or char in "/>"


def _find_tag_close_index(text: str, start: int) -> int:
    """Index of the `>` that closes a tag, quote-aware; -1 if truncated."""
    quote_char = None
    is_escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if quote_char is not None:
            if is_escaped:
                is_escaped = False
            elif char == "\\":
                is_escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in "\"'":
            quote_char = char
            continue
        if char == "<":
            return -1
        if char == ">":
            return idx
    return -1


def _parse_xml_tag_at(text: str, start: int) -> _XmlTag | None:
    if _at(text, start) != "<":
        return None
    cursor = start + 1
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    is_close = False
    if _at(text, cursor) == "/":
        is_close = True
        cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    name_start = cursor
    if not _XML_NAME_START_RE.match(_at(text, cursor) or "\0"):
        return None
    cursor += 1
    while cursor < len(text) and _XML_NAME_CHAR_RE.match(text[cursor]):
        cursor += 1
    tag_name = text[name_start:cursor].lower()
    if not _is_tool_call_boundary(_at(text, cursor)):
        return None
    content_start = cursor
    close_index = _find_tag_close_index(text, cursor)
    if close_index == -1:
        return _XmlTag(content_start, len(text), is_close, False, tag_name, True)
    return _XmlTag(
        content_start, close_index + 1, is_close,
        not is_close and bool(_TRAILING_SLASH_RE.search(text[cursor:close_index])),
        tag_name, False)


def _parse_tool_call_tag_at(text: str, start: int) -> _XmlTag | None:
    tag = _parse_xml_tag_at(text, start)
    return tag if tag is not None and tag.tag_name in _TOOL_CALL_TAG_NAMES else None


def _detect_tool_call_payload_kind(text: str, start: int) -> str | None:
    if _TOOL_CALL_JSON_PAYLOAD_START_RE.match(text, start):
        return "json"
    if _TOOL_CALL_XML_PAYLOAD_START_RE.match(text, start):
        return "xml"
    return None


def _starts_with_nested_json_tool_call_payload(text: str, start: int) -> bool:
    if not _NESTED_JSON_TOOL_CALL_PAYLOAD_START_RE.match(text, start):
        return False
    cursor = start
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    nested = _parse_tool_call_tag_at(text, cursor)
    if (nested is None or nested.is_close or nested.is_self_closing
            or nested.is_truncated
            or nested.tag_name not in ("function_call", "tool_call")):
        return False
    return bool(_TOOL_CALL_JSON_PAYLOAD_START_RE.match(text, nested.end))


def _is_likely_standalone_function_tool_call(text: str, tag_start: int,
                                             tag: _XmlTag) -> bool:
    """A bare `<function name=…>` only counts at the start of a line or sentence.

    Without this, prose about "the `<function name=x>` shape" would take the
    rest of the message with it.
    """
    if (tag.tag_name != "function" or tag.is_close or tag.is_self_closing
            or tag.is_truncated):
        return False
    if not _FUNCTION_NAME_ATTR_RE.search(text[tag.content_start:tag.end]):
        return False
    idx = tag_start - 1
    while idx >= 0 and text[idx] in " \t":
        idx -= 1
    return idx < 0 or text[idx] in "\n\r" or text[idx] in ".!?:"


def _is_standalone_opening_tag_line(text: str, tag_start: int, tag: _XmlTag) -> bool:
    idx = tag_start - 1
    while idx >= 0 and text[idx] in " \t":
        idx -= 1
    if not (idx < 0 or text[idx] in "\n\r"):
        return False
    return _is_opening_tag_followed_by_line_break(text, tag)


def _is_opening_tag_followed_by_line_break(text: str, tag: _XmlTag) -> bool:
    after = tag.end
    while after < len(text) and text[after] in " \t":
        after += 1
    return after >= len(text) or text[after] in "\n\r"


def _has_same_line_content_after_opening_tag(text: str, tag: _XmlTag) -> bool:
    after = tag.end
    while after < len(text) and text[after] in " \t":
        after += 1
    return after < len(text) and text[after] not in "\n\r"


def _is_visible_line_start(parts: list[str]) -> bool:
    """Upstream's isVisibleLineStart(result), over the accumulated output."""
    for part in reversed(parts):
        for char in reversed(part):
            if char in " \t":
                continue
            return char in "\n\r"
    return True


def _is_adjacent_to_stripped_tool_call_block(text: str, tag_start: int,
                                             last_stripped_end: int | None) -> bool:
    if last_stripped_end is None or last_stripped_end > tag_start:
        return False
    return all(text[idx] in " \t\n\r" for idx in range(last_stripped_end, tag_start))


def _find_matching_tool_call_close_index(text: str, start: int, tag_name: str) -> int:
    idx = start
    while idx < len(text):
        if text[idx] != "<":
            idx += 1
            continue
        tag = _parse_tool_call_tag_at(text, idx)
        if tag is None:
            idx += 1
            continue
        if tag.is_close and tag.tag_name == tag_name and not tag.is_truncated:
            return idx
        idx = max(idx, tag.end - 1) + 1
    return -1


def _find_adjacent_opening_tool_call_tag(text: str, start: int,
                                         tag_name: str) -> _XmlTag | None:
    idx = start
    while idx < len(text) and text[idx].isspace():
        idx += 1
    if _at(text, idx) != "<":
        return None
    tag = _parse_tool_call_tag_at(text, idx)
    if tag is None or tag.is_close or tag.tag_name != tag_name:
        return None
    return tag


def _has_matching_xml_close_tag(text: str, start: int, tag_name: str) -> bool:
    depth = 1
    idx = start
    while idx < len(text):
        if text[idx] != "<":
            idx += 1
            continue
        tag = _parse_xml_tag_at(text, idx)
        if tag is None or tag.tag_name != tag_name or tag.is_truncated:
            idx += 1
            continue
        if tag.is_close:
            depth -= 1
            if depth == 0:
                return True
        elif not tag.is_self_closing:
            depth += 1
        idx = max(idx, tag.end - 1) + 1
    return False


def _is_dangling_function_parameter_parent(text: str, tag: _XmlTag) -> bool:
    if (tag.tag_name != "function"
            or not _FUNCTION_NAME_ATTR_RE.search(text[tag.content_start:tag.end])):
        return False
    cursor = tag.end
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    next_tag = _parse_xml_tag_at(text, cursor)
    return (next_tag is not None and next_tag.tag_name == "parameter"
            and not next_tag.is_close)


def _consume_immediate_line_break(text: str, start: int) -> int | None:
    if _at(text, start) == "\r" and _at(text, start + 1) == "\n":
        return start + 2
    return start + 1 if _at(text, start) in ("\n", "\r") else None


def _trim_immediate_line_break_before(text: str, start: int, end: int) -> int:
    if end > start and text[end - 1] == "\n":
        return end - (2 if end - 2 >= start and text[end - 2] == "\r" else 1)
    return end - 1 if end > start and text[end - 1] == "\r" else end


def _is_line_start_at(text: str, start: int) -> bool:
    cursor = start - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    return cursor < 0 or text[cursor] in "\n\r"


def _is_line_end_after(text: str, end: int) -> bool:
    cursor = end
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    return cursor >= len(text) or text[cursor] in "\n\r"


def _unwrap_standalone_parameter_tags(text: str) -> str:
    """Unwrap a top-level `<parameter=…>` shell, keeping the value inside it."""
    if not _PARAMETER_TAG_QUICK_RE.search(text):
        return text
    regions = _find_code_regions(text)
    open_tags: list[dict] = []
    out: list[str] = []
    last = 0
    idx = 0
    while idx < len(text):
        if text[idx] != "<" or _is_inside_code(idx, regions):
            idx += 1
            continue
        tag = _parse_xml_tag_at(text, idx)
        if tag is None or tag.is_truncated:
            idx += 1
            continue
        if tag.is_close:
            open_index = next(
                (i for i in range(len(open_tags) - 1, -1, -1)
                 if open_tags[i]["name"] == tag.tag_name), None)
            if open_index is not None:
                opening = open_tags[open_index]
                if opening["unwrap"]:
                    content_end = idx
                    if (opening["trim_boundary_line_breaks"]
                            and _is_line_start_at(text, idx)
                            and _is_line_end_after(text, tag.end)):
                        content_end = _trim_immediate_line_break_before(text, last, idx)
                    out.append(text[last:content_end])
                    last = tag.end
                del open_tags[open_index:]
        elif tag.is_self_closing:
            if tag.tag_name == "parameter" and not open_tags:
                out.append(text[last:idx])
                last = tag.end
        elif (_has_matching_xml_close_tag(text, tag.end, tag.tag_name)
              or _is_dangling_function_parameter_parent(text, tag)):
            unwrap = tag.tag_name == "parameter" and not open_tags
            trim_boundary_line_breaks = False
            if unwrap:
                out.append(text[last:idx])
                last = tag.end
                content_start = (_consume_immediate_line_break(text, last)
                                 if _is_line_start_at(text, idx) else None)
                if content_start is not None:
                    last = content_start
                    trim_boundary_line_breaks = True
            open_tags.append({"name": tag.tag_name, "unwrap": unwrap,
                              "trim_boundary_line_breaks": trim_boundary_line_breaks})
        idx = max(idx, tag.end - 1) + 1
    out.append(text[last:])
    return "".join(out)


def _strip_tool_call_xml_tags(
        text: str, *,
        strip_function_calls_xml_payloads: bool = False,
        strip_function_response_after_plural_tool_calls: bool = False) -> str:
    """Hide XML-style tool calls models emit as plain text.

    Stateful: from an opening tag through its matching close, or to end-of-text
    when the stream was truncated mid-block (fail closed). A tag with no tool
    PAYLOAD after it is left visible — that is what keeps `<function>` used as
    an ordinary word from swallowing a reply.
    """
    if not text or not _TOOL_CALL_QUICK_RE.search(text):
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    last = 0
    in_block = False
    block_content_start = 0
    block_needs_quote_balance = False
    block_start = 0
    block_tag_name: str | None = None
    last_stripped_end: int | None = None
    visible_balance: dict[str, int] = {}
    idx = 0
    while idx < len(text):
        if text[idx] != "<":
            idx += 1
            continue
        if not in_block and _is_inside_code(idx, regions):
            idx += 1
            continue
        tag = _parse_tool_call_tag_at(text, idx)
        if tag is None:
            idx += 1
            continue
        if not in_block:
            out.append(text[last:idx])
            if tag.is_close:
                if tag.is_truncated:
                    out.append(text[idx:tag.content_start])
                    last = tag.content_start
                    idx = max(idx, tag.content_start - 1) + 1
                    continue
                balance = visible_balance.get(tag.tag_name, 0)
                if balance > 0:
                    out.append(text[idx:tag.end])
                    visible_balance[tag.tag_name] = balance - 1
                last = tag.end
                idx = max(idx, tag.end - 1) + 1
                continue
            if tag.is_self_closing:
                last_stripped_end = tag.end
                last = tag.end
                idx = max(idx, tag.end - 1) + 1
                continue
            payload_start = tag.content_start if tag.is_truncated else tag.end
            is_plural_wrapper = tag.tag_name in ("function_calls", "tool_calls")
            matching_close_start = (
                _find_matching_tool_call_close_index(text, tag.end, tag.tag_name)
                if is_plural_wrapper else -1)
            matching_close_tag = (
                None if matching_close_start == -1
                else _parse_tool_call_tag_at(text, matching_close_start))
            strip_plural_before_response = (
                strip_function_response_after_plural_tool_calls
                and is_plural_wrapper and matching_close_tag is not None
                and _find_adjacent_opening_tool_call_tag(
                    text, matching_close_tag.end, "function_response") is not None)
            if (tag.tag_name in ("tool_call", "function", "antml:invoke")
                    or ((strip_function_calls_xml_payloads
                         or strip_plural_before_response) and is_plural_wrapper)):
                payload_kind = _detect_tool_call_payload_kind(text, payload_start)
            else:
                payload_kind = ("json" if _TOOL_CALL_JSON_PAYLOAD_START_RE.match(
                    text, payload_start) else None)
            strip_standalone_function = (
                tag.tag_name != "function"
                or _is_likely_standalone_function_tool_call(text, idx, tag))
            function_response_close_start = (
                _find_matching_tool_call_close_index(text, tag.end, tag.tag_name)
                if tag.tag_name == "function_response" else -1)
            strip_adjacent_result = (
                _is_adjacent_to_stripped_tool_call_block(text, idx, last_stripped_end)
                and (_is_opening_tag_followed_by_line_break(text, tag)
                     or function_response_close_start != -1
                     or _has_same_line_content_after_opening_tag(text, tag)))
            strip_standalone_result = tag.tag_name == "function_response" and (
                _is_standalone_opening_tag_line(text, idx, tag)
                or strip_adjacent_result
                or (function_response_close_start != -1
                    and _is_visible_line_start(out)
                    and _is_opening_tag_followed_by_line_break(text, tag)))
            if ((payload_kind and strip_standalone_function)
                    or strip_standalone_result):
                in_block = True
                block_content_start = tag.end
                block_needs_quote_balance = (
                    payload_kind == "json"
                    or (payload_kind == "xml"
                        and _starts_with_nested_json_tool_call_payload(
                            text, payload_start)))
                block_start = idx
                block_tag_name = tag.tag_name
                if tag.is_truncated:
                    last = len(text)
                    break
            else:
                preserve_end = tag.content_start if tag.is_truncated else tag.end
                out.append(text[idx:preserve_end])
                if not tag.is_truncated:
                    visible_balance[tag.tag_name] = (
                        visible_balance.get(tag.tag_name, 0) + 1)
                last = preserve_end
                idx = max(idx, preserve_end - 1) + 1
                continue
        elif (tag.is_close
              and (tag.tag_name == block_tag_name
                   or (block_tag_name == "tool_result" and tag.tag_name == "tool_call"))
              and (not block_needs_quote_balance
                   or not _ends_inside_quoted_string(text, block_content_start, idx))):
            in_block = False
            block_needs_quote_balance = False
            if block_tag_name:
                last_stripped_end = tag.end
            block_tag_name = None
        last = tag.end
        idx = max(idx, tag.end - 1) + 1
    if not in_block:
        out.append(text[last:])
    elif block_tag_name == "function":
        out.append(text[block_start:])
    return _unwrap_standalone_parameter_tags("".join(out))


# Characters a text block can only START with when it is the TAIL of a
# mis-split thinking stream: closing brackets and mid-expression separators.
#
# POSITIVE EVIDENCE ONLY, DELIBERATELY. This used to be the inverse — an
# allowlist of characters a real reply may open with — and every markdown
# opener it forgot was classified as reasoning and returned as the empty
# string. main.py treats "" as nothing to persist, so a reply opening with a
# blockquote (">"), an image ("!["), a "+" list, a "~~~" fence or a question
# ("?") was silently never written to the database at all. An allowlist that
# has to enumerate every legal opener fails closed on the MESSAGE; this
# direction fails open, which is the right way round for a heuristic whose
# false positive is total data loss.
_REASONING_TAIL_START_CHARS = frozenset(",;)]}%&^\\")

# "/" is the one opener that is evidence BOTH ways: the observed 2026-08-19
# case opened "/ ` blocks that leak…" (reasoning cut at a backtick), while a
# reply opening "/media/…" or "/api/health" is a path and is speech. A dangling
# slash is followed by a space; a path is not, so that is the discriminator.
_DANGLING_SLASH_RE = re.compile(r"^/\s")

# A reasoning tail is long; a terse odd-start line is more likely a real
# (if unusual) reply. Only judge texts at least this long.
_REASONING_TAIL_MIN_CHARS = 200


def _is_reasoning_tail(text: str) -> bool:
    """True when a stripped text block is the unlabelled continuation of a
    thinking stream: it begins with a character a reply cannot begin with."""
    if not text:
        return False
    if (text[0] not in _REASONING_TAIL_START_CHARS
            and not _DANGLING_SLASH_RE.match(text)):
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
    # From here the ORDER IS THE GATEWAY'S — applyAssistantVisibleTextStagePipeline's
    # stripNonReasoningStages, then reasoning last (the "delivery" profile's
    # stageOrder). Order is load-bearing between several of these (the special-token
    # strip mangles Harmony headers the plain-text stage still needs to see the
    # shape of, and the trace-line stage runs before the bracket stages), so keep
    # them in step with dist/assistant-visible-text-*.js rather than tidying them.
    cleaned = _strip_minimax_tool_call_xml(cleaned)
    # DisPatch-local, wedged in ahead of the special-token strip: that strip would
    # otherwise eat a `<|channel|>thought` fence marker at a time and leave the
    # reasoning text behind.
    cleaned = _strip_outside_code(cleaned, _THINKING_CHANNEL_RE)
    cleaned = _strip_outside_code(cleaned, _THINKING_FENCED_RE)
    cleaned = _strip_model_special_tokens(cleaned)
    cleaned = _strip_relevant_memories_tags(cleaned)
    cleaned = _strip_tool_call_xml_tags(
        cleaned, strip_function_response_after_plural_tool_calls=True)
    cleaned = _strip_internal_trace_lines(cleaned)
    cleaned = _strip_legacy_bracket_tool_call_blocks(cleaned)
    cleaned = strip_plain_text_tool_call_blocks(cleaned)
    cleaned = _strip_downgraded_tool_call_text(cleaned)
    cleaned = _strip_outside_code(cleaned, _REASONING_TAG_RE)
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


# --------------------------------------------------------------------------- #
# Drift tripwire
#
# Every hand-ported pattern in this module, paired with a literal fragment of
# the regex/marker it was copied from that must still exist in the INSTALLED
# gateway's `dist/*.js`. test_openclaw_text.py asserts each one is still there.
#
# This exists because the port is only as good as the day it was written, and
# the two defects that made it fail open were both silent drift: the memory-tag
# pattern matched a shape (`<relevant_memories>`) the gateway had already
# generalised to `relevant[-_]memories` with attributes, and the tool-result
# trace rule expected a colon (`[Tool Result for ID:`) the producer never
# emitted. Nothing failed; internal text simply started arriving in the family
# chat. A missing anchor here means the next `openclaw update` moved something
# under us and the corresponding stage should be re-derived from the dist.
# --------------------------------------------------------------------------- #

GATEWAY_DIST_ANCHORS: dict[str, str] = {
    "INTERNAL_RUNTIME_CONTEXT_BEGIN": INTERNAL_RUNTIME_CONTEXT_BEGIN,
    "INTERNAL_RUNTIME_CONTEXT_END": INTERNAL_RUNTIME_CONTEXT_END,
    "_LEGACY_INTERNAL_CONTEXT_HEADER": "OpenClaw runtime context (internal):",
    "_UNTRUSTED_RESULT_MARKERS": "<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>",
    "_SCAFFOLDING_TAGS": "system-reminder",
    "_PROMPT_DATA_TAG_NAMES": "untrusted-text",
    "_MODEL_SPECIAL_TOKEN_RE": r"<[|｜][^|｜]*[|｜]>",
    "_MEMORY_TAG_RE": r"relevant[-_]memories\b",
    "_REASONING_TAG_NAME": r"(?:(?:antml:|mm:)?(?:think(?:ing)?|thought)|antthinking)",
    "_MINIMAX_TOOL_XML_RE": r"<\/?minimax:tool_call>",
    "_LEGACY_BRACKET_OPEN_RE": r"\[\s*TOOL_(CALL|RESULT)\s*\]",
    "_LEGACY_CALL_TOOL_RE": r"\btool\s*=>\s*",
    "_LEGACY_RESULT_FAT_ARROW_RE": r"\b(?:tool|result|output|content)\s*=>",
    "_DOWNGRADED_TOOL_CALL_RE": r"\[Tool Call:[^\]]*\]",
    "_DOWNGRADED_TOOL_RESULT_RE": r"\[Tool Result for ID[^\]]*\]",
    "_DOWNGRADED_HISTORICAL_RE": r"\[Historical context:[^\]]*\]",
    "_INTERNAL_TRACE_LINE_RE": (
        "Session Status|Exec|Read|Edit|Write|Patch|Search|Open|Click|Find"
        "|Screenshot|Update Plan|Tool Call|Tool Result|Function Call|Shell|Command"),
    "_INTERNAL_CHANNEL_TRACE_LINE_RE": (
        r"(?:tool[-_ ]?call|tool[-_ ]?result|function[-_ ]?call)\s*[:=]"),
    "_TOOL_CALL_TAG_NAMES": "antml:invoke",
    "_TOOL_CALL_JSON_PAYLOAD_START_RE": (
        r"[A-Za-z_:][-A-Za-z0-9_:.]*\s*=\s*"),
    "_TOOL_CALL_XML_PAYLOAD_START_RE": (
        r"(?:function_call|tool_call|function|invoke|parameters?|arguments?)\b"),
    "_XMLISH_FUNCTION_OPEN_RE": "<function=[A-Za-z0-9_.:-]{1,120}>",
    "_XMLISH_PARAM_OPEN_RE": "<parameter=([A-Za-z0-9_.:-]{1,120})>",
    "_QUICK_BRACKET_RE": r"\[(?:tool:)?[A-Za-z0-9_-]+\]",
    "_QUICK_HARMONY_RE": r"(?:commentary|analysis|final)\s+to=",
    "_END_TOOL_REQUEST": "[END_TOOL_REQUEST]",
    "_HARMONY_MESSAGE_MARKER": "<|message|>",
    "_HARMONY_CALL_MARKER": "<|call|>",
}


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
