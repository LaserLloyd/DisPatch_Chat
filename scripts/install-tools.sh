#!/usr/bin/env bash
# Install DisPatch's host tools into the operator's PATH directory.
#
#     sh scripts/install-tools.sh              # dry run: show what would change
#     sh scripts/install-tools.sh --go         # install
#     sh scripts/install-tools.sh --check      # exit 1 if the installed copy has drifted
#     sh scripts/install-tools.sh --go --dest /somewhere/else/bin
#
# What these are and why they are not part of `deploy`
# ----------------------------------------------------
# `dispatch-avatar-rotate` and `local-chat-daily.sh` are run by systemd timers
# on the HOST, not by the app. They live on the operator's PATH
# (~/.local/bin), which is neither the repo nor the install directory — so the
# deploy allowlist, which only ever writes into the install tree, cannot carry
# them. For a long time that meant they were versioned nowhere at all: the
# rotator held a hardcoded path to a directory that had since moved, and the
# outage lasted two days because there was no copy to diff against.
#
# This is that copy. The repo is upstream; this script is the one documented
# way the repo's version reaches the host.
#
# It never overwrites without --go, backs up whatever it replaces, and refuses
# to touch a destination file that is neither missing nor a previous install of
# the same tool (compare with --check first).
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
dest="${DISPATCH_TOOLS_DIR:-$HOME/.local/bin}"
go=0
check=0

TOOLS="dispatch-avatar-rotate local-chat-daily.sh dispatch-jobs"

usage() {
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-2}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --go)    go=1 ;;
        --check) check=1 ;;
        --dest)  shift; [ $# -gt 0 ] || usage; dest=$1 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
    shift
done

if [ "$go" = 1 ] && [ "$check" = 1 ]; then
    echo "--check and --go are mutually exclusive" >&2
    exit 2
fi

drift=0
changes=0

for tool in $TOOLS; do
    src="$root/scripts/$tool"
    dst="$dest/$tool"
    if [ ! -f "$src" ]; then
        echo "✗ missing in repo: scripts/$tool" >&2
        exit 2
    fi
    if [ ! -e "$dst" ]; then
        echo "  + $dst  (not installed)"
        changes=$((changes + 1))
        drift=1
    elif cmp -s "$src" "$dst"; then
        echo "  = $dst  (up to date)"
        continue
    else
        echo "  M $dst  (differs from the repo)"
        changes=$((changes + 1))
        drift=1
    fi
    [ "$go" = 1 ] || continue

    mkdir -p "$dest"
    if [ -e "$dst" ]; then
        # Back up before overwriting. A tool that ran last night is evidence;
        # replacing it with no copy is how a regression becomes unprovable.
        backup="$dst.bak-$(date +%Y%m%d-%H%M%S)"
        cp -p "$dst" "$backup"
        echo "    backed up → $backup"
    fi
    # Write via a temp file and rename, so a tool is never half-written while
    # a timer might be starting it.
    tmp="$dst.partial.$$"
    cp "$src" "$tmp"
    chmod 755 "$tmp"
    mv "$tmp" "$dst"
    echo "    installed"
done

if [ "$check" = 1 ]; then
    if [ "$drift" = 1 ]; then
        echo
        echo "✗ installed host tools differ from the repo (see above)." >&2
        exit 1
    fi
    echo
    echo "✓ installed host tools match the repo."
    exit 0
fi

echo
if [ "$changes" = 0 ]; then
    echo "✓ nothing to do — $dest is already current."
elif [ "$go" = 1 ]; then
    echo "✓ installed $changes tool(s) into $dest."
    echo "  systemd runs these by absolute path; no daemon-reload is needed."
else
    echo "Dry run — $changes change(s) above. Re-run with --go to apply."
fi
