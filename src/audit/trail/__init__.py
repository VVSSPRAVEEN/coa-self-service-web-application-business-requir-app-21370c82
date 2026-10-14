"""Immutable, hash-chained audit trail for COA events."""

from src.audit.trail.chain import AuditChain
from src.audit.trail.models import AuditEventType, AuditLogEntry
from src.audit.trail.repository import (
    AuditRepository,
    InMemoryAuditStorage,
    PostgresAuditStorage,
    build_audit_storage,
)
from src.audit.trail.service import AuditTrailService
from src.audit.trail.cli import main as cli_main

__all__ = [
    "AuditChain",
    "AuditEventType",
    "AuditLogEntry",
    "AuditRepository",
    "AuditTrailService",
    "InMemoryAuditStorage",
    "PostgresAuditStorage",
    "build_audit_storage",
    "cli_main",
]
