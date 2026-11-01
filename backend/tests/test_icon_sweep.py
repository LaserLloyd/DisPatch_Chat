"""Every icon-only control in the UI has a tooltip AND an accessible name.

`tests/e2e/icon_sweep.py` reads index.html, js/*.js and the apps' static files and lists
every button / link / role=button whose face is only an icon or a glyph and that lacks a
`title` or an `aria-label` (or the data-i18n-attr that sets them). A new icon-only
control without both fails here, so a person who does not know the icons can always
hover (or listen) to learn what a control does. Exceptions go in the sweep's ALLOW map
with a reason, or on the line as `icon-sweep: ok (<reason>)`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SWEEP = REPO / "tests" / "e2e" / "icon_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("icon_sweep", SWEEP)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_no_icon_only_control_without_tooltip_and_name():
    rows = _load().sweep(REPO)
    assert rows == [], "icon-only controls without title + aria-label:\n" + "\n".join(
        f"{r['file']}:{r['line']}: {r['finding']}" for r in rows)


def _tree(tmp_path: Path, html: str = "", js: str = "") -> Path:
    st = tmp_path / "frontend" / "static"
    (st / "js").mkdir(parents=True)
    (st / "index.html").write_text(f"<!doctype html><body>{html}</body>", encoding="utf8")
    (st / "js" / "x.js").write_text(js, encoding="utf8")
    (tmp_path / "apps").mkdir()
    return tmp_path


def test_sweep_catches_each_shape(tmp_path):
    """The guard is only worth something if it sees the shapes the app uses."""
    root = _tree(
        tmp_path,
        html='<button id="a"><svg class="rail-icon"><use href="#x"/></svg></button>'
             '<button id="b" aria-label="Close" data-i18n-attr="aria-label=common.close">'
             '<svg class="rail-icon"></svg></button>',
        js="const a = el('button', { class: 'icon-btn' }, [railIcon(RAIL_ICONS.close)]);\n"
           "const b = el('a', { href: u, title: t('x.y') }, [spriteIcon('eye', 16)]);\n"
           "const c = document.createElement('button');\nc.append(railIcon(RAIL_ICONS.copy));\n"
           "const d = `<button class=\"x\">${spriteIconHtml('x', 14)}</button>`;\n"
           "const e = el('button', { class: 'x', text: '×' });\n",
    )
    rows = _load().sweep(root)
    where = sorted((r["file"].rsplit("/", 1)[-1], r["line"]) for r in rows)
    assert where == [("index.html", 1), ("index.html", 1), ("x.js", 1), ("x.js", 2),
                     ("x.js", 3), ("x.js", 5), ("x.js", 6)], rows


def test_sweep_passes_labelled_and_text_controls(tmp_path):
    root = _tree(
        tmp_path,
        html='<button title="Close" aria-label="Close"><svg></svg></button>'
             '<button data-i18n-attr="title=common.close;aria-label=common.close"><svg></svg></button>'
             '<button><svg></svg> <span data-i18n="common.save">Save</span></button>'
             '<button>Save</button>',
        js="const a = el('button', { title: t('k'), 'aria-label': t('k') }, [railIcon(RAIL_ICONS.close)]);\n"
           "const b = el('button', { class: 'x' }, [spriteIcon('plus', 14), ` ${t('k.add')}`]);\n"
           "const c = el('button', { class: 'x', text: t('k.go') });\n"
           "const d = el('button', { class: 'x' }, [railIcon(RAIL_ICONS.close)]); // icon-sweep: ok (test)\n",
    )
    assert _load().sweep(root) == []
