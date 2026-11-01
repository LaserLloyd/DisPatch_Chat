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


# --------------------------------------------------------------------------- #
# Repository-wide lint (issue #20): frontend JS string literals, index.html,
# apps/*/static, README, CHANGELOG, docs/*.md (not docs/design/).
# --------------------------------------------------------------------------- #

_BAD = FORBIDDEN_LABELS[0]

# Repository-relative paths that carried a forbidden label when this lint was
# widened and that this WP does not own (see ~/.cache/cfp/42/i43/branding-hits.txt).
# Each stays xfail(strict=True) until WP-D1 / WP-INT fix the file and delete the
# path here: a fixed file then XPASSes and the strict xfail turns the run red.
KNOWN_HITS: frozenset[str] = frozenset()


def _repo_scan_params():
    out = []
    for p in branding.repo_scan_files(REPO):
        rel = p.relative_to(REPO).as_posix()
        marks = [pytest.mark.xfail(strict=True, reason="known branding hit, see branding-hits.txt")] \
            if rel in KNOWN_HITS else []
        out.append(pytest.param(p, id=rel, marks=marks))
    return out


def _plant(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_the_repo_scan_finds_a_planted_label_in_a_readme(tmp_path):
    _plant(tmp_path, "README.md", f"# Demo\n\nRuns on {_BAD} out of the box.\n")
    [v] = branding.scan_repo(tmp_path)
    assert v.source.endswith("README.md") and v.line == 3 and v.match == _BAD


def test_the_repo_scan_finds_a_planted_label_in_a_js_string_literal(tmp_path):
    _plant(tmp_path, "frontend/static/js/picker.js",
           "// a comment is not shipped text: " + _BAD + "\n"
           "const re = /['\"]/g;\n"
           "export const LABEL = 'Agents';\n"
           "export const BACKEND = \"" + _BAD + " Agent\";\n"
           "const t = `line one\n" + _BAD + "`;\n")
    found = branding.scan_repo(tmp_path)
    assert [(v.line, v.match) for v in found] == [(4, _BAD), (6, _BAD)], found


def test_every_widened_surface_is_scanned(tmp_path):
    rels = ["frontend/static/index.html", "frontend/static/js/sub/deep.js",
            "apps/demo/static/index.html", "apps/demo/static/x/app.js", "apps/demo/locales/en.json",
            "CHANGELOG.md", "docs/guide.md", "docs/sub/page.md",
            "frontend/static/locales/en.json"]
    for rel in rels:
        _plant(tmp_path, rel, f'"{_BAD}"\n')
    _plant(tmp_path, "docs/design/note.md", f"{_BAD}\n")     # design notes: excluded
    got = sorted({Path(v.source).relative_to(tmp_path).as_posix()
                  for v in branding.scan_repo(tmp_path)})
    assert got == sorted(rels)


def test_the_allowlist_excuses_quoted_history_and_reports_stale_entries(tmp_path):
    _plant(tmp_path, "CHANGELOG.md", f"- Renamed the old \"{_BAD}\" label (quoted history)\n"
                                     f"- New text still says {_BAD}\n")
    allow = (("CHANGELOG.md", "quoted history"), ("CHANGELOG.md", "no such line"))
    [v] = branding.scan_repo(tmp_path, allow)
    assert v.line == 2
    assert branding.stale_allowlist_entries(tmp_path, allow) == [("CHANGELOG.md", "no such line")]


def test_the_shipped_allowlist_has_no_stale_entries():
    assert branding.stale_allowlist_entries(REPO) == []


def test_the_repo_scan_covers_the_real_surfaces():
    rels = {p.relative_to(REPO).as_posix() for p in branding.repo_scan_files(REPO)}
    for must in ("README.md", "CHANGELOG.md", "frontend/static/index.html",
                 "frontend/static/js/agents.js", "docs/agents.md"):
        assert must in rels, must
    assert not any(r.startswith("docs/design/") for r in rels)
    assert any(r.startswith("apps/") and "/static/" in r for r in rels)


@pytest.mark.parametrize("path", _repo_scan_params() if hasattr(branding, "repo_scan_files") else [])
def test_no_repo_surface_uses_a_forbidden_agent_label(path):
    found = branding.scan_repo_file(REPO, path)
    assert found == [], [f"{v.source}:{v.line}: {v.match}" for v in found]


def test_known_hits_name_real_scanned_files():
    rels = {p.relative_to(REPO).as_posix() for p in branding.repo_scan_files(REPO)}
    assert KNOWN_HITS <= rels, sorted(KNOWN_HITS - rels)


# --------------------------------------------------------------------------- #
# CI tooling guards (issue #26 prep): scripts/ci-local.sh mirrors ci.yml and
# stays local. Kept next to the branding lint because both are repo hygiene.
# --------------------------------------------------------------------------- #

CI_YML = REPO / ".github" / "workflows" / "ci.yml"
CI_LOCAL = REPO / "scripts" / "ci-local.sh"


def _ci_local_text() -> str:
    assert CI_LOCAL.is_file(), "scripts/ci-local.sh is missing"
    return CI_LOCAL.read_text(encoding="utf-8")


def _ci_local_jobs(text: str) -> tuple[set[str], set[str]]:
    import re
    jobs = set(re.search(r"^JOBS=\(([^)]*)\)", text, re.M).group(1).split())
    skipped = set(re.findall(r"^\s*\[([\w.-]+)\]=", text, re.M))
    return jobs, skipped


def test_ci_local_mirrors_every_ci_yml_job():
    import yaml
    wf = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    text = _ci_local_text()
    jobs, skipped = _ci_local_jobs(text)
    for job_id, job in wf["jobs"].items():
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        names = [f"{job_id}-{v}" for v in matrix["python"]] if "python" in matrix else [job_id]
        for name in names:
            assert name in jobs, f"ci.yml job {name} has no local counterpart"
            assert name in skipped or f"job_{name}()" in text, f"{name}: neither run nor skipped"
    for name in skipped:
        assert f"job_{name}()" not in text, f"{name} is both skipped and run"


def test_ci_local_never_pushes_publishes_or_builds_images():
    import re
    text = re.sub(r"<<'EOF'\n.*?\nEOF\n", "\n", _ci_local_text(), flags=re.S)   # help text
    code = "\n".join(line.strip() for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    for bad in (r"\bgit\s+(push|remote|tag|fetch|pull)\b",
                r"(^|[;&|(]|\$\()\s*(docker|podman|buildah)\s",      # in command position
                r"\bbuild-push\b", r"--push\b", r"\bdispatchctl\b", r"\bgh\s",
                r"\bimagetools\b"):
        assert not re.search(bad, code, re.M), bad


def test_ci_local_keeps_temp_files_off_tmp_and_never_sets_the_data_dir():
    import re
    text = _ci_local_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    tmp_uses = [line for line in code if re.search(r"(?<![\w}])/tmp\b", line)]
    assert all(re.search(r"^\s*/tmp/\*\)\s*die\b", line) for line in tmp_uses), tmp_uses
    assert "unset DISPATCH_DATA_DIR" in text
    assert not re.search(r"^\s*(export\s+)?DISPATCH_DATA_DIR=", text, re.M)
    assert re.search(r"--basetemp=\"\$CI_TMP/", text), "pytest basetemp must live under CI_TMP"
    assert '/tmp/' not in CI_YML.read_text(encoding="utf-8"), "ci.yml should use $RUNNER_TEMP"


def test_ci_local_refuses_a_scratch_dir_under_tmp(tmp_path):
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash on this platform")
    link = tmp_path / "to-tmp"
    link.symlink_to("/tmp")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CI_LOCAL_TMP": str(link / "ci-x")}
    r = subprocess.run([bash, str(CI_LOCAL), "--only", "privacy"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "under /tmp" in r.stderr, (r.returncode, r.stderr)
    assert not Path("/tmp/ci-x").exists()
