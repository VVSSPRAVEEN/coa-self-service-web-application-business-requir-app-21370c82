"""The runtime job loop (C7/C8/C9), runnable as ``python -m src.workers.scheduler``.

Four jobs run on one interval. Each one delegates to a module that already owns
its domain logic - nothing here reimplements parsing, status reconciliation,
redrive or mail assembly:

===============  ==================================================  ==========
job              delegates to                                        no-ops when
===============  ==================================================  ==========
bottomline_ingest :mod:`src.integration.bottomline`                   no inbound dir
status_sync      :mod:`src.integration.sync`                         SYNC_BASE_URL unset
dlq_redrive      :mod:`src.integration.retry`                       nothing pending
notifications    :mod:`src.notifications.email`                      queue empty
===============  ==================================================  ==========

**Injectable boundaries.** The loop never reaches for a global. The clock
(``sleep``/``now``), the HTTP fetcher used by ``status_sync``, the mail transport
and the ingestion repository are all constructor arguments, so
``tests/unit/test_scheduler.py`` drives a complete cycle against temporary
directories and stubs and never opens a socket. The defaults are the real ones.

**File choreography.** Three jobs consume files, and each one ends with the file
out of the directory it was picked up from: ``bottomline_ingest`` moves a file
to ``./processed`` once it is ingested and to ``./quarantine`` when it is
rejected, ``notifications`` does the same for an alert it dispatched, and both
directories are created on demand. A redriveable failure is additionally written
to the dead-letter queue with its content attached, which is what makes the
redrive job a replay rather than a retry from memory.

**Failure isolation.** :meth:`Scheduler.run_once` runs every job and returns one
:class:`JobOutcome` each; a job that raises is reported as a failed outcome and
the remaining jobs still run. A single bad inbound file therefore cannot take the
worker down, and the exit code stays meaningful.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Protocol, Sequence

from src.integration.bottomline.ingestor import BottomlineIngestor
from src.integration.bottomline.models import IngestedFile, IngestionStatus
from src.integration.bottomline.repository import IngestionRepository
from src.integration.bottomline.sftp_poller import SftpFileEntry, SftpPoller
from src.integration.retry.dlq import DeadLetterQueue
from src.integration.retry.redrive import redrive_dlq
from src.integration.sync.status_sync import StatusSyncService
from src.integration.sync.webhook import handle_batch_webhook
from src.notifications.email.dispatcher import (
    DispatcherSettings,
    EmailDispatcher,
)
from src.notifications.email.transport import (
    DryRunTransport,
    MailTransport,
    SmtpMailTransport,
)

__all__ = [
    "BOTTOMLINE_INGEST",
    "DEFAULT_ALERT_QUEUE_DIR",
    "DEFAULT_BOTTOMLINE_INBOUND_DIR",
    "DEFAULT_DLQ_STORAGE_PATH",
    "DEFAULT_INTERVAL_SECONDS",
    "DLQ_REDRIVE",
    "HttpJsonFetcher",
    "JOB_NAMES",
    "JobOutcome",
    "LocalDropDirectoryClient",
    "NOTIFICATIONS",
    "STATUS_SYNC",
    "Scheduler",
    "SystemClock",
    "main",
    "run_once",
]

#: Drop directory scanned for Bottomline files.
BOTTOMLINE_INGEST = "bottomline_ingest"

#: Reconciles COA headers against a configured sync endpoint.
STATUS_SYNC = "status_sync"

#: Replays pending dead-letter records.
DLQ_REDRIVE = "dlq_redrive"

#: Dispatches queued alert emails.
NOTIFICATIONS = "notifications"

#: The jobs :meth:`Scheduler.run_once` executes, in order.
JOB_NAMES: tuple[str, ...] = (
    BOTTOMLINE_INGEST,
    STATUS_SYNC,
    DLQ_REDRIVE,
    NOTIFICATIONS,
)

#: Defaults applied when the matching environment variable is unset.
DEFAULT_BOTTOMLINE_INBOUND_DIR = "./inbound/bottomline"
DEFAULT_ALERT_QUEUE_DIR = "./inbound/alerts"
DEFAULT_PROCESSED_DIR = "./processed"
DEFAULT_QUARANTINE_DIR = "./quarantine"
DEFAULT_DLQ_STORAGE_PATH = "./dlq_store"
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_SYNC_STATUS_PATH = "/api/v1/sync/status"
DEFAULT_SENDER_ADDRESS = "no-reply@ventura-coa.local"
DEFAULT_FILE_PATTERN = "*.txt"

logger = logging.getLogger("ventura.coa.workers")


class Clock(Protocol):
    """The two time operations the loop needs, so a test can supply its own."""

    def now(self) -> datetime:
        ...

    def sleep(self, seconds: float) -> None:
        ...


class SystemClock:
    """The real clock: UTC wall time and :func:`time.sleep`."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""
        time.sleep(seconds)


