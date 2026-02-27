#!/bin/bash
# Loop-based scheduler to avoid cron's setpgid in containers (setpgid: Operation not permitted)
set -e

# Handle signals to exit immediately
trap "exit 0" SIGTERM SIGINT

INTERVAL="${BACKUP_INTERVAL_SECONDS:-21600}"  # default 6 hours

echo "Backup runner started (interval=${INTERVAL}s)."
while true; do
    /backup.sh >> /var/log/backup.log 2>&1 &
    wait $!
    
    # Sleep in background and wait so signals can be caught immediately
    sleep "$INTERVAL" &
    wait $!
done


