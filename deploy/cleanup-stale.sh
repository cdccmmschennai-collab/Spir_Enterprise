#!/usr/bin/env bash
# SPIR Dynamic — stale temp-file cleanup (systemd timer, every 2 hours)
#
# Runs every 2 hours via spir-cleanup.timer to catch anything the Celery
# task missed (crashed worker, power loss, etc.).
#
# The Celery Beat lifecycle_cleanup_task handles:
#   - JSON retention (14-day expiry)            → runs at 02:00 UTC daily
#   - Orphan JSON cleanup (DB-verified)         → runs at 02:00 UTC daily
#   - Disk metrics (Prometheus Gauge refresh)   → runs at 02:00 UTC daily
#
# This script handles:
#   - Stale batch_upload files (safety net)     → every 2 hours
#   - Stale /tmp/spir_* files (legacy path)     → every 2 hours
#   - Disk usage summary to journal             → every 2 hours
#
# Manual run:
#   sudo -u spir /opt/spir_dynamic/deploy/cleanup-stale.sh
#   sudo systemctl start spir-cleanup.service   # run as the systemd service

set -euo pipefail

UPLOAD_DIR="${BATCH_UPLOAD_DIR:-/opt/spir_dynamic/storage/batch_uploads}"
STORAGE_PATH="${ROWS_STORAGE_PATH:-/opt/spir_dynamic/storage/extracted_rows}"
UPLOAD_STALE_H="${CLEANUP_UPLOAD_STALE_HOURS:-24}"
TMP_MAX_AGE_MIN="${SPIR_TMP_MAX_AGE_MIN:-120}"

STALE_UPLOAD_MIN=$(( UPLOAD_STALE_H * 60 ))

# ── 1. Batch upload staging cleanup ──────────────────────────────
if [ -d "$UPLOAD_DIR" ]; then
    stale_count=$(find "$UPLOAD_DIR" -maxdepth 1 -type f \
        -mmin "+${STALE_UPLOAD_MIN}" 2>/dev/null | wc -l)
    if [ "$stale_count" -gt 0 ]; then
        find "$UPLOAD_DIR" -maxdepth 1 -type f \
            -mmin "+${STALE_UPLOAD_MIN}" -delete
        echo "[spir-cleanup] Removed ${stale_count} stale batch upload(s) older than ${UPLOAD_STALE_H}h"
    else
        echo "[spir-cleanup] No stale batch uploads (threshold: ${UPLOAD_STALE_H}h)"
    fi
else
    echo "[spir-cleanup] Batch upload dir not found: ${UPLOAD_DIR}"
fi

# ── 2. Legacy /tmp/spir_* cleanup (belt-and-suspenders) ─────────
tmp_stale=$(find /tmp -maxdepth 1 -name "spir_*" -type f \
    -mmin "+${TMP_MAX_AGE_MIN}" 2>/dev/null | wc -l)
if [ "$tmp_stale" -gt 0 ]; then
    find /tmp -maxdepth 1 -name "spir_*" -type f \
        -mmin "+${TMP_MAX_AGE_MIN}" -delete
    echo "[spir-cleanup] Removed ${tmp_stale} stale /tmp/spir_* file(s)"
fi

# ── 3. Storage stats (informational — Celery task manages deletion) ─
if [ -d "$STORAGE_PATH" ]; then
    json_count=$(find "$STORAGE_PATH" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)
    storage_size=$(du -sh "$STORAGE_PATH" 2>/dev/null | cut -f1 || echo "?")
    echo "[spir-cleanup] Extracted JSON: ${json_count} file(s), ${storage_size} total"
else
    echo "[spir-cleanup] Extracted rows dir not found: ${STORAGE_PATH}"
fi

# ── 4. Disk summary ───────────────────────────────────────────────
disk_info=$(df -h / | tail -1 | awk '{print $5 " used (" $4 " free)"}')
echo "[spir-cleanup] Root disk: ${disk_info}"