class Fetcher(Protocol):
    """Reads a JSON document list from a URL."""

    def __call__(self, url: str) -> List[dict[str, Any]]:
        ...


class HttpJsonFetcher:
    """Default :class:`Fetcher`: one HTTP GET, JSON-decoded.

    A payload that is a bare list is taken as-is; an object is searched for a
    ``deltas`` or ``items`` list, which is how both documented status-poll
    endpoints wrap their results.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def __call__(self, url: str) -> List[dict[str, Any]]:
        """Return the documents published at ``url``."""
        import httpx

        response = httpx.get(url, timeout=self._timeout)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, Mapping):
            for key in ("deltas", "items", "results"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    return [dict(item) for item in candidate]
            return []
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, Mapping)]
        return []


class LocalDropDirectoryClient:
    """An :class:`~src.integration.bottomline.sftp_poller.SftpClient` over a directory.

    The ingestion pipeline discovers files through an ``SftpClient``. Rather than
    duplicate the pipeline for a local drop, this adapter presents a directory
    through that same three-method protocol, so ``SftpPoller`` and
    ``BottomlineIngestor`` run unmodified against a mounted volume.

    ``delete_file`` is a no-op: the scheduler moves a processed file to
    ``./processed`` instead of removing it, so the ingested payload stays
    inspectable.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def list_files(self, remote_dir: str) -> List[str]:
        """Return the names of the regular files in the drop directory."""
        try:
            return sorted(
                entry.name for entry in Path(remote_dir).iterdir() if entry.is_file()
            )
        except OSError:
            return []

    def read_file(self, remote_path: str) -> bytes:
        """Return the bytes of one file in the drop directory."""
        return Path(remote_path).read_bytes()

    def delete_file(self, remote_path: str) -> None:
        """Leave the file in place; the scheduler moves it instead."""


@dataclass(frozen=True)
class JobOutcome:
    """What one job did during a single cycle.

    Attributes:
        name: The job's identifier; one of :data:`JOB_NAMES`.
        ok: ``False`` only when the job raised.
        detail: A human-readable summary, including why a job was skipped.
        processed: Items the job successfully handled.
        failed: Items the job could not handle.
        skipped: Items the job deliberately did not act on.
    """

    name: str
    ok: bool = True
    detail: str = ""
    processed: int = 0
    failed: int = 0
    skipped: int = 0

    def as_log_fields(self) -> dict[str, Any]:
        """Return the counters under names suitable for structured logging."""
        return {
            "job": self.name,
            "job_ok": self.ok,
            "job_processed": self.processed,
            "job_failed": self.failed,
            "job_skipped": self.skipped,
        }


