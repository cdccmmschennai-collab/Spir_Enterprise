#!/usr/bin/env bash
# ============================================================
# SPIR Dynamic — Operational Commands Reference
# Run sections interactively. Do NOT pipe this to bash blindly.
# ============================================================


# ══════════════════════════════════════════════════════════════
# DAILY OPERATIONAL CHECKS
# ══════════════════════════════════════════════════════════════

# 1. Service health (systemd + PM2)
systemctl is-active spir-api spir-worker
pm2 status

# 2. FastAPI health endpoint (bypass nginx, hit uvicorn directly)
curl -sf http://127.0.0.1:8000/health | python3 -m json.tool

# 3. Last 50 log lines — API and worker together
journalctl -u spir-api -u spir-worker -n 50 --no-pager

# 4. Errors in last 24 hours
journalctl -u spir-api -u spir-worker --since "24h ago" -p err --no-pager

# 5. Disk space
df -h / /opt/spir_dynamic 2>/dev/null || df -h /
du -sh /opt/spir_dynamic/storage/extracted_rows/

# 6. RAM and swap
free -h

# 7. Active temp upload files currently on disk
ls -lhrt /tmp/spir_upload_* 2>/dev/null || echo "No active uploads in /tmp"

# 8. PostgreSQL alive
systemctl is-active postgresql || pg_isready -h localhost

# 9. Redis alive (when CELERY_ENABLED=true)
redis-cli ping


# ══════════════════════════════════════════════════════════════
# WEEKLY MAINTENANCE CHECKS
# ══════════════════════════════════════════════════════════════

# 1. Extraction event summary — all completed extractions in last 7 days
journalctl -u spir-api --since "7 days ago" --no-pager | grep "EXTRACTION_EVENT" | tail -50

# 2. Slow extractions (>120s warning from pipeline)
journalctl -u spir-api --since "7 days ago" --no-pager | grep -E "Slow extraction|extract_dur=[0-9]{3}"

# 3. Timeout events
journalctl -u spir-api --since "7 days ago" --no-pager | grep "status=timeout"

# 4. Out-of-memory events
journalctl -u spir-api --since "7 days ago" --no-pager | grep -iE "memory|oom|507"

# 5. Memory high-water marks per extraction
journalctl -u spir-api --since "7 days ago" --no-pager | grep "mem_rss_mb"

# 6. Stale /tmp uploads older than 2 hours (should be 0 if cleanup timer is running)
find /tmp -maxdepth 1 -name "spir_upload_*" -mmin +120 2>/dev/null | wc -l

# 7. JSON row storage — file count and size
find /opt/spir_dynamic/storage/extracted_rows/ -name "*.json" | wc -l
du -sh /opt/spir_dynamic/storage/extracted_rows/

# 8. Oldest JSON files in storage (candidates to review if disk grows)
find /opt/spir_dynamic/storage/extracted_rows/ -name "*.json" \
    -printf '%T+\t%p\n' 2>/dev/null | sort | head -10

# 9. Inode usage (can fill without df showing disk full)
df -i /

# 10. Open file handle count (uvicorn — high values indicate leak)
ls /proc/$(pgrep -f "uvicorn" | head -1)/fd 2>/dev/null | wc -l

# 11. PostgreSQL active connection count
sudo -u postgres psql -c "SELECT count(*) FROM pg_stat_activity;" 2>/dev/null

# 12. PostgreSQL extraction_history table row count and disk size
sudo -u postgres psql -d spir_db -c \
    "SELECT count(*), pg_size_pretty(pg_total_relation_size('extraction_history')) FROM extraction_history;" \
    2>/dev/null

# 13. Log rotation dry-run (verify config is valid, no actual rotation)
sudo logrotate -d /etc/logrotate.d/spir-api

# 14. Cleanup timer — last run and next scheduled run
systemctl status spir-cleanup.timer
journalctl -u spir-cleanup -n 5 --no-pager


# ══════════════════════════════════════════════════════════════
# NGINX LOG DEBUGGING
# ══════════════════════════════════════════════════════════════

# Tail live access log (with upstream timing: rt= urt=)
sudo tail -f /var/log/nginx/spir_access.log

