"""Admin CLI entry point for audit trail management."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from src.audit.trail.models import AuditLogEntry
from src.audit.trail.repository import AuditRepository
from src.audit.trail.service import AuditTrailService


def _format_entry(entry: AuditLogEntry) -> str:
    """Format a single audit entry for CLI display."""
    return (
        f"[{entry.sequence_number}] {entry.timestamp.isoformat()} | "
        f"{entry.event_type.value} | user={entry.user_sub} | "
        f"resource={entry.resource_id} | hash={entry.entry_hash[:16]}..."
    )


def cmd_verify(service: AuditTrailService) -> int:
    """Run chain integrity verification."""
    is_valid, failed_idx = service.verify_chain_integrity()
    if is_valid:
        print("AUDIT CHAIN INTEGRITY: VALID")
        print(f"Total entries verified: {len(service.get_all_events())}")
        return 0
    print(f"AUDIT CHAIN INTEGRITY: TAMPERED at index {failed_idx}")
    return 1


def cmd_list(
    service: AuditTrailService,
    resource_id: str | None = None,
) -> int:
    """List audit entries, optionally filtered by resource."""
    if resource_id:
        entries = service.get_events_for_resource(resource_id)
    else:
        entries = service.get_all_events()

    if not entries:
        print("No audit entries found.")
        return 0

    for entry in entries:
        print(_format_entry(entry))

    print(f"\nTotal: {len(entries)} entries")
    return 0


def cmd_export_json(
    service: AuditTrailService,
    output_path: str | None = None,
) -> int:
    """Export audit trail as JSON."""
    entries: list[AuditLogEntry] = service.get_all_events()
    data = [entry.model_dump(mode="json") for entry in entries]
    json_str = json.dumps(data, indent=2, default=str)

    if output_path:
        safe_name = os.path.basename(output_path)
        with open(safe_name, "w", encoding="utf-8") as handle:
            handle.write(json_str)
        print(f"Exported {len(entries)} entries to {safe_name}")
    else:
        print(json_str)

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI entry point for audit trail administration."""
    parser = argparse.ArgumentParser(
        prog="audit-trail-admin",
        description="Ventura COA Audit Trail Administration CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("verify", help="Verify audit chain integrity")

    list_parser = subparsers.add_parser("list", help="List audit entries")
    list_parser.add_argument(
        "--resource-id",
        type=str,
        default=None,
        help="Filter by COA resource ID",
    )

    export_parser = subparsers.add_parser("export", help="Export audit trail as JSON")
    export_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (prints to stdout if omitted)",
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Append-only repository over the backend the environment selects: the
    # PostgreSQL trail when a DSN is configured, the in-memory one otherwise.
    repository = AuditRepository()
    service = AuditTrailService(repository=repository)

    if args.command == "verify":
        return cmd_verify(service)
    if args.command == "list":
        return cmd_list(service, resource_id=args.resource_id)
    if args.command == "export":
        return cmd_export_json(service, output_path=args.output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
