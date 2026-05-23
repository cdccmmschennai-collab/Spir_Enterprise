"""
Notification service — placeholder for email/webhook alerts.

When infrastructure is ready, implement send_reset_request_notification()
to alert branch admins of incoming password reset requests.

Configuration (add to .env when ready):
  SMTP_HOST=smtp.example.com
  SMTP_PORT=587
  SMTP_USER=alerts@example.com
  SMTP_PASS=...
  SMTP_FROM=noreply@example.com
"""
from __future__ import annotations

import structlog

log = structlog.stdlib.get_logger(__name__)


async def send_reset_request_notification(
    username: str,
    branch_id: str | None = None,
    email: str | None = None,
) -> None:
    """
    Notify branch admin(s) of a new password reset request.
    No-op until SMTP/webhook settings are configured.
    """
    log.info(
        "notification.reset_request_received",
        username=username,
        branch_id=branch_id,
        notified_to=email,
        status="not_configured",
    )
