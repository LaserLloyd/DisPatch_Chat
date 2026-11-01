#!/usr/bin/env bash
# Jobs are called indirectly ("job_$name"), which shellcheck cannot follow.
# shellcheck disable=SC2329
# Run every job of .github/workflows/ci.yml on this machine, in ci.yml order.
#
#   scripts/ci-local.sh                 # every job
#   scripts/ci-local.sh --only lint,locales
#   scripts/ci-local.sh --skip test-3.12,audit
#   scripts/ci-local.sh --list          # the jobs and what each one runs
#
# What it will NOT do, by design:
#   * push, tag, or talk to a git remote (it only reads the working tree);
#   * build or publish a container image (the `docker` job and the whole of
#     docker-publish.yml are reported as "skipped locally");
#   * write under /tmp (a small, quota'd tmpfs on many desktops): every temp
#     file, venv and pytest basetemp lives under $CI_LOCAL_TMP;
#   * read or set DISPATCH_DATA_DIR (it is unset, so no test can reach a real
#     install's data).
#
# Environment:
#   CI_LOCAL_TMP            scratch root (default: ${XDG_CACHE_HOME:-~/.cache}/dispatch-ci-local).
#                           Refused when it resolves under /tmp.
#   CI_LOCAL_PYTEST_TIMEOUT seconds per backend suite run (default 3600). The suite can
#                           print its summary and then hang in teardown; a run that timed out
#                           AFTER a clean summary line counts as green and says so.
#   CI_LOCAL_NPM_CI=1       run `npm ci` even when frontend/node_modules already exists
#                           (never through a symlinked node_modules).
#
# Keep this file in step with ci.yml: every job there is either a function
# below or a row in SKIPPED_JOBS with the reason.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CI_TMP="${CI_LOCAL_TMP:-${XDG_CACHE_HOME:-$HOME/.cache}/dispatch-ci-local}"
PYTEST_TIMEOUT="${CI_LOCAL_PYTEST_TIMEOUT:-3600}"

# ci.yml job id -> local job names (a matrix job expands to one name per leg).
JOBS=(privacy test-3.12 test-3.13 windows lint locales audit docker)
declare -A SKIPPED_JOBS=(
  [windows]="needs a native Windows runner (windows-latest); run it on GitHub"
  [docker]="builds container images (QEMU multi-arch + the voice add-on image); run it on GitHub"
)
# docker-publish.yml is never run here: it builds AND publishes release images.

die() { echo "ci-local: $*" >&2; exit 2; }

list_jobs() {
  cat <<'EOF'
privacy     python3 scripts/scrub_check.py
test-3.12   backend: uv sync --frozen --python 3.12; uv run pytest -q   (own venv + basetemp)
test-3.13   backend: uv sync --frozen --python 3.13; uv run pytest -q   (own venv + basetemp)
windows     skipped locally: needs a native Windows runner
lint        ruff@0.17.0; forgeui vendor --check; node --check every module; npm ci; npm run test:ci
locales     scripts/check-locales.py; scripts/check-locales.py --selftest
audit       uv export --frozen --all-extras | pip-audit==2.10.1 --strict
docker      skipped locally: builds container images
(docker-publish.yml is never run locally: it publishes release images)
EOF
}

ONLY="" SKIP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="${2:-}"; shift 2 ;;
    --only=*) ONLY="${1#*=}"; shift ;;
    --skip) SKIP="${2:-}"; shift 2 ;;
    --skip=*) SKIP="${1#*=}"; shift ;;
    --list) list_jobs; exit 0 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done
