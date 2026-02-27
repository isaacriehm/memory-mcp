#!/bin/bash
set -e

REPO_DIR="/backup/repo"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
DUMP_FILE="memory.sql"

echo "[$TIMESTAMP] Starting backup..."

# Clone repo if not exists, otherwise pull
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "Cloning backup repo..."
    git clone "https://${GITHUB_PAT}@github.com/${GITHUB_REPO}.git" "$REPO_DIR"
else
    cd "$REPO_DIR"
    git pull --rebase
fi

cd "$REPO_DIR"

# Configure git
git config user.email "memory-backup@localhost"
git config user.name "Memory Backup"

# Dump the database
echo "Dumping database..."
PGPASSWORD="$DB_PASSWORD" pg_dump \
    -h memory-db \
    -U memory \
    -d memory \
    --no-owner \
    --no-acl \
    -f "$DUMP_FILE"

# Commit and push if there are changes
if git diff --quiet "$DUMP_FILE" 2>/dev/null && git ls-files --error-unmatch "$DUMP_FILE" 2>/dev/null; then
    echo "No changes since last backup, skipping commit."
else
    git add "$DUMP_FILE"
    git commit -m "backup: $TIMESTAMP"
    git push "https://${GITHUB_PAT}@github.com/${GITHUB_REPO}.git" main
    echo "[$TIMESTAMP] Backup pushed successfully."
fi
