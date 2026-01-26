#!/usr/bin/env bash
set -euo pipefail
echo "[backup] start $(date --iso-8601=seconds)"
mkdir -p /app/data/backups
tar -czf /app/data/backups/backup-$(date +%Y%m%dT%H%M%S).tar.gz /app/data || true
echo "[backup] finished"
