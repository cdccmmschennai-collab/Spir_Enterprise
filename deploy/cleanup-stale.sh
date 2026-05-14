#!/usr/bin/env bash
# SPIR Dynamic — stale temp-file cleanup
#
# Runs every 2 hours via spir-cleanup.timer (systemd).
# Also safe to run manually: sudo -u spir /opt/spir_dynamic/deploy/cleanup-stale.sh
#
# WHAT IT DOES:
#   - Deletes /tmp/spir_upload_* files older than TMP_MAX_AGE_MIN (default: 120 min)
#   - Reports row storage file count and disk size (never auto-deletes JSON rows)
#   - Prints a disk usage summary to systemd journal
#
# WHAT IT NEVER DOES:
#   - Does not touch storage/extracted_rows/*.json (those require explicit user delete via UI)
#   - Does not touch any file that could be actively in use (mmin guard ensures safety)

set -euo pipefail

STORAGE_PATH="${ROWS_STORAGE_PATH:-/opt/spir_dynamic/storage/extracted_rows}"
TMP_MAX_AGE_MIN="${SPIR_TMP_MAX_AGE_MIN:-120}"

# ── Temp upload cleanup ────────────────────────────────────────
stale_count=$(find /tmp -maxdepth 1 -name "spir_upload_*" -type f \
    -mmin "+${TMP_MAX_AGE_MIN}" 2>/dev/null | wc -l)

if [ "$stale_count" -gt 0 ]; then
    find /tmp -maxdepth 1 -name "spir_upload_*" -type f \
        -mmin "+${TMP_MAX_AGE_MIN}" -delete
    echo "[spir-cleanup] Removed ${stale_count} stale temp upload(s) older than ${TMP_MAX_AGE_MIN}min"
else
    echo "[spir-cleanup] No stale temp uploads"
fi

# ── Row storage stats (informational — no deletions) ──────────
if [ -d "$STORAGE_PATH" ]; then
    json_count=$(find "$STORAGE_PATH" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)
    storage_size=$(du -sh "$STORAGE_PATH" 2>/dev/null | cut -f1 || echo "?")
    echo "[spir-cleanup] Row storage: ${json_count} file(s), ${storage_size} total"
else
    echo "[spir-cleanup] Row storage directory not found: ${STORAGE_PATH}"
fi

# ── Disk summary ───────────────────────────────────────────────
disk_info=$(df -h / | tail -1 | awk '{print $5 " used (" $4 " free)"}')
echo "[spir-cleanup] Disk: ${disk_info}"
