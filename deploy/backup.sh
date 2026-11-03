#!/usr/bin/env bash
# Nightly backup: database dump + new pictures. Copy the backups folder off the server too
# (e.g. rclone to cloud storage); a backup on the same disk does not protect against disk loss.
#   crontab -e  ->  17 2 * * * /path/to/deploy/backup.sh >> /var/log/aq-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups/db backups/frames
stamp=$(date +%F)
docker compose exec -T db pg_dump -U aq aq | gzip > "backups/db/aq-$stamp.sql.gz"
# Pictures are never changed after upload, so rsync only copies new ones.
vol=$(docker volume inspect aq-platform_frames --format '{{ .Mountpoint }}')
rsync -a "$vol/" backups/frames/
find backups/db -name 'aq-*.sql.gz' -mtime +30 -delete
echo "$(date -Is) backup done"
