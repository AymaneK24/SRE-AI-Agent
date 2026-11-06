#!/bin/sh
# Writes a compressed PostgreSQL backup to ./backups. Run it daily, and copy the result off this server.
# Example cron line: 15 2 * * * cd /opt/root-cause-console && sh deploy/backup.sh
set -eu
mkdir -p backups
file="backups/rcc-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
docker compose exec -T postgres pg_dump -U rcc -d rcc | gzip > "$file"
echo "Wrote $file"
# Keep 14 days locally.
find backups -name 'rcc-*.sql.gz' -mtime +14 -delete