# All 4xx and 5xx errors, last 30 lines
sudo grep -E '" [45][0-9]{2} ' /var/log/nginx/spir_access.log | tail -30

# 499 — client closed connection (upload aborted, browser timeout, user cancel)
sudo grep '" 499 ' /var/log/nginx/spir_access.log | tail -20

# 502 — upstream down or crashed
sudo grep '" 502 ' /var/log/nginx/spir_access.log | tail -20

# 504 — upstream timeout (extraction ran past proxy_read_timeout=300s)
sudo grep '" 504 ' /var/log/nginx/spir_access.log | tail -20

# 403 — forbidden (misconfigured CORS or block rule)
sudo grep '" 403 ' /var/log/nginx/spir_access.log | tail -20

# Requests with upstream response time >60s
sudo grep -oP 'urt=\K[0-9]+\.[0-9]+' /var/log/nginx/spir_access.log \
    | awk '$1+0 > 60 {print}' | wc -l

# Show the 20 slowest requests today (by upstream response time)
sudo grep "$(date '+%d/%b/%Y')" /var/log/nginx/spir_access.log \
    | grep -oP '.*urt=\K[0-9]+\.[0-9]+.*' \
    | sort -rn | head -20

# Tail nginx error log
sudo tail -f /var/log/nginx/spir_error.log

# Active TCP connections on API/frontend ports
ss -tnp | grep -E ':443|:8000|:3000' | awk '{print $1}' | sort | uniq -c


# ══════════════════════════════════════════════════════════════
# PROCESS AND SYSTEM MONITORING
# ══════════════════════════════════════════════════════════════

# CPU and RAM by process — top 10 consumers
ps aux --sort=-%mem | head -12

# uvicorn worker memory (each of the 4 workers separately)
ps -o pid,rss,vsz,%mem,cmd -p $(pgrep -f "uvicorn" | tr '\n' ',') 2>/dev/null

# Celery worker memory
ps -o pid,rss,vsz,%mem,cmd -p $(pgrep -f "celery" | tr '\n' ',') 2>/dev/null

# All spir_dynamic processes — RSS in MB
ps -o pid,rss,cmd -p $(pgrep -f "spir_dynamic" 2>/dev/null | tr '\n' ',') 2>/dev/null \
    | awk 'NR>1 {printf "%s  %sMB  %s\n", $1, int($2/1024), $3}'

# Swap usage detail
swapon --show

# Network connections to FastAPI (active upstream connections)
ss -tn 'dport = :8000' | head -20

# Network connections TO the VPS on port 443 (active browser sessions)
ss -tn 'dport = :443' | wc -l

# CPU usage snapshot
top -b -n 1 -o +%CPU | head -20

# Load average (>2.0 on 2 vCPU = saturated)
uptime

# Celery worker status (if celery_enabled)
cd /opt/spir_dynamic && \
    venv/bin/celery -A spir_dynamic.celery_app inspect active 2>/dev/null | head -20

# Celery queue depth (pending tasks)
redis-cli llen celery 2>/dev/null


# ══════════════════════════════════════════════════════════════
# DEPLOYMENT CHECKLIST
# (Run items top-to-bottom before and after each deploy)
# ══════════════════════════════════════════════════════════════

# BEFORE DEPLOYING:
# [ ] git pull && git status  (confirm branch is at expected commit)
# [ ] sudo nginx -t            (test nginx config — never reload without this)
# [ ] df -h /                  (verify >10% free disk)
# [ ] systemctl is-active postgresql  (database must be running)
# [ ] redis-cli ping           (Redis must be running if CELERY_ENABLED=true)
# [ ] curl -sf http://127.0.0.1:8000/health  (baseline health before restart)

# DEPLOYING:
# sudo systemctl restart spir-api
# sudo systemctl restart spir-worker   # only if CELERY_ENABLED=true

# AFTER DEPLOYING (wait 15-30s for workers to come up):
# [ ] journalctl -u spir-api -n 30 --no-pager  (no CRITICAL/ERROR at startup)
# [ ] curl -sf http://127.0.0.1:8000/health | python3 -m json.tool
# [ ]   → status must be "healthy", extraction_dir must be "ok"
# [ ] pm2 restart spir-frontend && pm2 status
# [ ] Test one extraction through the UI (small file, <5 MB)
# [ ] Watch journalctl -u spir-api -f for 60s after the test extraction


