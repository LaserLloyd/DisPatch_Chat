#!/usr/bin/env bash
# DisPatch — daily thread bootstrap, run by a systemd timer (nightly).
#
# Ensures today's dated thread exists for each bot in BOTS, rotates every
# bot's avatar, and prunes yesterday's empty dated threads. That is ALL it
# does: no message is injected here. Whatever posts a daily briefing (an agent
# cron job, a script of your own) writes into the thread this creates, so the
# thread exists before anything tries to post into it.
#
# Everything is overridable from the environment; edit BOTS below for a
# permanent change.
set -uo pipefail

BASE="${LOCAL_CHAT_URL:-http://127.0.0.1:8765}"
BOTS="${LOCAL_CHAT_DAILY_BOTS:-main}"            # space-separated bot ids
DATE="${LOCAL_CHAT_DATE:-$(date +%F)}"   # overridable for testing/backfill
SYNC_HINTS="${LOCAL_CHAT_SYNC_HINTS:-$HOME/.local/bin/local-chat-sync-model-hints.py}"

# Optional hook: keep DisPatch's per-bot model labels (the "model_hint" under
# each bot name) aligned with whatever actually backs that bot, by editing
# config.yaml. Skipped silently when the script is not installed — point
# LOCAL_CHAT_SYNC_HINTS at your own if you have one.
# Best-effort and independent of the chat server
# (it only edits config.yaml on disk), so run it before the reachability gate
# and never let it fail the daily job.
if [ -f "$SYNC_HINTS" ]; then
  python3 "$SYNC_HINTS" || echo "model-hint sync failed (non-fatal)" >&2
fi

if ! curl -sf --max-time 10 "$BASE/api/health" >/dev/null; then
  echo "local-chat not reachable at $BASE" >&2
  exit 1
fi

# Rotate every bot's avatar daily — silent side effect, no thread or chat
# touched. Runs BEFORE /api/daily so today's thread is created with today's
# face already pinned. Never allowed to fail the daily job.
AVATAR_ROTATE="${HOME}/.local/bin/dispatch-avatar-rotate"
# Beside the app's other logs, NOT /tmp: /tmp is tmpfs here, so the only record
# of a failed rotation used to vanish on reboot.
ROTATE_LOG="${HOME}/.local/share/local-chat/logs/avatar-rotate.log"
mkdir -p "$(dirname "$ROTATE_LOG")"
if [ -x "$AVATAR_ROTATE" ]; then
  # Still never fatal -- a missing avatar must not stop the daily threads --
  # but no longer SILENT. `|| true` on its own hid a two-day rotation outage:
  # the job reported success every night while nothing rotated. Failures now
  # reach the journal, where `systemctl --user status` and the operator see them.
  if ! "$AVATAR_ROTATE" >> "$ROTATE_LOG" 2>&1; then
    rc=$?
    echo "avatar rotation FAILED (rc=$rc) -- last lines of $ROTATE_LOG:" >&2
    tail -n 5 "$ROTATE_LOG" >&2
  fi
fi

for bot in $BOTS; do
  out=$(curl -sf --max-time 15 "$BASE/api/daily" \
        -H 'content-type: application/json' \
        -d "{\"bot_id\":\"$bot\",\"date\":\"$DATE\"}") || { echo "daily failed for $bot" >&2; continue; }
  echo "daily thread for $bot: $(echo "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["thread"]["id"],"created" if d["created"] else "existing")')"
done

# Prune stale zero-message daily threads (keep today's + anything with activity).
# SQLite direct-path bypasses the auth gate; same DB the server writes to.
DB="${LOCAL_CHAT_DB:-$HOME/.local/share/local-chat/chats.db}"
if [ -f "$DB" ]; then
  echo "pruning zero-message daily threads older than $DATE..."
  python3 - "$DB" "$DATE" "$BOTS" <<'PRUNE_PY'
import sqlite3, sys, os
db_path, today, bots = sys.argv[1], sys.argv[2], sys.argv[3].split()
if not os.path.exists(db_path):
    print("  DB not found, skipping prune")
    sys.exit(0)
conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True)
conn.execute("PRAGMA journal_mode=WAL")
removed = 0
for bot in bots:
    cur = conn.execute('''
        SELECT t.id FROM threads t
        LEFT JOIN messages m ON m.thread_id = t.id
        WHERE t.bot_id = ? AND t.id LIKE "daily-%" AND t.id != "daily-" || ? || "-" || ?
        GROUP BY t.id HAVING COUNT(m.rowid) = 0
    ''', (bot, bot, today))
    ids = [r[0] for r in cur.fetchall()]
    for tid in ids:
        conn.execute('DELETE FROM messages WHERE thread_id = ?', (tid,))
        conn.execute('DELETE FROM threads WHERE id = ?', (tid,))
    if ids:
        conn.commit()
        print(f"  {bot}: pruned {len(ids)} zero-msg thread(s)")
        removed += len(ids)
if removed:
    print(f"  total pruned: {removed}")
else:
    print("  nothing to prune")
conn.close()
PRUNE_PY
fi
