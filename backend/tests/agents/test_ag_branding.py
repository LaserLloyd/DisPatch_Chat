"""Branding lint (gate G17) and the label rules (RESEARCH.md F3, section 5 item 3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import branding
from app.agents.branding import (
    FORBIDDEN_LABELS,
    allowed_labels,
    is_allowed_agent_label,
    scan_locale_file,
    scan_text,
    scan_tree,
)

REPO = Path(__file__).resolve().parents[3]
LOCALES = sorted((REPO / "frontend" / "static" / "locales").glob("*.json"))
AGENTS_PKG = REPO / "backend" / "app" / "agents"
AGENT_DOCS = REPO / "docs" / "agents"


def test_the_allowed_labels_are_the_three_from_the_brief():
    labels = allowed_labels("Acme Agent")
    assert set(labels) == {"Claude Agent", "Claude", "Acme Agent Powered by Claude"}
    assert "Agents" in labels["Claude"]


def test_label_validation():
    assert is_allowed_agent_label("Claude Agent")
    assert not is_allowed_agent_label("Claude")
    assert is_allowed_agent_label("Claude", in_agents_menu=True)
    assert is_allowed_agent_label("DisPatch Agent Powered by Claude")
    assert not is_allowed_agent_label("Claude Assistant")
    assert not is_allowed_agent_label("Anthropic Agent")


@pytest.mark.parametrize("bad", FORBIDDEN_LABELS)
def test_every_forbidden_label_is_detected(bad):
    assert scan_text(f"Pick a backend: {bad}")
    assert scan_text(bad.upper()) and scan_text(bad.lower())
    assert scan_text(bad.replace(" ", " ")), "a non-breaking space is not a loophole"


@pytest.mark.parametrize("fine", [
    "Claude Agent", "Claude", "DisPatch Agent Powered by Claude", "CLAUDE_CODE_OAUTH_TOKEN",
    "claude-code", "Claudette", "claude --bare -p", "Use Claude for code review",
])
def test_allowed_wording_and_identifiers_are_not_flagged(fine):
    assert scan_text(fine) == []


def test_the_scanner_reports_file_line_and_match(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"menu": {"agent": "Run in ' + FORBIDDEN_LABELS[0] + '"}}', encoding="utf-8")
    [v] = scan_locale_file(f)
    assert v.source.endswith("x.json") and v.match == FORBIDDEN_LABELS[0] and v.line == 1


def test_this_module_does_not_contain_the_strings_it_forbids():
    assert scan_text(Path(branding.__file__).read_text(encoding="utf-8")) == []


def test_there_are_locale_files_to_scan():
    assert LOCALES, "frontend/static/locales/*.json not found - the lint would pass vacuously"


@pytest.mark.parametrize("path", LOCALES, ids=lambda p: p.name)
def test_no_ui_string_in_the_locales_uses_a_forbidden_agent_label(path):
    assert scan_locale_file(path) == []


def test_the_agents_package_and_its_docs_use_no_forbidden_label():
    assert any(AGENTS_PKG.rglob("*.py"))
    found = scan_tree(AGENTS_PKG)
    if AGENT_DOCS.is_dir():
        found += scan_tree(AGENT_DOCS)
    assert found == [], [f"{v.source}:{v.line}: {v.match}" for v in found]
