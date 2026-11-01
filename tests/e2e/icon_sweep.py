#!/usr/bin/env python3
"""Static sweep for icon-only controls that have no name.

Every control a person can click whose face is only a picture (an <svg>, a sprite icon, an
<img>, an icon span, one emoji or glyph) must carry BOTH a tooltip (`title`) and an
accessible name (`aria-label`, or `aria-labelledby`), or the i18n attribute that sets them
(`data-i18n-attr="title=..;aria-label=.."`). The headless inventory (roles_axe.py) only
sees screens it visits; this sweep reads the source, so controls drawn on demand (message
actions, menus, the lightbox, app panels) are covered too.

What it reads: frontend/static/index.html, frontend/static/js/*.js and
apps/*/static/**/*.{html,js}. What it flags:
  html  <button>/<a>/role=button|tab|menuitem elements with no text and a missing label
  el    el('button'|'a', {...}, [icon]) calls (and role=button) with an icon child, no text,
        and no label set in the call or on the variable within the next 6 lines
  dom   document.createElement('button') / el('button', {...}) with no children whose
        variable then gets an icon or a glyph (append/textContent/innerHTML) and no label
  tpl   `<button ...>${spriteIconHtml(..)}</button>`-style template markup with no label

A line may opt out with the marker `icon-sweep: ok (<reason>)` (on the control's line).
Heuristic by design: judge findings by reading the code. The CI guard
(backend/tests/test_icon_sweep.py) asserts zero findings outside ALLOW below.

Usage: python3 tests/e2e/icon_sweep.py [--root <repo>] [--json]   exit 1 when findings remain.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

MARKER = "icon-sweep: ok"

# (relative path, snippet substring) -> reason. Keep this short; every entry needs a reason.
ALLOW: dict[tuple[str, str], str] = {}

ICON_CALL = re.compile(r"\b(railIcon|spriteIcon|uiIcon|spriteIconHtml|iconSvg|svgIcon|icon)\s*\(")
# One emoji / symbol / short glyph and nothing else.
GLYPH_ONLY = re.compile(r"^\s*([^\w\s]{1,3}|&[a-z#0-9]+;)\s*$", re.U)


def glyph_only(text: str) -> bool:
    text = text.strip()
    return bool(text) and bool(GLYPH_ONLY.match(text))


# ---------------------------------------------------------------- HTML files
class _Html(HTMLParser):
    CONTROL_TAGS = frozenset({"button", "a"})
    ROLES = frozenset({"button", "tab", "menuitem"})

    def __init__(self, lines: list[str]):
        super().__init__(convert_charrefs=True)
        self.lines = lines
        self.stack: list[dict] = []
        self.found: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        for open_ in self.stack:
            if tag in ("svg", "img", "use") or "icn" in a.get("class", "") or "rail-icon" in a.get("class", ""):
                open_["icon"] = True
            if open_.get("attrs") is None:
                continue
            if tag == "img" and a.get("alt"):
                open_["named"] = True
            if "data-i18n" in a:
                open_["named"] = True
        is_control = tag in self.CONTROL_TAGS or a.get("role") in self.ROLES
        if is_control and not (tag == "a" and "href" not in a and "role" not in a and "id" not in a):
            self.stack.append({"tag": tag, "attrs": a, "line": self.getpos()[0], "text": "", "icon": False})
        elif tag in ("img", "input", "br", "hr", "meta", "link", "use", "source", "path"):
            pass
        else:
            self.stack.append({"tag": tag, "attrs": None})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in ("img", "input", "br", "hr", "meta", "link", "use", "source", "path"):
            self.handle_endtag(tag)

    def handle_data(self, data):
        for open_ in self.stack:
            if open_.get("attrs") is not None:
                open_["text"] += data

    def handle_endtag(self, tag):
        while self.stack:
            top = self.stack.pop()
            if top.get("attrs") is not None:
                self._judge(top)
            if top["tag"] == tag:
                break

    def _judge(self, c):
        a = c["attrs"]
        if "data-i18n" in a:
            return
        spec = a.get("data-i18n-attr", "")
        has_title = "title" in a or "title=" in spec
        has_aria = "aria-label" in a or "aria-labelledby" in a or "aria-label=" in spec
        if c.get("named"):
            return
        text = c["text"].strip()
        if text and not glyph_only(text):
            return
        if has_title and has_aria:
            return
        line = c["line"]
        src = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        if MARKER in src:
            return
        miss = [n for n, ok in (("title", has_title), ("aria-label", has_aria)) if not ok]
        self.found.append((line, f"html <{c['tag']}> no {'/'.join(miss)}: {src.strip()[:140]}"))


def sweep_html(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf8")
    p = _Html(src.split("\n"))
    p.feed(src)
    p.close()
    return p.found


# ---------------------------------------------------------------- JS files
def _balanced(src: str, start: int) -> int:
    """Index just past the ')' that closes the '(' at src[start]."""
    depth, i, n = 0, start, len(src)
    quote = None
    while i < n:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            elif quote == "`" and src.startswith("${", i):
                j = _balanced_brace(src, i + 1)
                i = j
                continue
        elif ch in "'\"`":
            quote = ch
        elif ch == "/" and src.startswith("//", i):
            i = src.find("\n", i)
            if i < 0:
                return n
            continue
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _balanced_brace(src: str, start: int) -> int:
    return _balanced(src, start)


def _split_attrs(call: str) -> tuple[str, str]:
    """el('button', {attrs}, children) -> (attrs text, children text)."""
    m = re.match(r"el\(\s*['\"]\w+['\"]\s*,?\s*", call)
    rest = call[m.end():] if m else call
    if rest.startswith("{"):
        end = _balanced(rest, 0)
        return rest[:end], rest[end:]
    return "", rest


def _line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def _var_before(src: str, idx: int) -> str | None:
    line_start = src.rfind("\n", 0, idx) + 1
    m = re.search(r"(?:const|let|var)?\s*([\w$.]+)\s*=\s*$", src[line_start:idx])
    return m.group(1) if m else None


def _children_have_text(children: str) -> bool:
    body = children.strip().rstrip(")").strip().lstrip(",").strip()
    # drop every icon helper call (its arguments are sprite names, not words)
    while True:
        m = ICON_CALL.search(body)
        if not m:
            break
        body = body[:m.start()] + body[_balanced(body, m.end() - 1):]
    if not body.strip(" ,[]\n"):
        return False
    if re.search(r"\bt\(\s*['\"`]", body) or "iconLabel(" in body or re.search(r"\btext\s*:", body):
        return True
    # a string literal child that is more than a glyph
    for m in re.finditer(r"""(['"])((?:\\.|(?!\1).)*)\1""", body):
        if m.group(2).strip() and not glyph_only(m.group(2)) and not m.group(2).startswith(("/static", "rail", "icon")):
            return True
    # a bare identifier/expression child that is not an icon helper -> assume it is text
    stripped = re.sub(r"\([^()]*\)", "", body)
    if re.search(r"\b(name|label|title|caption|text|txt|lbl|count|n|msg)\w*\b", stripped, re.I):
        return True
    return False


def _after(src: str, end: int, nlines: int = 6) -> str:
    j = end
    for _ in range(nlines + 1):
        k = src.find("\n", j + 1)
        if k < 0:
            return src[end:]
        j = k
    return src[end:j]


def sweep_js(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf8")
    lines = src.split("\n")
    found: list[tuple[int, str]] = []

    def add(idx: int, kind: str, why: str):
        ln = _line_of(src, idx)
        line = lines[ln - 1]
        if MARKER in line:
            return
        found.append((ln, f"{kind} {why}: {line.strip()[:140]}"))

    # el('button'|'a', ...) and el(any, {role: 'button'}) calls
    for m in re.finditer(r"\bel\(\s*['\"](\w+)['\"]", src):
        tag = m.group(1)
        end = _balanced(src, m.start() + 2)
        call = src[m.start():end]
        attrs, children = _split_attrs(call)
        role = re.search(r"\brole\s*:\s*['\"](button|tab|menuitem)['\"]", attrs)
        if tag not in ("button", "a") and not role:
            continue
        if tag == "a" and not ICON_CALL.search(children):
            continue
        var = _var_before(src, m.start())
        after = _after(src, end) if var else ""
        var_re = re.escape(var) if var else None
        has_title = bool(re.search(r"\btitle\s*:", attrs)) or "title=" in attrs
        has_aria = bool(re.search(r"aria-label(ledby)?['\"]?\s*:", attrs)) or "aria-label=" in attrs
        if var_re:
            has_title = has_title or bool(re.search(var_re + r"\s*\.\s*(title\s*=|setAttribute\(\s*['\"]title['\"])", after))
            has_aria = has_aria or bool(re.search(var_re + r"\s*\.\s*setAttribute\(\s*['\"]aria-label", after))
            if re.search(r"labelIcon\(\s*" + var_re, after):
                has_title = has_aria = True
        if has_title and has_aria:
            continue
        text_attr = re.search(r"\btext\s*:\s*(.+?)(,\s*\w+\s*:|\}$)", attrs, re.S)
        if text_attr:
            val = text_attr.group(1).strip()
            lit = re.fullmatch(r"""(['"])(.*)\1""", val)
            if not (lit and glyph_only(lit.group(2))):
                continue
        icon_child = bool(ICON_CALL.search(children)) or bool(re.search(r"<svg|\.svg", children))
        if icon_child and _children_have_text(children):
            continue
        empty_children = not children.strip().strip(")").strip().strip(",").strip() or \
            re.fullmatch(r"[\s,\[\]\)]*", children) is not None
        if empty_children and var_re and not text_attr:
            # filled later: icon/glyph appended or text set
            if re.search(var_re + r"\s*\.\s*(textContent|innerText)\s*=\s*(?!['\"`]\s*[^\w\s'\"`]{1,2}\s*['\"`])", after) or \
               re.search(var_re + r"\s*\.\s*(append|replaceChildren)\([^)]*\bt\(", after) or \
               re.search(var_re + r"\s*\.\s*(append|replaceChildren)\(\s*\.\.\.iconLabel", after) or \
               re.search(var_re + r"\s*\.\s*(append|replaceChildren)\(\s*el\([^)]*\btext\s*:\s*(?!['\"`][^\w\s'\"`]{1,2}['\"`])", after):
                continue
            if not (re.search(var_re + r"\s*\.\s*(append|replaceChildren|appendChild|prepend)\([^\n]*(railIcon|spriteIcon|uiIcon|svg)", after)
                    or re.search(var_re + r"\s*\.\s*(innerHTML)\s*=", after)
                    or re.search(var_re + r"\s*\.\s*textContent\s*=\s*['\"`]\s*[^\w\s'\"`]{1,2}\s*['\"`]", after)):
                continue
            icon_child = True
        if not (icon_child or text_attr):
            continue
        miss = [n for n, ok in (("title", has_title), ("aria-label", has_aria)) if not ok]
        add(m.start(), "el", f"<{tag}> no {'/'.join(miss)}")

    # document.createElement('button')
    for m in re.finditer(r"document\.createElement\(\s*['\"]button['\"]\s*\)", src):
        var = _var_before(src, m.start())
        if not var:
            continue
        after = _after(src, m.end(), 16)
        vr = re.escape(var)
        has_title = bool(re.search(vr + r"\s*\.\s*(title\s*=|setAttribute\(\s*['\"]title['\"])", after))
        has_aria = bool(re.search(vr + r"\s*\.\s*setAttribute\(\s*['\"]aria-label", after)) or \
            bool(re.search(vr + r"\.ariaLabel\s*=", after))
        if has_title and has_aria:
            continue
        text = re.search(vr + r"\s*\.\s*(textContent|innerText)\s*=\s*([^;\n]+)", after)
        icon = re.search(vr + r"\s*\.\s*(append|appendChild|replaceChildren|innerHTML)[^\n]*(railIcon|spriteIcon|uiIcon|svg)", after)
        if text:
            val = text.group(2).strip()
            lit = re.fullmatch(r"""(['"`])(.*)\1""", val)
            if not (lit and glyph_only(lit.group(2))):
                continue
        elif not icon:
            continue
        miss = [n for n, ok in (("title", has_title), ("aria-label", has_aria)) if not ok]
        add(m.start(), "dom", f"<button> no {'/'.join(miss)}")

    # template-literal markup
    for m in re.finditer(r"<(button|a)\b([^>]*)>(.*?)</\1>", src, re.S):
        attrs, inner = m.group(2), m.group(3)
        if "\n" in inner and inner.count("\n") > 6:
            continue
        has_title = re.search(r"\btitle=", attrs) is not None or "title=" in attrs
        has_aria = re.search(r"aria-label(ledby)?=", attrs) is not None
        if has_title and has_aria:
            continue
        if "data-i18n=" in attrs:
            continue
        txt = re.sub(r"\$\{[^}]*\}", lambda x: "ICON" if ICON_CALL.search(x.group(0)) or "svg" in x.group(0).lower() else "TEXT", inner)
        txt = re.sub(r"<[^>]+>", "", txt).replace("ICON", "").strip()
        icon = "ICON" in re.sub(r"\$\{[^}]*\}", lambda x: "ICON" if ICON_CALL.search(x.group(0)) else "", inner) or "<svg" in inner or "<img" in inner
        if txt and not glyph_only(txt):
            continue
        if not txt and not icon:
            continue
        miss = [n for n, ok in (("title", has_title), ("aria-label", has_aria)) if not ok]
        add(m.start(), "tpl", f"<{m.group(1)}> no {'/'.join(miss)}")
    return found


def targets(root: Path) -> list[Path]:
    st = root / "frontend" / "static"
    out = [st / "index.html"]
    out += sorted((st / "js").glob("*.js"))
    for app in sorted((root / "apps").glob("*/static")):
        out += sorted(p for p in app.rglob("*") if p.suffix in (".html", ".js"))
    return [p for p in out if p.is_file()]


def sweep(root: Path) -> list[dict]:
    rows = []
    for p in targets(root):
        rel = str(p.relative_to(root))
        hits = sweep_html(p) if p.suffix == ".html" else sweep_js(p)
        for line, msg in hits:
            if any(rel == f and snip in msg for (f, snip) in ALLOW):
                continue
            rows.append({"file": rel, "line": line, "finding": msg})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = sweep(Path(a.root))
    if a.json:
        print(json.dumps(rows, indent=1))
    else:
        for r in rows:
            print(f"{r['file']}:{r['line']}: {r['finding']}")
        print(f"{len(rows)} finding(s)")
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