for name in ${ONLY//,/ } ${SKIP//,/ }; do
  [[ " ${JOBS[*]} " == *" $name "* ]] || die "unknown job: $name (jobs: ${JOBS[*]})"
done

# Refuse /tmp BEFORE creating anything: resolve symlinks on the path as given.
CI_TMP="$(realpath -m -- "$CI_TMP")"
case "$CI_TMP/" in
  /tmp/*) die "CI_LOCAL_TMP resolves under /tmp ($CI_TMP); pick a directory on a real disk" ;;
esac
mkdir -p "$CI_TMP" || die "cannot create $CI_TMP"
export RUNNER_TEMP="$CI_TMP/runner"
export TMPDIR="$CI_TMP/tmp"
mkdir -p "$RUNNER_TEMP" "$TMPDIR"
unset DISPATCH_DATA_DIR
export CI=true
cd "$REPO" || die "cannot cd to $REPO"

declare -A RESULT=()
declare -A NOTE=()

step() {   # step <description> <command...>
  local desc="$1"; shift
  echo "  - $desc"
  "$@"
}

job_privacy() {
  step "Scan the tree for private data" uv run --no-project --python 3.13 python scripts/scrub_check.py
}

run_backend_suite() {   # <python version>
  local py="$1" log="$CI_TMP/pytest-$1.log" rc
  export UV_PROJECT_ENVIRONMENT="$CI_TMP/venv-py$py"
  (cd backend && step "Sync dependencies" uv sync --frozen --python "$py") || return 1
  echo "  - Run the test suite (log: $log)"
  rm -rf "$CI_TMP/basetemp-$py"
  (cd backend && timeout --signal=TERM --kill-after=30 "$PYTEST_TIMEOUT" \
      uv run pytest -q -p no:cacheprovider --basetemp="$CI_TMP/basetemp-$py") 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  unset UV_PROJECT_ENVIRONMENT
  rm -rf "$CI_TMP/basetemp-$py"
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    # Hung after the summary? Accept only a summary with no failure or error.
    local summary
    summary="$(grep -E '^(=+ )?[0-9]+ (passed|failed|error).* in [0-9.]+s' "$log" | tail -1)"
    if [ -n "$summary" ] && ! grep -qE '[0-9]+ (failed|errors?)' <<<"$summary"; then
      NOTE["test-$py"]="hung on exit after a clean summary: $summary"
      return 0
    fi
    NOTE["test-$py"]="timed out after ${PYTEST_TIMEOUT}s"
  fi
  return "$rc"
}
job_test-3.12() { run_backend_suite 3.12; }
job_test-3.13() { run_backend_suite 3.13; }

job_lint() {
  local f venv="$CI_TMP/forgeui-venv"
  (cd backend && step "Ruff" uvx ruff@0.17.0 check app tests) || return 1
  echo "  - Vendored ForgeUI bundle matches forgeui.json"
  rm -rf "$venv"
  uv venv -q --python 3.12 "$venv" &&
    uv pip install -q --python "$venv" "themeforge @ git+https://github.com/LaserLloyd/ThemeForge@9d5a2c25ad641423df3615a38c7642011a91833d" &&
    "$venv/bin/python" -m forgeui vendor --check || return 1
  echo "  - Frontend syntax check"
  for f in frontend/static/js/*.js frontend/static/sw.js; do
    cp "$f" "$RUNNER_TEMP/check.mjs"
    node --check "$RUNNER_TEMP/check.mjs" || { echo "::error file=$f::syntax error"; return 1; }
  done
  echo "All frontend modules parse."
  echo "  - Install test dependencies"
  if [ -L frontend/node_modules ]; then
    echo "    frontend/node_modules is a symlink (shared install): npm ci skipped, reused as is"
  elif [ -d frontend/node_modules ] && [ "${CI_LOCAL_NPM_CI:-0}" != 1 ]; then
    echo "    frontend/node_modules exists: npm ci skipped (CI_LOCAL_NPM_CI=1 to reinstall)"
  else
    (cd frontend && npm ci --no-audit --no-fund) || return 1
  fi
  (cd frontend && step "Frontend tests" npm run test:ci)
}

job_locales() {
  step "Validate locale files" uv run --no-project --python 3.13 python scripts/check-locales.py || return 1
  step "Locale HTML allowlist self-test" uv run --no-project --python 3.13 python scripts/check-locales.py --selftest
}

job_audit() {
  echo "  - pip-audit the locked set"
  (cd backend &&
    uv export --frozen --no-hashes --no-emit-project --all-extras > "$RUNNER_TEMP/requirements-audit.txt" &&
    uvx --python 3.12 pip-audit==2.10.1 -r "$RUNNER_TEMP/requirements-audit.txt" --no-deps --disable-pip --strict)
}

selected() {
  local j="$1"
  if [ -n "$ONLY" ] && [[ ",$ONLY," != *",$j,"* ]]; then return 1; fi
  if [ -n "$SKIP" ] && [[ ",$SKIP," == *",$j,"* ]]; then return 1; fi
  return 0
}

for j in "${JOBS[@]}"; do
  selected "$j" || { RESULT[$j]="not selected"; continue; }
  if [ -n "${SKIPPED_JOBS[$j]:-}" ]; then
    RESULT[$j]="skipped locally"; NOTE[$j]="${SKIPPED_JOBS[$j]}"
    continue
  fi
  echo "=== $j ==="
  if "job_$j"; then RESULT[$j]="green"; else RESULT[$j]="RED"; fi
  echo "=== $j: ${RESULT[$j]} ==="
done

echo
echo "ci-local summary ($REPO, scratch $CI_TMP)"
rc=0
for j in "${JOBS[@]}"; do
  printf '  %-10s %-16s %s\n' "$j" "${RESULT[$j]}" "${NOTE[$j]:-}"
  [ "${RESULT[$j]}" = "RED" ] && rc=1
done
echo "  docker-publish.yml: never run locally (it publishes release images)"
exit "$rc"
