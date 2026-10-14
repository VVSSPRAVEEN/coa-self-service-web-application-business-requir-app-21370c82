"""Webhook endpoints for receiving status deltas from JDE and Bottomline."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from src.integration.sync.models import CoaStatus, StatusDelta
from src.integration.sync.status_sync import StatusSyncService, SyncResult

logger = logging.getLogger(__name__)


class WebhookValidationError(Exception):
    """Raised when webhook payload validation fails."""


def validate_webhook_payload(payload: Dict[str, Any]) -> None:
    """Validate incoming webhook payload structure.

    Args:
        payload: The raw webhook JSON payload.

    Raises:
        WebhookValidationError: If required fields are missing or invalid.
    """
    required_fields = ["coa_number", "status", "source", "sequence_id", "timestamp"]
    for field_name in required_fields:
        if field_name not in payload:
            raise WebhookValidationError(f"Missing required field: {field_name}")

    source = str(payload["source"]).upper()
    if source not in ("JDE", "BOTTOMLINE"):
        raise WebhookValidationError(f"Invalid source: {payload['source']}")

    try:
        CoaStatus(payload["status"])
    except ValueError as exc:
        raise WebhookValidationError(
            f"Invalid status value: {payload['status']}"
        ) from exc

    if not isinstance(payload["sequence_id"], int):
        raise WebhookValidationError("sequence_id must be an integer")


def parse_status_delta(payload: Dict[str, Any]) -> StatusDelta:
    """Parse a validated webhook payload into a StatusDelta.

    Args:
        payload: Validated webhook payload dictionary.

    Returns:
        Parsed StatusDelta instance.
    """
    return StatusDelta(
        coa_number=str(payload["coa_number"]),
        new_status=CoaStatus(payload["status"]),
        source=str(payload["source"]).upper(),
        sequence_id=int(payload["sequence_id"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        idempotency_key=str(
            payload.get(
                "idempotency_key",
                f"{payload['source']}-{payload['coa_number']}-{payload['sequence_id']}",
            )
        ),
    )


def handle_status_webhook(
    payload: Dict[str, Any],
    sync_service: StatusSyncService,
) -> SyncResult:
    """Process an incoming status webhook from JDE or Bottomline.

    Validates the payload, parses it into a StatusDelta, and applies it
    through the sync service.

    Args:
        payload: Raw webhook JSON payload.
        sync_service: The StatusSyncService instance.

    Returns:
        SyncResult from applying the delta.

    Raises:
        WebhookValidationError: If payload validation fails.
    """
    validate_webhook_payload(payload)
    delta = parse_status_delta(payload)
    return sync_service.apply_delta(delta)


def handle_batch_webhook(
    payloads: List[Dict[str, Any]],
    sync_service: StatusSyncService,
) -> List[SyncResult]:
    """Process a batch of incoming status webhooks.

    Invalid payloads are logged and skipped rather than failing the batch.

    Args:
        payloads: List of raw webhook JSON payloads.
        sync_service: The StatusSyncService instance.

    Returns:
        List of SyncResults for successfully processed deltas.
    """
    results: List[SyncResult] = []
    for payload in payloads:
        try:
            result = handle_status_webhook(payload, sync_service)
            results.append(result)
        except WebhookValidationError:
            logger.exception("Webhook validation failed for payload: %s", payload)
    return results