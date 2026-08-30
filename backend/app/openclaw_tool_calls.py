"""Port of OpenClaw's plain-text tool-call stripper.

The gateway runs `stripPlainTextToolCallBlocks` as one stage of its
assistant-visible-text pipeline. It removes the tool calls a model emits as
ORDINARY TEXT instead of as structured tool calls — three shapes:

  * bracketed   ``[tool:exec]{"cmd": "ls"}``  /  ``[exec]\\n{...}\\n[/exec]``
  * Harmony     ``<|channel|>commentary to=exec code<|message|>{...}<|call|>``
  * XML-ish     ``<function=exec><parameter=cmd>ls</parameter></function>``

These are exactly what the LOCAL the local model server models (Doxy, Charley, beta) emit
when a chat template's tool grammar is not honoured by the runtime, and
DisPatch had no equivalent stage — so the raw block reached the family chat
verbatim, JSON payload and all.

Ported from the installed gateway's unminified
`dist/payload-*.js` (`packages/tool-call-repair/src/{grammar,payload}.ts`).
It lives in its own module because it is its own package upstream.

Kept deliberately close to the original (same function names, same order) so a
future OpenClaw update can be diffed against it rather than re-derived. The one
deliberate deviation: upstream's parsers return the parsed tool name and
arguments because the gateway also REPAIRS these blocks into real tool calls.
DisPatch only ever deletes them, so the ports return the end offset alone —
the payload is still fully parsed (an unparseable payload is not a tool call
and must be left in the message), it is simply not handed back.
"""

from __future__ import annotations

import json
import re
from typing import NamedTuple

# Upstream DEFAULT_MAX_PLAIN_TEXT_TOOL_PAYLOAD_BYTES. A bigger "payload" than
# this is prose that happens to contain braces, not a tool call.
_MAX_PLAIN_TEXT_TOOL_PAYLOAD_BYTES = 256_000

_END_TOOL_REQUEST = "[END_TOOL_REQUEST]"

_TOOL_NAME_CHAR_RE = re.compile(r"[A-Za-z0-9_-]")
_CHANNEL_NAME_CHAR_RE = re.compile(r"[A-Za-z_]")
_HARMONY_CHANNELS = ("commentary", "analysis", "final")

_XMLISH_FUNCTION_OPEN_RE = re.compile(r"<function=([A-Za-z0-9_.:-]{1,120})>\s*", re.I)
_XMLISH_PARAM_OPEN_RE = re.compile(r"<parameter=([A-Za-z0-9_.:-]{1,120})>", re.I)
_XMLISH_PARAM_CLOSE_RE = re.compile(r"</parameter>", re.I)

# Cheap pre-filter, mirroring upstream: any of the three shapes' opening token.
_QUICK_BRACKET_RE = re.compile(r"\[(?:tool:)?[A-Za-z0-9_-]+\]")
_QUICK_HARMONY_RE = re.compile(
    r"(?:^|\n)\s*(?:<\|channel\|>)?(?:commentary|analysis|final)\s+to=")
_QUICK_XMLISH_RE = re.compile(r"(?:^|\n)\s*<function=[A-Za-z0-9_.:-]{1,120}>", re.I)


class _Opening(NamedTuple):
    """A parsed tool-call opening token and what it obliges the block to have."""

    end: int
    name: str
    requires_closing: bool
    allows_optional_xmlish_close: bool = False


def _at(text: str, index: int) -> str:
    """The character at ``index``, or "" past either end — JS indexing."""
    return text[index] if 0 <= index < len(text) else ""


def _is_plain_text_tool_name_char(char: str) -> bool:
    """Tool names in bracket/plain-text repairs match provider-safe ids only."""
    return bool(char) and bool(_TOOL_NAME_CHAR_RE.match(char))