# ══════════════════════════════════════════════════════════════
# EMERGENCY DEBUGGING CHECKLIST
# ══════════════════════════════════════════════════════════════

# SCENARIO: API returns 502 Bad Gateway
# ─────────────────────────────────────
# Step 1: Is uvicorn actually running and listening?
systemctl status spir-api
ss -tlnp | grep 8000

# Step 2: Is uvicorn reachable directly (bypassing nginx)?
curl -sf http://127.0.0.1:8000/health

# Step 3: Check uvicorn logs for crash
journalctl -u spir-api --since "10 min ago" -p err --no-pager

# Step 4: If crashed, restart gracefully (30s SIGTERM drain):
sudo systemctl restart spir-api
journalctl -u spir-api -f   # watch startup


# SCENARIO: Extraction stuck / no response for >10min
# ───────────────────────────────────────────────────
# Step 1: Confirm there are queued extractions with no EXTRACTION_EVENT
journalctl -u spir-api --since "30 min ago" | grep "Extraction queued"
journalctl -u spir-api --since "30 min ago" | grep "EXTRACTION_EVENT"

# Step 2: Check semaphore/concurrency (are slots all taken?)
journalctl -u spir-api --since "1h ago" | grep "Semaphore acquired"

# Step 3: Check if a timeout fires (extraction_timeout_seconds=600)
journalctl -u spir-api --since "1h ago" | grep "status=timeout"

# Step 4: If genuinely hung, restart (drains current requests safely):
sudo systemctl restart spir-api


# SCENARIO: Out of disk space
# ────────────────────────────
df -h /
# Identify large files:
du -sh /tmp/spir_upload_* 2>/dev/null || echo "no temp uploads"
du -sh /opt/spir_dynamic/storage/extracted_rows/
du -sh /var/log/nginx/
sudo journalctl --disk-usage

# Safe cleanup — /tmp uploads ONLY (never touch extracted_rows/ manually):
find /tmp -maxdepth 1 -name "spir_upload_*" -mmin +120 -delete

# Rotate nginx logs immediately if bloated:
sudo logrotate -f /etc/logrotate.d/spir-api

# Vacuum systemd journal to 200 MB:
sudo journalctl --vacuum-size=200M


# SCENARIO: Out of memory / OOM killer fired
# ───────────────────────────────────────────
# Step 1: Check if OOM killer fired recently
dmesg -T | grep -i "oom\|killed" | tail -20

# Step 2: Current RAM state
free -h
ps aux --sort=-%mem | head -10

# Step 3: Check which extraction was running at OOM time
journalctl -u spir-api --since "30 min ago" | grep -E "Pipeline start|mem_rss_mb|EXTRACTION_EVENT"

# Step 4: If recurrent, the file is likely >500MB with many embedded objects
# Reduce max_concurrent_extractions from 4 to 2 in .env and restart.


# SCENARIO: PM2 frontend not serving
# ────────────────────────────────────
pm2 status
pm2 logs spir-frontend --lines 50 --nostream
pm2 restart spir-frontend
# If still failing:
pm2 delete spir-frontend
cd /opt/spir_dynamic/frontend && pm2 start ecosystem.config.js
pm2 save


# SCENARIO: Redis down (Celery batch jobs failing)
# ─────────────────────────────────────────────────
redis-cli ping
systemctl status redis
sudo systemctl restart redis
# Verify batch queue is draining:
redis-cli llen celery


# SCENARIO: PostgreSQL slow or locking up
# ─────────────────────────────────────────
# Active sessions and wait events:
sudo -u postgres psql -c \
    "SELECT pid, wait_event_type, wait_event, state, query_start, query
     FROM pg_stat_activity WHERE state='active' ORDER BY query_start;" 2>/dev/null

# Long-running queries (>30s):
sudo -u postgres psql -c \
    "SELECT pid, now()-query_start AS duration, query
     FROM pg_stat_activity WHERE state='active' AND now()-query_start > interval '30s';" 2>/dev/null

# Terminate a stuck query (replace PID):
# sudo -u postgres psql -c "SELECT pg_terminate_backend(PID);"
