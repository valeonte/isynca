#!/usr/bin/env bash
# Run `isynca files sync` from cron, one run at a time, with daily log files.
#
#   ~/logs/isynca/sync-YYYY-MM-DD.log      everything, debug lines included
#   ~/logs/isynca/warnings-YYYY-MM-DD.log  warnings and errors only
#
# Log files older than KEEP_DAYS are deleted at the start of each run.
set -euo pipefail

ISYNCA=/home/fistix/repos/isynca/.pixi/envs/default/bin/isynca
SYNC_DIR=/mnt/data/iCloud
LOG_DIR="$HOME/logs/isynca"
KEEP_DAYS=10

mkdir -p "$LOG_DIR"

# Only one sync at a time. If a previous run still holds the lock, leave
# quietly; the next cron tick will try again.
exec 9>"$LOG_DIR/.sync.lock"
flock -n 9 || exit 0

find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime +"$KEEP_DAYS" -delete

today=$(date +%F)
full_log="$LOG_DIR/sync-$today.log"
warn_log="$LOG_DIR/warnings-$today.log"

# isynca writes its own log records to the two files. Its stdout (the final
# report) is appended to the full log. Its stderr repeats the log lines on
# the console and is the only place auth/config errors are printed, so it is
# kept aside and appended to both logs when the run fails.
console=$(mktemp)
trap 'rm -f "$console"' EXIT

echo "=== sync started $(date '+%F %T') ===" >>"$full_log"
status=0
"$ISYNCA" --log-file "$full_log" --warn-log "$warn_log" \
    files sync "$SYNC_DIR" >>"$full_log" 2>"$console" || status=$?

if [ "$status" -ne 0 ]; then
    {
        echo "=== sync failed with exit code $status at $(date '+%F %T') ==="
        cat "$console"
    } | tee -a "$full_log" >>"$warn_log"
fi
echo "=== sync finished $(date '+%F %T') (exit $status) ===" >>"$full_log"
exit "$status"
