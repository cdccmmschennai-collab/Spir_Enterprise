from spir_dynamic.celery_app import celery_app
from spir_dynamic.tasks.base import ping
from spir_dynamic.tasks.extraction_tasks import process_file_task

__all__ = ["celery_app", "ping", "process_file_task"]
