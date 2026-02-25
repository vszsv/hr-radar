#!/bin/bash
# Daily SQLite backup with 7-day retention
BACKUP_DIR="/opt/hr-radar/backups"
DATE=$(date +%Y-%m-%d)

for db in /opt/hr-radar/data/*.db; do
    [ -f "$db" ] || continue
    name=$(basename "$db" .db)
    sqlite3 "$db" ".backup '${BACKUP_DIR}/${name}_${DATE}.db'"
done

# Remove backups older than 7 days
find "$BACKUP_DIR" -name "*.db" -mtime +7 -delete
