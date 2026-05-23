from prometheus_client import Counter, Histogram, Gauge

FILES_PROCESSED = Counter(
    "files_processed_total",
    "Total processed files"
)

FAILED_TASKS = Counter(
    "failed_tasks_total",
    "Total failed tasks"
)

SUCCESSFUL_TASKS = Counter(
    "successful_tasks_total",
    "Total successful tasks"
)

EXTRACTION_ROWS = Counter(
    "extraction_rows_total",
    "Total extracted rows"
)

PROCESSING_DURATION = Histogram(
    "processing_duration_seconds",
    "Processing duration"
)

ACTIVE_WORKERS = Gauge(
    "active_workers",
    "Currently active Celery workers"
)

QUEUE_SIZE = Gauge(
    "queue_size",
    "Pending queue size"
)

MEMORY_USAGE = Gauge(
    "memory_usage_mb",
    "Memory usage in MB"
)

# ── Storage / disk metrics ────────────────────────────────────────────────────

STORAGE_JSON_COUNT = Gauge(
    "storage_json_files_total",
    "Number of extracted JSON row files on disk",
)

STORAGE_JSON_SIZE_MB = Gauge(
    "storage_json_size_mb",
    "Total size of extracted_rows directory in MB",
)

STORAGE_UPLOAD_SIZE_MB = Gauge(
    "storage_upload_size_mb",
    "Total size of batch_uploads staging directory in MB",
)

CLEANUP_DELETED_FILES = Counter(
    "cleanup_deleted_files_total",
    "Files deleted by the lifecycle cleanup task",
    ["reason"],  # "expired" | "orphan" | "stale_upload"
)

CLEANUP_DURATION = Histogram(
    "cleanup_duration_seconds",
    "Wall-clock time for a full lifecycle cleanup run",
    buckets=[1, 5, 10, 30, 60, 120, 300],
)

# ── Sanitizer metrics ─────────────────────────────────────────────────────────

SANITIZER_RUNS = Counter(
    "sanitizer_runs_total",
    "Sanitizer invocations by outcome",
    ["outcome"],   # "success" | "fallback" | "skipped"
)

SANITIZER_SAVINGS_MB = Histogram(
    "sanitizer_savings_mb",
    "MB removed by sanitization (original - sanitized)",
    buckets=[0, 10, 50, 100, 200, 500, 1000],
)

SANITIZER_REDUCTION_PCT = Histogram(
    "sanitizer_reduction_pct",
    "File size reduction percentage after sanitization",
    buckets=[0, 10, 20, 30, 50, 70, 90, 95, 99],
)

SANITIZER_DURATION = Histogram(
    "sanitizer_duration_seconds",
    "Wall-clock time spent sanitizing (extract + strip + rezip)",
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120],
)
