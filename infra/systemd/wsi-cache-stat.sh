#!/bin/sh
# Snapshot nginx tile cache disk usage to /run/wsi-cache-stat/nginx_cache_stat.json.
# Runs as www-data (the nginx worker user that owns the 0700 cache subdirs).
# The wsi app reads this file to display cache usage in the admin dashboard.
#
# The output directory is intentionally NOT /run/wsi — that path is the
# wsi-app IPC dir (wsi:wsi, used for backup/restore handoff) and managed
# by /etc/tmpfiles.d/wsi.conf. Sharing the RuntimeDirectory= leaf there
# would force its ownership to www-data on every cache-stat tick and
# break restore.request writes from the wsi-app worker.
#
# `set -eu` rather than the bash-only `-eo pipefail`: this is /bin/sh (dash
# on Ubuntu) and pipefail is not POSIX. -e catches an unexpected failure
# from a future edit; the existing du/find pipes are explicitly tolerant
# via `|| true` and the `${VAR:-null}` expansions, so this does not
# change behaviour on the happy or failure paths today.
set -eu

CACHE_DIR="/var/cache/nginx/wsi_dzi"
OUT="/run/wsi-cache-stat/nginx_cache_stat.json"

BYTES=""
COUNT=""
if [ -d "$CACHE_DIR" ]; then
    BYTES=$(du -sb "$CACHE_DIR" 2>/dev/null | cut -f1) || true
    COUNT=$(find "$CACHE_DIR" -type f 2>/dev/null | wc -l | tr -d '[:space:]') || true
fi

# Write to a temp sibling then atomically rename over $OUT. A bare
# `printf > "$OUT"` truncates the file and then fills it non-atomically, so the
# wsi-app process reading it on a dashboard load can race this once-a-minute
# writer and parse an empty/partial JSON document. mv(1) within the same tmpfs
# is a rename(2), so a reader always sees either the previous or the new
# complete file, never a half-written one.
TMP="$OUT.tmp"
printf '{"used_bytes":%s,"file_count":%s}\n' \
    "${BYTES:-null}" "${COUNT:-null}" > "$TMP"
mv -f "$TMP" "$OUT"