def _move(source: Path, destination_dir: Path) -> Path:
    """Move ``source`` into ``destination_dir`` under a non-colliding name."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / source.name
    counter = 1
    while target.exists():
        target = destination_dir / f"{source.stem}.{counter}{source.suffix}"
        counter += 1
    shutil.move(str(source), str(target))
    return target


def _file_incoming_file(
    status: IngestionStatus, source: Path, processed: Path
) -> None:
    """Take one ingested file out of the drop directory.

    A rejected file is deleted rather than moved because
    :func:`quarantine_rejected_file` has already written its bytes into the
    quarantine directory under a ``{batch}_{timestamp}_{name}`` filename;
    copying the original there as well would store the same payload twice under
    two names. Every other status is a file the pipeline owns now, so it is moved
    to ``processed``.
    """
    if status is IngestionStatus.QUARANTINED:
        source.unlink(missing_ok=True)
        return
    _move(source, processed)


def _recipients(raw: Optional[str]) -> tuple[str, ...]:
    """Split a comma-separated recipient list, dropping blanks."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class Scheduler:
    """Runs the four runtime jobs on an interval.

    Args:
        env: Environment mapping to read; :data:`os.environ` by default. Every
            job's directories, endpoint, SMTP configuration and the loop
            interval come from here, so a deployment configures the worker
            without touching this module.
        clock: Time source; :class:`SystemClock` by default.
        transport: Mail transport; built from ``SMTP_HOST`` by default, falling
            back to :class:`DryRunTransport` when no SMTP host is configured.
        fetcher: HTTP fetcher for :data:`STATUS_SYNC`; :class:`HttpJsonFetcher`
            by default.
        repository: Bottomline ingestion state; a fresh
            :class:`~src.integration.bottomline.repository.IngestionRepository`
            by default, created once per scheduler so duplicate detection holds
            across cycles for the life of the process.
        sync_service: COA header state for :data:`STATUS_SYNC`; created once per
            scheduler so its processed-key set and history survive cycles.
        dispatcher: Email dispatcher; built once per scheduler so its
            idempotency store survives cycles and a replayed alert is skipped.
    """

    def __init__(
        self,
        *,
        env: Optional[Mapping[str, str]] = None,
        clock: Optional[Clock] = None,
        transport: Optional[MailTransport] = None,
        fetcher: Optional[Fetcher] = None,
        repository: Optional[IngestionRepository] = None,
        sync_service: Optional[StatusSyncService] = None,
        dispatcher: Optional[EmailDispatcher] = None,
    ) -> None:
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._clock: Clock = clock or SystemClock()
        self._fetcher: Fetcher = fetcher or HttpJsonFetcher()
        self._repository = repository or IngestionRepository()
        self._sync_service = sync_service or StatusSyncService()
        self._transport: MailTransport = (
            transport if transport is not None else self._build_transport()
        )
        self._dispatcher = dispatcher or self._build_dispatcher()

    # -- configuration -------------------------------------------------------

    def _setting(self, name: str, default: str = "") -> str:
        """Return ``name`` from the environment, trimmed, or ``default``."""
        raw = self._env.get(name)
        if raw is None:
            return default
        return str(raw).strip() or default

    @property
    def interval_seconds(self) -> float:
        """Seconds between cycles, from ``WORKER_INTERVAL_SECONDS``."""
        raw = self._setting("WORKER_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS))
        try:
            return float(raw)
        except ValueError:
            logger.warning("WORKER_INTERVAL_SECONDS=%r is not a number", raw)
            return DEFAULT_INTERVAL_SECONDS

    def _build_transport(self) -> MailTransport:
        """Return the SMTP transport, or the dry-run one when unconfigured."""
        host = self._setting("SMTP_HOST")
        if not host:
            return DryRunTransport()
        try:
            port = int(self._setting("SMTP_PORT", "587"))
        except ValueError:
            port = 587
        return SmtpMailTransport(
            host=host,
            port=port,
            username=self._setting("SMTP_USER") or None,
            password=self._setting("SMTP_PASSWORD") or None,
            use_tls=self._setting("SMTP_STARTTLS", "1") not in ("0", "false", "False"),
        )

    def _build_dispatcher(self) -> EmailDispatcher:
        """Build the email dispatcher over the configured transport.

        ``dry_run`` is an operator override (``ALERT_DRY_RUN``), not the default:
        leaving mail is the *transport's* decision, so an unconfigured worker
        still exercises the dispatcher's recipients, idempotency keys and retry
        policy and lands the rendered message in the dry-run outbox instead of
        short-circuiting inside the dispatcher.
        """
        return EmailDispatcher(
            self._transport,
            DispatcherSettings(
                sender_address=self._setting("ALERT_SENDER", DEFAULT_SENDER_ADDRESS),
                cs_recipients=_recipients(self._setting("ALERT_CS_RECIPIENTS")),
                qa_recipients=_recipients(self._setting("ALERT_QA_RECIPIENTS")),
                dry_run=self._setting("ALERT_DRY_RUN", "0").lower()
                in ("1", "true", "yes"),
            ),
        )

    def _ingestor(self, quarantine_dir: Path) -> BottomlineIngestor:
        """Build the existing ingestion pipeline over a local drop directory."""
        ingestor, _ = self._pipeline(quarantine_dir)
        return ingestor

    def _pipeline(self, quarantine_dir: Path) -> tuple[BottomlineIngestor, SftpPoller]:
        """Build the ingestion pipeline and the poller that feeds it.

        Both halves are returned because the poller is the only thing that
        discovers files, and the scheduler drives discovery itself so it can file
        each file once its outcome is known.
        """
        inbound = self._inbound_dir()
        poller = SftpPoller(
            LocalDropDirectoryClient(inbound),
            str(inbound),
            file_pattern=self._setting("BOTTOMLINE_FILE_PATTERN", DEFAULT_FILE_PATTERN),
        )
        ingestor = BottomlineIngestor(
            repository=self._repository,
            poller=poller,
            quarantine_dir=str(quarantine_dir),
        )
        return ingestor, poller

    def _dlq(self) -> DeadLetterQueue:
        """Return the dead-letter queue at ``DLQ_STORAGE_PATH``."""
        return DeadLetterQueue(
            self._setting("DLQ_STORAGE_PATH", DEFAULT_DLQ_STORAGE_PATH)
        )

    def _batch_size(self) -> int:
        """Return the redrive batch ceiling from ``WORKER_DLQ_BATCH_SIZE``."""
        try:
            return int(self._setting("WORKER_DLQ_BATCH_SIZE", "100"))
        except ValueError:
            return 100

    def _inbound_dir(self) -> Path:
        """Return the Bottomline drop directory."""
        return Path(self._setting("BOTTOMLINE_INBOUND_DIR", DEFAULT_BOTTOMLINE_INBOUND_DIR))

    def _processed_dir(self) -> Path:
        """Return the directory successfully handled files are moved to."""
        return Path(self._setting("BOTTOMLINE_PROCESSED_DIR", DEFAULT_PROCESSED_DIR))

    def _quarantine_dir(self) -> Path:
        """Return the directory rejected files are moved to."""
        return Path(self._setting("BOTTOMLINE_QUARANTINE_DIR", DEFAULT_QUARANTINE_DIR))

    # -- job (a): Bottomline ingestion ---------------------------------------

    def run_bottomline_ingest(self) -> JobOutcome:
        """Ingest every file in the drop directory through the existing pipeline.

        A file the pipeline persists (or recognises as a duplicate) is moved to
        ``./processed``; a file it rejects is moved to ``./quarantine``; a file
        that raises is written to the dead-letter queue - with its content
        attached, so :data:`DLQ_REDRIVE` can replay it - and quarantined.
        """
        inbound = self._inbound_dir()
        if not inbound.is_dir():
            return JobOutcome(
                name=BOTTOMLINE_INGEST,
                detail=f"no drop directory at {inbound}",
                skipped=1,
            )

        processed = self._processed_dir()
        quarantine = self._quarantine_dir()
        ingestor, poller = self._pipeline(quarantine)
        dlq = self._dlq()
        outcomes = [
            self._ingest_entry(ingestor, dlq, entry, inbound, processed, quarantine)
            for entry in poller.poll()
        ]

        return JobOutcome(
            name=BOTTOMLINE_INGEST,
            detail=f"polled {inbound}",
            processed=sum(1 for status in outcomes if status == IngestionStatus.PERSISTED),
            failed=sum(1 for status in outcomes if status == IngestionStatus.QUARANTINED),
            skipped=sum(1 for status in outcomes if status == IngestionStatus.DUPLICATE),
        )

    def _ingest_entry(
        self,
        ingestor: BottomlineIngestor,
        dlq: DeadLetterQueue,
        entry: Any,
        inbound: Path,
        processed: Path,
        quarantine: Path,
    ) -> IngestionStatus:
        """Ingest one discovered file and file it under the right directory."""
        source = inbound / entry.file_name
        try:
            result: IngestedFile = ingestor.ingest_file(entry)
        except Exception as exc:
            dlq.write(
                record_id=f"bottomline:{entry.batch_id}:{entry.file_name}",
                payload={
                    "batch_id": entry.batch_id,
                    "file_name": entry.file_name,
                    "content_b64": base64.b64encode(entry.content).decode("ascii"),
                },
                attempt_count=1,
                error=exc,
                source="bottomline_ingest",
            )
            _move(source, quarantine)
            logger.exception(
                "bottomline ingestion failed for %s",
                entry.file_name,
                extra={"job": BOTTOMLINE_INGEST},
            )
            return IngestionStatus.QUARANTINED

        _file_incoming_file(result.status, source, processed)
        return result.status

    # -- job (b): COA status sync --------------------------------------------

    def run_status_sync(self) -> JobOutcome:
        """Reconcile COA headers against the configured sync endpoint.

        A no-op when ``SYNC_BASE_URL`` is unset, so a deployment that receives
        its deltas purely by webhook runs the loop without one. Payloads are fed
        through :func:`handle_batch_webhook`, so validation, parsing, terminal
        state protection and idempotency are the sync module's own rules.
        """
        base_url = self._setting("SYNC_BASE_URL")
        if not base_url:
            return JobOutcome(
                name=STATUS_SYNC,
                detail="SYNC_BASE_URL is not configured; status deltas arrive by webhook only",
                skipped=1,
            )

        url = base_url.rstrip("/") + self._setting("SYNC_STATUS_PATH", DEFAULT_SYNC_STATUS_PATH)
        payloads = self._fetcher(url)
        results = handle_batch_webhook(payloads, self._sync_service)
        applied = sum(1 for result in results if result.applied)

        return JobOutcome(
            name=STATUS_SYNC,
            detail=f"reconciled {len(payloads)} delta(s) from {url}",
            processed=applied,
            skipped=len(payloads) - applied,
        )

    # -- job (c): dead-letter redrive ----------------------------------------

    def run_dlq_redrive(self) -> JobOutcome:
        """Replay every pending dead-letter record exactly once.

        Replayable payloads - the ones this scheduler wrote, carrying the
        original file content - go back through the Bottomline pipeline. A
        payload from another producer that this worker cannot interpret is left
        pending and counted as failed, rather than being silently dropped.
        """
        summary = redrive_dlq(
            self._dlq(),
            self._redrive_payload,
            batch_size=self._batch_size(),
        )
        return JobOutcome(
            name=DLQ_REDRIVE,
            detail=(
                f"redrove {summary['succeeded']} of {summary['total']} pending record(s)"
            ),
            processed=summary["succeeded"],
            failed=summary["failed"],
            skipped=summary["skipped"],
        )

    def _redrive_payload(self, payload: Any) -> IngestedFile:
        """Replay one dead-letter payload through the ingestion pipeline."""
        if not isinstance(payload, Mapping) or "content_b64" not in payload:
            raise ValueError("this worker can only redrive Bottomline file payloads")
        file_name = str(payload["file_name"])
        entry = SftpFileEntry(
            file_name=file_name,
            content=base64.b64decode(str(payload["content_b64"])),
            batch_id=str(payload.get("batch_id") or Path(file_name).stem),
        )
        ingestor = self._ingestor(self._quarantine_dir())
        result = ingestor.ingest_file(entry)
        if result.status is IngestionStatus.QUARANTINED:
            raise RuntimeError(f"redriven file {file_name} was rejected again")
        return result

    # -- job (d): queued alert notifications ---------------------------------

    def run_notifications(self) -> JobOutcome:
        """Dispatch every queued alert through the email dispatcher.

        The queue is a directory of JSON documents, each carrying a
        ``report_window``, a ``subject`` and an optional ``body_text`` /
        ``csv_content``. A dispatched alert moves to ``./processed``; one the
        dispatcher refused, or one that is not a readable alert, moves to
        ``./quarantine``. Mail leaves through ``DryRunTransport`` unless SMTP is
        configured, so an unconfigured worker exercises the whole dispatcher
        without emitting a message.
        """
        queue_dir = Path(self._setting("ALERT_QUEUE_DIR", DEFAULT_ALERT_QUEUE_DIR))
        if not queue_dir.is_dir():
            return JobOutcome(
                name=NOTIFICATIONS,
                detail=f"no alert queue at {queue_dir}",
                skipped=1,
            )

        processed = self._processed_dir()
        quarantine = self._quarantine_dir()
        dispatched = 0
        failed = 0

        for alert_file in sorted(queue_dir.glob("*.json")):
            try:
                alert = json.loads(alert_file.read_text(encoding="utf-8"))
                if not isinstance(alert, Mapping):
                    raise ValueError("alert must be a JSON object")
                sent = self._dispatcher.dispatch(
                    report_window=str(alert["report_window"]),
                    subject=str(alert["subject"]),
                    body_text=str(alert.get("body_text", "")),
                    csv_content=str(alert.get("csv_content", "")),
                    distribution_lists=alert.get("distribution_lists"),
                    channel=str(alert.get("channel", "email")),
                    body_html=alert.get("body_html"),
                )
            except Exception:
                failed += 1
                _move(alert_file, quarantine)
                logger.exception(
                    "alert %s could not be dispatched",
                    alert_file.name,
                    extra={"job": NOTIFICATIONS},
                )
                continue

            if sent:
                dispatched += 1
                _move(alert_file, processed)
            else:
                failed += 1
                _move(alert_file, quarantine)

        return JobOutcome(
            name=NOTIFICATIONS,
            detail=f"dispatched alerts from {queue_dir}",
            processed=dispatched,
            failed=failed,
        )

    # -- the loop ------------------------------------------------------------

    def run_once(self) -> List[JobOutcome]:
        """Run every job once and return one :class:`JobOutcome` per job.

        A job that raises is reported as a failed outcome; the remaining jobs
        still run, so one bad input cannot stop the cycle.
        """
        outcomes: List[JobOutcome] = []
        for name, job in (
            (BOTTOMLINE_INGEST, self.run_bottomline_ingest),
            (STATUS_SYNC, self.run_status_sync),
            (DLQ_REDRIVE, self.run_dlq_redrive),
            (NOTIFICATIONS, self.run_notifications),
        ):
            try:
                outcome = job()
            except Exception as exc:
                logger.exception("job %s raised", name, extra={"job": name})
                outcome = JobOutcome(name=name, ok=False, detail=str(exc) or type(exc).__name__)
            outcomes.append(outcome)
            logger.info("job %s finished", name, extra=outcome.as_log_fields())
        return outcomes

    def run_forever(self) -> int:
        """Run :meth:`run_once` on every interval until interrupted.

        Returns:
            ``0`` on a clean ``KeyboardInterrupt``, which is the only exit the
            loop takes on its own.
        """
        interval = self.interval_seconds
        logger.info("worker loop started", extra={"interval_seconds": interval})
        try:
            while True:
                self.run_once()
                self._clock.sleep(interval)
        except KeyboardInterrupt:
            logger.info("worker loop stopped", extra={"at": self._clock.now().isoformat()})
            return 0


def _build_scheduler() -> Scheduler:
    """Return a scheduler wired entirely from the process environment."""
    return Scheduler()


def run_once(scheduler: Optional[Scheduler] = None) -> List[JobOutcome]:
    """Run one full cycle of every job against ``scheduler``.

    This is the entry point the unit suite calls: it executes the same jobs the
    container loop runs, once, with no interval wait.
    """
    return (scheduler or _build_scheduler()).run_once()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the worker loop. ``python -m src.workers.scheduler`` lands here.

    Args:
        argv: Unused; accepted so the signature matches a console-script entry
            point and so a caller can pass an explicit empty list.

    Returns:
        The loop's exit code: ``0`` on a clean interrupt.
    """
    del argv
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return _build_scheduler().run_forever()


if __name__ == "__main__":
    sys.exit(main())
