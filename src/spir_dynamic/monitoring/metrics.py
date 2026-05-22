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