def _skip_horizontal_whitespace(text: str, start: int) -> int:
    """Skip spaces and tabs only, preserving line boundaries for the grammar."""
    index = start
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _skip_whitespace(text: str, start: int) -> int:
    """Skip all whitespace, for the points where line structure stops mattering."""
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _consume_line_break(text: str, start: int) -> int | None:
    """Consume one Unix or Windows line ending; None when there isn't one."""
    if _at(text, start) == "\r":
        return start + 2 if _at(text, start + 1) == "\n" else start + 1
    if _at(text, start) == "\n":
        return start + 1
    return None


def _find_json_object_end(text: str, start: int,
                          max_payload_bytes: int) -> int | None:
    """Exclusive end offset of the balanced JSON object starting at ``start``."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        if index + 1 - start > max_payload_bytes:
            return None
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _parse_bracket_opening(text: str, start: int) -> _Opening | None:
    """``[tool:name]`` (self-delimiting) or ``[name]\\n`` (needs a closing tag)."""
    if _at(text, start) != "[":
        return None
    cursor = start + 1
    if text.startswith("tool:", cursor):
        cursor += 5
        name_start = cursor
        while _is_plain_text_tool_name_char(_at(text, cursor)):
            cursor += 1
        if cursor == name_start or _at(text, cursor) != "]":
            return None
        return _Opening(end=cursor + 1, name=text[name_start:cursor],
                        requires_closing=False, allows_optional_xmlish_close=True)
    name_start = cursor
    while _is_plain_text_tool_name_char(_at(text, cursor)):
        cursor += 1
    if cursor == name_start or _at(text, cursor) != "]":
        return None
    name = text[name_start:cursor]
    cursor = _skip_horizontal_whitespace(text, cursor + 1)
    after_line_break = _consume_line_break(text, cursor)
    if after_line_break is None:
        return None
    return _Opening(end=after_line_break, name=name, requires_closing=True)


def _parse_harmony_opening(text: str, start: int) -> _Opening | None:
    """``[<|channel|>]<channel> to=<name> code [<|message|>]`` — Harmony headers."""
    cursor = start
    if text.startswith("<|channel|>", cursor):
        cursor += 11
    channel_start = cursor
    while _CHANNEL_NAME_CHAR_RE.match(_at(text, cursor) or "\0"):
        cursor += 1
    if text[channel_start:cursor] not in _HARMONY_CHANNELS:
        return None
    cursor = _skip_horizontal_whitespace(text, cursor)
    if not text.startswith("to=", cursor):
        return None
    cursor += 3
    name_start = cursor
    while _is_plain_text_tool_name_char(_at(text, cursor)):
        cursor += 1
    if cursor == name_start:
        return None
    name = text[name_start:cursor]
    cursor = _skip_horizontal_whitespace(text, cursor)
    if not text.startswith("code", cursor):
        return None
    cursor = _skip_whitespace(text, cursor + 4)
    if text.startswith("<|message|>", cursor):
        cursor = _skip_whitespace(text, cursor + 11)
    return _Opening(end=cursor, name=name, requires_closing=False)


def _parse_xmlish_function_opening(text: str, start: int) -> _Opening | None:
    match = _XMLISH_FUNCTION_OPEN_RE.match(text, start)
    if not match:
        return None
    return _Opening(end=match.end(), name=match.group(1), requires_closing=False)


def _parse_opening(text: str, start: int) -> _Opening | None:
    return (_parse_bracket_opening(text, start)
            or _parse_harmony_opening(text, start))


def _parse_xmlish_opening(text: str, start: int) -> _Opening | None:
    return (_parse_bracket_opening(text, start)
            or _parse_xmlish_function_opening(text, start))


def _consume_json_object(text: str, start: int,
                         max_payload_bytes: int) -> int | None:
    """End offset of a well-formed JSON OBJECT at ``start``, else None.

    The parse is the load-bearing part: a bracketed line followed by prose is a
    markdown footnote, not a tool call, and must survive untouched.
    """
    cursor = _skip_whitespace(text, start)
    if _at(text, cursor) != "{":
        return None
    end = _find_json_object_end(text, cursor, max_payload_bytes)
    if end is None:
        return None
    try:
        parsed = json.loads(text[cursor:end])
    except ValueError:
        return None
    return end if isinstance(parsed, dict) else None


def _parse_closing(text: str, start: int, name: str) -> int | None:
    cursor = _skip_whitespace(text, start)
    if text.startswith(_END_TOOL_REQUEST, cursor):
        return cursor + len(_END_TOOL_REQUEST)
    named_closing = f"[/{name}]"
    if text.startswith(named_closing, cursor):
        return cursor + len(named_closing)
    return None


def _parse_optional_harmony_closing(text: str, start: int) -> int:
    cursor = _skip_whitespace(text, start)
    return cursor + 8 if text.startswith("<|call|>", cursor) else start


def _parse_plain_text_tool_call_block_end_at(text: str, start: int) -> int | None:
    """End offset of a bracketed/Harmony JSON tool-call block at ``start``."""
    opening = _parse_opening(text, start)
    if opening is None:
        return None
    payload_end = _consume_json_object(text, opening.end,
                                       _MAX_PLAIN_TEXT_TOOL_PAYLOAD_BYTES)
    if payload_end is None:
        return None
    if opening.requires_closing:
        return _parse_closing(text, payload_end, opening.name)
    return _parse_optional_harmony_closing(text, payload_end)


def _find_xmlish_parameter_block_end(text: str, start: int) -> int | None:
    cursor = _skip_whitespace(text, start)
    open_match = _XMLISH_PARAM_OPEN_RE.match(text, cursor)
    if not open_match:
        return None
    close_match = _XMLISH_PARAM_CLOSE_RE.search(text, open_match.end())
    if not close_match:
        return None
    return close_match.end()


def _consume_xmlish_function_close(text: str, start: int) -> int | None:
    cursor = _skip_whitespace(text, start)
    return cursor + 11 if text[cursor:cursor + 11].lower() == "</function>" else None


def _parse_xmlish_plain_text_tool_call_block_end_at(text: str,
                                                    start: int) -> int | None:
    """End offset of a ``<function=…><parameter=…>…`` block at ``start``."""
    opening = _parse_xmlish_opening(text, start)
    if opening is None:
        return None
    cursor = opening.end
    parameter_count = 0
    while True:
        parameter_end = _find_xmlish_parameter_block_end(text, cursor)
        if parameter_end is None:
            break
        parameter_count += 1
        cursor = parameter_end
    if parameter_count == 0:
        return None
    close_end = _consume_xmlish_function_close(text, cursor)
    if opening.allows_optional_xmlish_close:
        return cursor if close_end is None else close_end
    return close_end


def strip_plain_text_tool_call_blocks(text: str) -> str:
    """Remove full-line standalone plain-text tool-call blocks.

    FULL-LINE, deliberately: a block only counts when its opening token starts
    a line (after horizontal whitespace). That is what keeps an agent writing
    "call it with `[tool:exec]` and a JSON body" mid-sentence intact, without
    needing the code-region machinery the other stages use.
    """
    if not text:
        return text
    if not (_QUICK_BRACKET_RE.search(text)
            or _QUICK_HARMONY_RE.search(text)
            or _QUICK_XMLISH_RE.search(text)):
        return text
    out: list[str] = []
    cursor = 0
    index = 0
    while index < len(text):
        if not (index == 0 or text[index - 1] == "\n"):
            index += 1
            continue
        block_start = _skip_horizontal_whitespace(text, index)
        block_end = _parse_plain_text_tool_call_block_end_at(text, block_start)
        if block_end is None:
            block_end = _parse_xmlish_plain_text_tool_call_block_end_at(
                text, block_start)
        if block_end is None:
            index += 1
            continue
        out.append(text[cursor:index])
        cursor = block_end
        after_block_line_break = _consume_line_break(text, cursor)
        if after_block_line_break is not None:
            cursor = after_block_line_break
        index = cursor
    out.append(text[cursor:])
    return "".join(out)
