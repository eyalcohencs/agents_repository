"""T-005 Queue POC: Procrastinate transactional enqueue, lease liveness, and universal fencing.

Validates ARCHITECTURE.md §4.5 and §9 (AC-58, AC-59, AC-60, AC-63, AC-79, AC-80):
- Same-connection SQLAlchemy 2 + psycopg v3 transactional deferral into Procrastinate.
- Atomic rollback of upload acceptance + Procrastinate job on post-enqueue failure.
- Idempotent confirmation with payload-fingerprint conflict rejection and double delivery.
- Per-attempt lease claiming, background heartbeat renewal (preventing false reclamation of healthy
  long-running jobs), and universal `active_attempt_id` fencing across all worker transitions
  (`ACTIVE`, `REJECTED`, `RETRY`, `TECHNICAL_FAILED`).
- Reconciler reclaiming genuinely expired `PROCESSING` rows (`lease_expires_at < now()`, never
  `processing_started_at`) and re-enqueuing orphaned `PENDING`/`RETRY` rows lacking an active job.
- Subprocess worker CLI (`python -m app.workers.queue_poc`) for real `SIGKILL` recovery testing,
  Procrastinate built-in stalled-job inspection, job retention cleanup, and go/no-go evaluation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import procrastinate
from procrastinate import connector as procrastinate_connector
from procrastinate import schema as procrastinate_schema
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from app.domain.geo.snapping import GRAPH_WRITER_ADVISORY_LOCK_KEY

logger = logging.getLogger(__name__)

UploadStatus = Literal[
    "DRAFT",
    "PENDING",
    "PROCESSING",
    "RETRY",
    "TECHNICAL_FAILED",
    "ACTIVE",
    "REJECTED",
]

QueueDecision = Literal[
    "GO_PROCRASTINATE",
    "FALLBACK_RABBITMQ_CELERY_OUTBOX",
    "FALLBACK_SKIP_LOCKED",
]

CREDIT_BEARING_STATUSES: frozenset[str] = frozenset(
    {"PENDING", "PROCESSING", "RETRY", "TECHNICAL_FAILED", "ACTIVE"}
)

DEFAULT_QUEUE_NAME: str = "ingestion"
INGEST_UPLOAD_TASK_NAME: str = "app.workers.queue_poc.ingest_upload"
CONFIRM_UPLOAD_OPERATION: str = "confirm_upload"
DEFAULT_TRACK_WKT: str = "MULTILINESTRING((35.0 32.6, 35.001 32.601))"

_CREATE_POC_IDEMPOTENCY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS poc_idempotency_records (
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    upload_id UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    status_code INTEGER NOT NULL,
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation, key)
);
"""


class QueuePocError(RuntimeError):
    """Base error raised by the T-005 queue POC module."""


class IdempotencyConflictError(ValueError):
    """Raised when an Idempotency-Key is reused with a different request payload."""


class InvalidUploadStateError(ValueError):
    """Raised when an upload record is missing, invalid, or in an unexpected state."""


class InjectedPostEnqueueError(QueuePocError):
    """Deterministic post-enqueue fault injected to verify same-transaction rollback."""


@dataclass(frozen=True, slots=True)
class QueuePocConfig:
    """Validated queue, lease, heartbeat, reconciler, and retention configuration."""

    lease_duration_s: float = 120.0
    heartbeat_interval_s: float = 30.0
    max_attempts: int = 5
    retry_backoff_base_s: float = 5.0
    reconcile_pending_grace_s: float = 30.0
    job_retention_days: int = 14
    queue_name: str = DEFAULT_QUEUE_NAME

    def __post_init__(self) -> None:
        if (
            isinstance(self.lease_duration_s, bool)
            or not math.isfinite(self.lease_duration_s)
            or self.lease_duration_s <= 0
        ):
            raise ValueError("lease_duration_s must be a finite positive number.")
        if (
            isinstance(self.heartbeat_interval_s, bool)
            or not math.isfinite(self.heartbeat_interval_s)
            or self.heartbeat_interval_s <= 0
        ):
            raise ValueError("heartbeat_interval_s must be a finite positive number.")
        if self.heartbeat_interval_s >= self.lease_duration_s:
            raise ValueError("heartbeat_interval_s must be strictly less than lease_duration_s.")
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if (
            isinstance(self.retry_backoff_base_s, bool)
            or not math.isfinite(self.retry_backoff_base_s)
            or self.retry_backoff_base_s < 0
        ):
            raise ValueError("retry_backoff_base_s must be a finite non-negative number.")
        if (
            isinstance(self.reconcile_pending_grace_s, bool)
            or not math.isfinite(self.reconcile_pending_grace_s)
            or self.reconcile_pending_grace_s < 0
        ):
            raise ValueError("reconcile_pending_grace_s must be a finite non-negative number.")
        if isinstance(self.job_retention_days, bool) or self.job_retention_days < 1:
            raise ValueError("job_retention_days must be at least 1.")
        if not self.queue_name or not self.queue_name.strip():
            raise ValueError("queue_name must be a non-empty string.")

    def retry_delay_s(self, attempt_count: int) -> float:
        """Compute bounded exponential retry delay in seconds for the given attempt number."""
        exponent = max(0, attempt_count - 1)
        return float(self.retry_backoff_base_s * (2**exponent))


@dataclass(frozen=True, slots=True)
class ConfirmUploadResult:
    """Outcome of atomic upload confirmation and same-connection job deferral."""

    upload_id: UUID
    status: str
    overall_difficulty: int
    procrastinate_job_id: int | None
    idempotency_replayed: bool
    xact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Outcome of attempting to claim a PENDING/RETRY upload into PROCESSING."""

    claimed: bool
    upload_id: UUID
    attempt_id: UUID | None
    attempt_count: int
    status: str | None
    processing_started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Outcome of a universally fenced PROCESSING -> target_status transition."""

    applied: bool
    upload_id: UUID
    attempt_id: UUID | None
    target_status: str
    fenced_out: bool
    track_id: str | None = None
    graph_revision: int | None = None
    error_category: str | None = None
    safe_error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Summary of one stale-lease reclamation and orphaned-enqueue reconciliation pass."""

    reclaimed_to_retry: tuple[UUID, ...]
    exhausted_to_technical_failed: tuple[UUID, ...]
    reenqueued_orphans: tuple[UUID, ...]
    reenqueued_job_ids: tuple[int, ...]
    skipped_healthy_processing: int


@dataclass(frozen=True, slots=True)
class QueuePocEvaluation:
    """Structured Go/No-Go decision for T-005 Queue POC (ARCHITECTURE.md §4.5, §9)."""

    decision: QueueDecision
    same_tx_enqueue_verified: bool
    rollback_atomicity_verified: bool
    idempotent_confirm_and_delivery_verified: bool
    sigkill_recovery_verified: bool
    healthy_long_job_preserved: bool
    universal_fencing_verified: bool
    orphan_reconciler_verified: bool
    procrastinate_stalled_jobs_verified: bool
    job_retention_verified: bool
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize evaluation summary into a JSON-compatible dictionary."""
        return asdict(self)


def evaluate_queue_poc(
    *,
    same_tx_enqueue_verified: bool,
    rollback_atomicity_verified: bool,
    idempotent_confirm_and_delivery_verified: bool,
    sigkill_recovery_verified: bool,
    healthy_long_job_preserved: bool,
    universal_fencing_verified: bool,
    orphan_reconciler_verified: bool,
    procrastinate_stalled_jobs_verified: bool,
    job_retention_verified: bool,
    custom_skip_locked_justification: str | None = None,
) -> QueuePocEvaluation:
    """Evaluate all Queue POC criteria and return the architectural Go/No-Go decision."""
    checks = {
        "same_tx_enqueue": same_tx_enqueue_verified,
        "rollback_atomicity": rollback_atomicity_verified,
        "idempotent_confirm_and_delivery": idempotent_confirm_and_delivery_verified,
        "sigkill_recovery": sigkill_recovery_verified,
        "healthy_long_job_preserved": healthy_long_job_preserved,
        "universal_fencing": universal_fencing_verified,
        "orphan_reconciler": orphan_reconciler_verified,
        "procrastinate_stalled_jobs": procrastinate_stalled_jobs_verified,
        "job_retention": job_retention_verified,
    }
    failed = [name for name, passed in checks.items() if not passed]

    if not failed:
        decision: QueueDecision = "GO_PROCRASTINATE"
        rationale = (
            "All T-005 Queue POC criteria passed on PostgreSQL + Procrastinate 3 + "
            "SQLAlchemy 2/psycopg v3 same-connection deferral, lease liveness, "
            "universal active_attempt_id fencing, SIGKILL recovery, and retention."
        )
    elif custom_skip_locked_justification and custom_skip_locked_justification.strip():
        decision = "FALLBACK_SKIP_LOCKED"
        rationale = (
            f"Procrastinate failed checks ({', '.join(failed)}); custom SKIP LOCKED selected "
            f"with documented justification: {custom_skip_locked_justification.strip()}"
        )
    else:
        decision = "FALLBACK_RABBITMQ_CELERY_OUTBOX"
        rationale = (
            f"Procrastinate failed checks ({', '.join(failed)}); fallback order requires "
            "RabbitMQ + Celery + Transactional Outbox (ARCHITECTURE.md §4.5)."
        )

    return QueuePocEvaluation(
        decision=decision,
        same_tx_enqueue_verified=same_tx_enqueue_verified,
        rollback_atomicity_verified=rollback_atomicity_verified,
        idempotent_confirm_and_delivery_verified=idempotent_confirm_and_delivery_verified,
        sigkill_recovery_verified=sigkill_recovery_verified,
        healthy_long_job_preserved=healthy_long_job_preserved,
        universal_fencing_verified=universal_fencing_verified,
        orphan_reconciler_verified=orphan_reconciler_verified,
        procrastinate_stalled_jobs_verified=procrastinate_stalled_jobs_verified,
        job_retention_verified=job_retention_verified,
        rationale=rationale,
    )


def to_psycopg_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy PostgreSQL URL (e.g. postgresql+psycopg://) into a libpq DSN."""
    parts = urlsplit(database_url)
    scheme = parts.scheme
    if "+" in scheme:
        base_scheme = scheme.split("+", 1)[0]
        if base_scheme in ("postgresql", "postgres"):
            scheme = "postgresql"
    elif scheme == "postgres":
        scheme = "postgresql"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _adapt_psycopg_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Adapt Python mapping values as PostgreSQL JSONB for psycopg v3 parameterized queries."""
    adapted: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, dict):
            adapted[key] = Jsonb(value)
        elif isinstance(value, list) and any(isinstance(item, dict) for item in value):
            adapted[key] = [Jsonb(item) if isinstance(item, dict) else item for item in value]
        else:
            adapted[key] = value
    return adapted


if TYPE_CHECKING:

    class _ProcrastinateBaseConnector:
        pass
else:
    _ProcrastinateBaseConnector = procrastinate_connector.BaseConnector


class SqlAlchemySessionConnector(_ProcrastinateBaseConnector):
    """Thin Procrastinate sync connector sharing an active SQLAlchemy 2 psycopg v3 transaction.

    Every query executes directly on `session.connection().connection.driver_connection` so
    Procrastinate job deferral shares the exact same PostgreSQL transaction (`pg_current_xact_id`)
    as SQLAlchemy ORM/Core writes and rolls back atomically on `session.rollback()`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_sync_connector(self) -> procrastinate_connector.BaseConnector:
        return self

    def open(self, pool: Any = None) -> None:
        _ = pool

    def close(self) -> None:
        return None

    async def open_async(self, pool: Any = None) -> None:
        _ = pool

    async def close_async(self) -> None:
        return None

    def _driver_connection(self) -> Any:
        sa_conn = self._session.connection()
        raw_conn = sa_conn.connection
        driver_conn = getattr(raw_conn, "driver_connection", None)
        if driver_conn is None:
            driver_conn = getattr(raw_conn, "dbapi_connection", raw_conn)
        return driver_conn

    def execute_query(self, query: str, **arguments: Any) -> None:
        driver_conn = self._driver_connection()
        params = _adapt_psycopg_arguments(arguments)
        with driver_conn.cursor() as cursor:
            cursor.execute(query, params or None)

    def execute_query_one(self, query: str, **arguments: Any) -> dict[str, Any]:
        driver_conn = self._driver_connection()
        params = _adapt_psycopg_arguments(arguments)
        with driver_conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params or None)
            row = cursor.fetchone()
            return dict(row) if row is not None else {}

    def execute_query_all(self, query: str, **arguments: Any) -> list[dict[str, Any]]:
        driver_conn = self._driver_connection()
        params = _adapt_psycopg_arguments(arguments)
        with driver_conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params or None)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    async def execute_query_async(self, query: str, **arguments: Any) -> None:
        self.execute_query(query, **arguments)

    async def execute_query_one_async(self, query: str, **arguments: Any) -> dict[str, Any]:
        return self.execute_query_one(query, **arguments)

    async def execute_query_all_async(self, query: str, **arguments: Any) -> list[dict[str, Any]]:
        return self.execute_query_all(query, **arguments)

    async def listen_notify(
        self,
        on_notification: Callable[[Any], Any],
        channels: list[str],
    ) -> None:
        raise NotImplementedError(
            "SqlAlchemySessionConnector supports transactional deferral only; "
            "use PsycopgConnector for Procrastinate workers."
        )


def apply_queue_poc_schema(engine: Engine) -> None:
    """Idempotently apply Procrastinate's real schema and the isolated POC idempotency table."""
    with engine.connect() as conn:
        existing_jobs_table: str | None = conn.execute(
            text("SELECT to_regclass('public.procrastinate_jobs')")
        ).scalar_one()

    if existing_jobs_table is None:
        dsn = to_psycopg_dsn(engine.url.render_as_string(hide_password=False))
        sync_connector = procrastinate.SyncPsycopgConnector(conninfo=dsn)
        app = procrastinate.App(connector=sync_connector)
        with app.open():
            schema_manager = procrastinate_schema.SchemaManager(connector=sync_connector)
            schema_manager.apply_schema()

    with engine.begin() as conn:
        conn.execute(text(_CREATE_POC_IDEMPOTENCY_TABLE_SQL))


def _compute_confirm_fingerprint(upload_id: UUID, overall_difficulty: int) -> str:
    payload = f"{CONFIRM_UPLOAD_OPERATION}:{upload_id}:{overall_difficulty}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enqueue_upload_job_in_session(
    session: Session,
    upload_id: UUID,
    *,
    queue_name: str = DEFAULT_QUEUE_NAME,
    priority: int = 0,
    lock: str | None = None,
    queueing_lock: str | None = None,
    scheduled_at: datetime | None = None,
) -> int:
    """Defer a real Procrastinate job inside the caller's active SQLAlchemy transaction."""
    tx_connector = SqlAlchemySessionConnector(session)
    app = procrastinate.App(connector=tx_connector)
    configure_kwargs: dict[str, Any] = {
        "queue": queue_name,
        "priority": priority,
        "lock": lock,
        "queueing_lock": queueing_lock,
    }
    if scheduled_at is not None:
        configure_kwargs["schedule_at"] = scheduled_at

    job_id = app.configure_task(
        name=INGEST_UPLOAD_TASK_NAME,
        **configure_kwargs,
    ).defer(upload_id=str(upload_id))
    return int(job_id)


def confirm_upload_and_enqueue(
    session: Session,
    upload_id: UUID,
    overall_difficulty: int,
    *,
    idempotency_key: str | None = None,
    inject_post_enqueue_error: bool = False,
    queue_name: str = DEFAULT_QUEUE_NAME,
) -> ConfirmUploadResult:
    """Atomically confirm a DRAFT upload into PENDING and enqueue its Procrastinate job.

    Executes inside the caller's SQLAlchemy transaction (no internal commit):
    1. Enforces Idempotency-Key replay and payload-fingerprint mismatch rejection (AC-60(a), AC-83).
    2. Locks the `uploads` row `FOR UPDATE` and validates `status == 'DRAFT'`.
    3. Transitions `DRAFT -> PENDING` with `overall_difficulty` and `accepted_at = now()`.
    4. Defers a Procrastinate job on the exact same psycopg v3 connection (`pg_current_xact_id`).
    5. Optionally raises `InjectedPostEnqueueError` after deferral to prove atomic rollback (AC-59).
    """
    if isinstance(overall_difficulty, bool) or overall_difficulty not in (1, 2, 3):
        raise InvalidUploadStateError(
            f"overall_difficulty must be 1, 2, or 3; got {overall_difficulty!r}."
        )

    fingerprint = _compute_confirm_fingerprint(upload_id, overall_difficulty)

    if idempotency_key is not None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty when provided.")
        session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtext(:op), hashtext(:key)"
                ")"
            ),
            {"op": CONFIRM_UPLOAD_OPERATION, "key": idempotency_key},
        )
        existing_idem = session.execute(
            text(
                """
                SELECT payload_fingerprint, upload_id, status_code, response_json
                FROM poc_idempotency_records
                WHERE operation = :op AND key = :key
                FOR UPDATE
                """
            ),
            {"op": CONFIRM_UPLOAD_OPERATION, "key": idempotency_key},
        ).mappings().first()

        if existing_idem is not None:
            if str(existing_idem["payload_fingerprint"]) != fingerprint:
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used with a different upload confirmation payload."
                )
            response_payload = cast(dict[str, Any], existing_idem["response_json"])
            stored_job_id = response_payload.get("procrastinate_job_id")
            return ConfirmUploadResult(
                upload_id=UUID(str(existing_idem["upload_id"])),
                status=str(response_payload.get("status", "PENDING")),
                overall_difficulty=int(
                    response_payload.get("overall_difficulty", overall_difficulty)
                ),
                procrastinate_job_id=int(stored_job_id) if stored_job_id is not None else None,
                idempotency_replayed=True,
                xact_id=(
                    str(response_payload["xact_id"])
                    if response_payload.get("xact_id") is not None
                    else None
                ),
            )

    upload_row = session.execute(
        text(
            """
            SELECT id, status, overall_difficulty
            FROM uploads
            WHERE id = :upload_id
            FOR UPDATE
            """
        ),
        {"upload_id": upload_id},
    ).mappings().first()

    if upload_row is None:
        raise InvalidUploadStateError(f"Upload {upload_id} does not exist.")
    if str(upload_row["status"]) != "DRAFT":
        raise InvalidUploadStateError(
            f"Upload {upload_id} must be DRAFT to confirm; found {upload_row['status']!r}."
        )

    xact_id = str(session.execute(text("SELECT pg_current_xact_id()::text")).scalar_one())

    updated = cast(
        CursorResult[Any],
        session.execute(
            text(
                """
                UPDATE uploads
                SET status = 'PENDING',
                    overall_difficulty = :overall_difficulty,
                    accepted_at = now()
                WHERE id = :upload_id
                  AND status = 'DRAFT'
                """
            ),
            {"upload_id": upload_id, "overall_difficulty": overall_difficulty},
        ),
    )
    if updated.rowcount != 1:
        raise InvalidUploadStateError(
            f"Upload {upload_id} could not transition from DRAFT to PENDING."
        )

    job_id = enqueue_upload_job_in_session(
        session,
        upload_id,
        queue_name=queue_name,
        queueing_lock=f"upload:{upload_id}",
    )

    if idempotency_key is not None:
        response_data: dict[str, Any] = {
            "upload_id": str(upload_id),
            "status": "PENDING",
            "overall_difficulty": overall_difficulty,
            "procrastinate_job_id": job_id,
            "xact_id": xact_id,
        }
        session.execute(
            text(
                """
                INSERT INTO poc_idempotency_records (
                    operation,
                    key,
                    payload_fingerprint,
                    upload_id,
                    status_code,
                    response_json
                )
                VALUES (
                    :op,
                    :key,
                    :fingerprint,
                    :upload_id,
                    202,
                    CAST(:response_json AS jsonb)
                )
                """
            ),
            {
                "op": CONFIRM_UPLOAD_OPERATION,
                "key": idempotency_key,
                "fingerprint": fingerprint,
                "upload_id": upload_id,
                "response_json": json.dumps(response_data),
            },
        )

    if inject_post_enqueue_error:
        raise InjectedPostEnqueueError(
            f"Injected post-enqueue failure for upload {upload_id} (job {job_id})."
        )

    return ConfirmUploadResult(
        upload_id=upload_id,
        status="PENDING",
        overall_difficulty=overall_difficulty,
        procrastinate_job_id=job_id,
        idempotency_replayed=False,
        xact_id=xact_id,
    )


def claim_upload_attempt(
    session: Session,
    upload_id: UUID,
    *,
    lease_duration_s: float = 120.0,
    attempt_id: UUID | None = None,
) -> ClaimResult:
    """Atomically claim a PENDING or RETRY upload into PROCESSING with a fresh lease."""
    if not math.isfinite(lease_duration_s) or lease_duration_s <= 0:
        raise ValueError("lease_duration_s must be a finite positive number.")

    new_attempt_id = attempt_id or uuid4()
    row = session.execute(
        text(
            """
            UPDATE uploads
            SET status = 'PROCESSING',
                active_attempt_id = :attempt_id,
                attempt_count = attempt_count + 1,
                processing_started_at = now(),
                last_heartbeat_at = now(),
                lease_expires_at = now() + make_interval(
                    secs => CAST(:lease_duration_s AS double precision)
                )
            WHERE id = :upload_id
              AND status IN ('PENDING', 'RETRY')
            RETURNING
                id,
                active_attempt_id,
                attempt_count,
                status,
                processing_started_at,
                last_heartbeat_at,
                lease_expires_at
            """
        ),
        {
            "upload_id": upload_id,
            "attempt_id": new_attempt_id,
            "lease_duration_s": float(lease_duration_s),
        },
    ).mappings().first()

    if row is not None:
        return ClaimResult(
            claimed=True,
            upload_id=UUID(str(row["id"])),
            attempt_id=UUID(str(row["active_attempt_id"])),
            attempt_count=int(row["attempt_count"]),
            status=str(row["status"]),
            processing_started_at=cast(datetime | None, row["processing_started_at"]),
            last_heartbeat_at=cast(datetime | None, row["last_heartbeat_at"]),
            lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
        )

    existing = session.execute(
        text("SELECT status, attempt_count FROM uploads WHERE id = :upload_id"),
        {"upload_id": upload_id},
    ).mappings().first()

    return ClaimResult(
        claimed=False,
        upload_id=upload_id,
        attempt_id=None,
        attempt_count=int(existing["attempt_count"]) if existing is not None else 0,
        status=str(existing["status"]) if existing is not None else None,
    )


def renew_attempt_lease(
    session: Session,
    upload_id: UUID,
    attempt_id: UUID,
    *,
    lease_duration_s: float = 120.0,
) -> bool:
    """Renew lease_expires_at and last_heartbeat_at for an active PROCESSING attempt."""
    if not math.isfinite(lease_duration_s) or lease_duration_s <= 0:
        raise ValueError("lease_duration_s must be a finite positive number.")

    result = cast(
        CursorResult[Any],
        session.execute(
            text(
                """
                UPDATE uploads
                SET last_heartbeat_at = now(),
                    lease_expires_at = now() + make_interval(
                        secs => CAST(:lease_duration_s AS double precision)
                    )
                WHERE id = :upload_id
                  AND status = 'PROCESSING'
                  AND active_attempt_id = :attempt_id
                """
            ),
            {
                "upload_id": upload_id,
                "attempt_id": attempt_id,
                "lease_duration_s": float(lease_duration_s),
            },
        ),
    )
    return bool(result.rowcount == 1)


class LeaseHeartbeatRunner:
    """Background daemon thread periodically renewing an active upload attempt lease (AC-79(b))."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        upload_id: UUID,
        attempt_id: UUID,
        *,
        lease_duration_s: float,
        heartbeat_interval_s: float,
    ) -> None:
        if not math.isfinite(lease_duration_s) or lease_duration_s <= 0:
            raise ValueError("lease_duration_s must be a finite positive number.")
        if not math.isfinite(heartbeat_interval_s) or heartbeat_interval_s <= 0:
            raise ValueError("heartbeat_interval_s must be a finite positive number.")
        if heartbeat_interval_s >= lease_duration_s:
            raise ValueError("heartbeat_interval_s must be strictly less than lease_duration_s.")

        self._session_factory = session_factory
        self._upload_id = upload_id
        self._attempt_id = attempt_id
        self._lease_duration_s = float(lease_duration_s)
        self._heartbeat_interval_s = float(heartbeat_interval_s)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.renewals_succeeded: int = 0
        self.lease_lost: bool = False
        self.last_error: BaseException | None = None

    def start(self) -> None:
        """Start the background heartbeat loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self._upload_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Signal the heartbeat thread to stop and wait briefly for clean exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    def _run(self) -> None:
        while not self._stop_event.wait(self._heartbeat_interval_s):
            try:
                with self._session_factory() as session, session.begin():
                    renewed = renew_attempt_lease(
                        session,
                        self._upload_id,
                        self._attempt_id,
                        lease_duration_s=self._lease_duration_s,
                    )
                if renewed:
                    self.renewals_succeeded += 1
                else:
                    self.lease_lost = True
                    return
            except BaseException as exc:  # noqa: BLE001 - background thread diagnostic capture
                self.last_error = exc
                logger.warning(
                    "Heartbeat renewal error for upload %s attempt %s: %s",
                    self._upload_id,
                    self._attempt_id,
                    exc,
                )

    def __enter__(self) -> LeaseHeartbeatRunner:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        _ = (exc_type, exc, tb)
        self.stop()


def complete_upload_active(
    session: Session,
    upload_id: UUID,
    attempt_id: UUID,
    *,
    track_id: str | None = None,
    normalized_geom_wkt: str = DEFAULT_TRACK_WKT,
) -> TransitionResult:
    """Universally fenced `PROCESSING -> ACTIVE` completion (AC-80, ARCH §4.8 lock order).

    Uses a transaction savepoint so that if the fenced `uploads` update matches 0 rows
    (stale `active_attempt_id`), the `tracks` upsert rolls back cleanly and `graph_metadata`
    is never incremented.
    """
    savepoint = session.begin_nested()
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": GRAPH_WRITER_ADVISORY_LOCK_KEY},
        )

        resolved_track_id = track_id
        if resolved_track_id is None:
            fp = session.execute(
                text("SELECT normalized_fingerprint FROM uploads WHERE id = :upload_id"),
                {"upload_id": upload_id},
            ).scalar_one_or_none()
            if fp is None:
                savepoint.rollback()
                return TransitionResult(
                    applied=False,
                    upload_id=upload_id,
                    attempt_id=attempt_id,
                    target_status="ACTIVE",
                    fenced_out=True,
                )
            resolved_track_id = str(fp)

        session.execute(
            text(
                """
                INSERT INTO tracks (id, normalized_geom)
                VALUES (:track_id, ST_GeomFromText(:geom_wkt, 4326))
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"track_id": resolved_track_id, "geom_wkt": normalized_geom_wkt},
        )

        updated = cast(
            CursorResult[Any],
            session.execute(
                text(
                    """
                    UPDATE uploads
                    SET status = 'ACTIVE',
                        track_id = :track_id,
                        active_attempt_id = NULL,
                        lease_expires_at = NULL,
                        error_category = NULL,
                        safe_error_message = NULL,
                        processed_at = now()
                    WHERE id = :upload_id
                      AND status = 'PROCESSING'
                      AND active_attempt_id = :attempt_id
                    """
                ),
                {
                    "upload_id": upload_id,
                    "attempt_id": attempt_id,
                    "track_id": resolved_track_id,
                },
            ),
        )

        if updated.rowcount != 1:
            savepoint.rollback()
            return TransitionResult(
                applied=False,
                upload_id=upload_id,
                attempt_id=attempt_id,
                target_status="ACTIVE",
                fenced_out=True,
            )

        graph_revision = int(
            session.execute(
                text(
                    """
                    UPDATE graph_metadata
                    SET revision = revision + 1
                    WHERE id = 1
                    RETURNING revision
                    """
                )
            ).scalar_one()
        )
        savepoint.commit()
        return TransitionResult(
            applied=True,
            upload_id=upload_id,
            attempt_id=attempt_id,
            target_status="ACTIVE",
            fenced_out=False,
            track_id=resolved_track_id,
            graph_revision=graph_revision,
        )
    except Exception:
        if savepoint.is_active:
            savepoint.rollback()
        raise


def reject_upload_business(
    session: Session,
    upload_id: UUID,
    attempt_id: UUID,
    *,
    error_category: str = "DEGENERATE_GEOMETRY",
    safe_error_message: str = "Uploaded track geometry is invalid or degenerate.",
) -> TransitionResult:
    """Universally fenced `PROCESSING -> REJECTED` business rejection (AC-80)."""
    updated = cast(
        CursorResult[Any],
        session.execute(
            text(
                """
                UPDATE uploads
                SET status = 'REJECTED',
                    active_attempt_id = NULL,
                    lease_expires_at = NULL,
                    error_category = :error_category,
                    safe_error_message = :safe_error_message,
                    processed_at = now()
                WHERE id = :upload_id
                  AND status = 'PROCESSING'
                  AND active_attempt_id = :attempt_id
                """
            ),
            {
                "upload_id": upload_id,
                "attempt_id": attempt_id,
                "error_category": error_category,
                "safe_error_message": safe_error_message,
            },
        ),
    )
    applied = bool(updated.rowcount == 1)
    return TransitionResult(
        applied=applied,
        upload_id=upload_id,
        attempt_id=attempt_id,
        target_status="REJECTED",
        fenced_out=not applied,
        error_category=error_category if applied else None,
        safe_error_message=safe_error_message if applied else None,
    )


def mark_upload_retry(
    session: Session,
    upload_id: UUID,
    attempt_id: UUID,
    *,
    error_category: str = "TRANSIENT_PROCESSING_ERROR",
    safe_error_message: str = "Processing temporarily delayed; automatic retry scheduled.",
    enqueue_retry_job: bool = False,
    queue_name: str = DEFAULT_QUEUE_NAME,
) -> TransitionResult:
    """Universally fenced `PROCESSING -> RETRY` transition (AC-80)."""
    updated = cast(
        CursorResult[Any],
        session.execute(
            text(
                """
                UPDATE uploads
                SET status = 'RETRY',
                    active_attempt_id = NULL,
                    lease_expires_at = NULL,
                    error_category = :error_category,
                    safe_error_message = :safe_error_message
                WHERE id = :upload_id
                  AND status = 'PROCESSING'
                  AND active_attempt_id = :attempt_id
                """
            ),
            {
                "upload_id": upload_id,
                "attempt_id": attempt_id,
                "error_category": error_category,
                "safe_error_message": safe_error_message,
            },
        ),
    )
    applied = bool(updated.rowcount == 1)
    if applied and enqueue_retry_job:
        enqueue_upload_job_in_session(
            session,
            upload_id,
            queue_name=queue_name,
            queueing_lock=f"upload:{upload_id}",
        )
    return TransitionResult(
        applied=applied,
        upload_id=upload_id,
        attempt_id=attempt_id,
        target_status="RETRY",
        fenced_out=not applied,
        error_category=error_category if applied else None,
        safe_error_message=safe_error_message if applied else None,
    )


def mark_upload_technical_failed(
    session: Session,
    upload_id: UUID,
    attempt_id: UUID,
    *,
    error_category: str = "TECHNICAL_EXHAUSTED",
    safe_error_message: str = (
        "Track processing is delayed. Your contribution credit is preserved while we investigate."
    ),
) -> TransitionResult:
    """Universally fenced `PROCESSING -> TECHNICAL_FAILED` transition retaining credit (AC-80)."""
    updated = cast(
        CursorResult[Any],
        session.execute(
            text(
                """
                UPDATE uploads
                SET status = 'TECHNICAL_FAILED',
                    active_attempt_id = NULL,
                    lease_expires_at = NULL,
                    error_category = :error_category,
                    safe_error_message = :safe_error_message,
                    processed_at = now()
                WHERE id = :upload_id
                  AND status = 'PROCESSING'
                  AND active_attempt_id = :attempt_id
                """
            ),
            {
                "upload_id": upload_id,
                "attempt_id": attempt_id,
                "error_category": error_category,
                "safe_error_message": safe_error_message,
            },
        ),
    )
    applied = bool(updated.rowcount == 1)
    return TransitionResult(
        applied=applied,
        upload_id=upload_id,
        attempt_id=attempt_id,
        target_status="TECHNICAL_FAILED",
        fenced_out=not applied,
        error_category=error_category if applied else None,
        safe_error_message=safe_error_message if applied else None,
    )


def _has_active_procrastinate_job(session: Session, upload_id: UUID) -> bool:
    exists: Any = session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM procrastinate_jobs
                WHERE task_name = :task_name
                  AND status IN ('todo', 'doing')
                  AND args->>'upload_id' = :upload_id_str
            )
            """
        ),
        {"task_name": INGEST_UPLOAD_TASK_NAME, "upload_id_str": str(upload_id)},
    ).scalar_one()
    return bool(exists)


def reconcile_stalled_and_orphaned(
    session: Session,
    *,
    pending_grace_s: float = 30.0,
    max_attempts: int = 5,
    queue_name: str = DEFAULT_QUEUE_NAME,
) -> ReconcileResult:
    """Reclaim genuinely abandoned PROCESSING rows and re-enqueue orphaned PENDING/RETRY rows.

    Invariants (ARCH §4.5, AC-63, AC-79):
    - Reclaims `PROCESSING` rows **exclusively** when `lease_expires_at < now()`, never based on
      `processing_started_at`, so healthy long-running jobs renewing heartbeats are never touched.
    - Exhausted attempts (`attempt_count >= max_attempts`) move to `TECHNICAL_FAILED` (retaining
      provisional credit); otherwise move to `RETRY` and enqueue a recovery job atomically.
    - Any `PENDING` or `RETRY` row older than `pending_grace_s` with no active (`todo`/`doing`)
      Procrastinate job is re-enqueued inside the same transaction.
    """
    if not math.isfinite(pending_grace_s) or pending_grace_s < 0:
        raise ValueError("pending_grace_s must be a finite non-negative number.")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")

    skipped_healthy = int(
        session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM uploads
                WHERE status = 'PROCESSING'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at >= now()
                """
            )
        ).scalar_one()
    )

    expired_rows = session.execute(
        text(
            """
            SELECT id, attempt_count
            FROM uploads
            WHERE status = 'PROCESSING'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < now()
            ORDER BY lease_expires_at ASC
            FOR UPDATE SKIP LOCKED
            """
        )
    ).mappings().all()

    reclaimed_to_retry: list[UUID] = []
    exhausted_to_technical_failed: list[UUID] = []
    reenqueued_orphans: list[UUID] = []
    reenqueued_job_ids: list[int] = []
    handled_ids: set[UUID] = set()

    for row in expired_rows:
        uid = UUID(str(row["id"]))
        attempts = int(row["attempt_count"])
        handled_ids.add(uid)

        if attempts >= max_attempts:
            session.execute(
                text(
                    """
                    UPDATE uploads
                    SET status = 'TECHNICAL_FAILED',
                        active_attempt_id = NULL,
                        lease_expires_at = NULL,
                        error_category = 'LEASE_EXPIRED_EXHAUSTED',
                        safe_error_message = :msg,
                        processed_at = now()
                    WHERE id = :upload_id
                      AND status = 'PROCESSING'
                      AND lease_expires_at < now()
                    """
                ),
                {
                    "upload_id": uid,
                    "msg": (
                        "Track processing is delayed after maximum retry attempts. "
                        "Your contribution credit is preserved."
                    ),
                },
            )
            exhausted_to_technical_failed.append(uid)
        else:
            session.execute(
                text(
                    """
                    UPDATE uploads
                    SET status = 'RETRY',
                        active_attempt_id = NULL,
                        lease_expires_at = NULL,
                        error_category = 'WORKER_LEASE_EXPIRED',
                        safe_error_message = 'Worker lease expired; automatic retry scheduled.'
                    WHERE id = :upload_id
                      AND status = 'PROCESSING'
                      AND lease_expires_at < now()
                    """
                ),
                {"upload_id": uid},
            )
            reclaimed_to_retry.append(uid)
            # Clear any stale `doing` Procrastinate job reference check by enqueuing a fresh
            # recovery job if no `todo` job is already queued for this upload.
            has_todo = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM procrastinate_jobs
                            WHERE task_name = :task_name
                              AND status = 'todo'
                              AND args->>'upload_id' = :upload_id_str
                        )
                        """
                    ),
                    {"task_name": INGEST_UPLOAD_TASK_NAME, "upload_id_str": str(uid)},
                ).scalar_one()
            )
            if not has_todo:
                job_id = enqueue_upload_job_in_session(
                    session,
                    uid,
                    queue_name=queue_name,
                    queueing_lock=f"upload:{uid}",
                )
                reenqueued_job_ids.append(job_id)

    orphan_rows = session.execute(
        text(
            """
            SELECT u.id
            FROM uploads AS u
            WHERE u.status IN ('PENDING', 'RETRY')
              AND COALESCE(u.last_heartbeat_at, u.accepted_at, u.created_at)
                  <= now() - make_interval(secs => CAST(:pending_grace_s AS double precision))
              AND NOT EXISTS (
                  SELECT 1
                  FROM procrastinate_jobs AS j
                  WHERE j.task_name = :task_name
                    AND j.status IN ('todo', 'doing')
                    AND j.args->>'upload_id' = u.id::text
              )
            ORDER BY u.created_at ASC
            FOR UPDATE SKIP LOCKED
            """
        ),
        {
            "pending_grace_s": float(pending_grace_s),
            "task_name": INGEST_UPLOAD_TASK_NAME,
        },
    ).mappings().all()

    for row in orphan_rows:
        uid = UUID(str(row["id"]))
        if uid in handled_ids:
            continue
        if _has_active_procrastinate_job(session, uid):
            continue
        job_id = enqueue_upload_job_in_session(
            session,
            uid,
            queue_name=queue_name,
            queueing_lock=f"upload:{uid}",
        )
        reenqueued_orphans.append(uid)
        reenqueued_job_ids.append(job_id)

    return ReconcileResult(
        reclaimed_to_retry=tuple(reclaimed_to_retry),
        exhausted_to_technical_failed=tuple(exhausted_to_technical_failed),
        reenqueued_orphans=tuple(reenqueued_orphans),
        reenqueued_job_ids=tuple(reenqueued_job_ids),
        skipped_healthy_processing=skipped_healthy,
    )


def count_qualifying_uploads(session: Session) -> int:
    """Count uploads currently holding provisional or permanent route-creation credit."""
    count: Any = session.execute(
        text(
            """
            SELECT COUNT(*)
            FROM uploads
            WHERE status IN ('PENDING', 'PROCESSING', 'RETRY', 'TECHNICAL_FAILED', 'ACTIVE')
            """
        )
    ).scalar_one()
    return int(count)


def query_procrastinate_jobs_for_upload(
    session: Session,
    upload_id: UUID,
) -> list[dict[str, Any]]:
    """Return all Procrastinate job rows associated with the given upload_id."""
    rows = session.execute(
        text(
            """
            SELECT id, queue_name, task_name, status, attempts, queueing_lock, args, scheduled_at
            FROM procrastinate_jobs
            WHERE task_name = :task_name
              AND args->>'upload_id' = :upload_id_str
            ORDER BY id ASC
            """
        ),
        {"task_name": INGEST_UPLOAD_TASK_NAME, "upload_id_str": str(upload_id)},
    ).mappings().all()
    return [dict(row) for row in rows]


def process_upload_attempt(
    session_factory: sessionmaker[Session],
    upload_id: UUID,
    *,
    config: QueuePocConfig | None = None,
    normalized_geom_wkt: str = DEFAULT_TRACK_WKT,
    hold_before_commit_s: float = 0.0,
    enable_heartbeat: bool = True,
    ready_file: Path | None = None,
    simulate_business_rejection: str | None = None,
    simulate_technical_error: str | None = None,
) -> TransitionResult:
    """Execute one idempotent worker attempt for an upload with optional heartbeat & hold.

    - Double delivery (`AC-60(b)`): if the upload is already `ACTIVE`, `REJECTED`,
      `TECHNICAL_FAILED`, or actively `PROCESSING`, `claim_upload_attempt` returns `claimed=False`
      and this handler exits as a clean no-op without duplicating tracks or graph revisions.
    - Crash testing (`AC-63`, `AC-79(a)`): writes a JSON marker to `ready_file` immediately after
      committing `PROCESSING` so an external harness can deliver `SIGKILL` mid-`PROCESSING`.
    - Long-job liveness (`AC-79(b)`): keeps `LeaseHeartbeatRunner` renewing `lease_expires_at`
      while `hold_before_commit_s` elapses.
    """
    cfg = config or QueuePocConfig()

    with session_factory() as session, session.begin():
        claim = claim_upload_attempt(
            session,
            upload_id,
            lease_duration_s=cfg.lease_duration_s,
        )

    if not claim.claimed or claim.attempt_id is None:
        return TransitionResult(
            applied=False,
            upload_id=upload_id,
            attempt_id=None,
            target_status=claim.status or "UNKNOWN",
            fenced_out=False,
        )

    attempt_id = claim.attempt_id
    heartbeat: LeaseHeartbeatRunner | None = None
    if enable_heartbeat:
        heartbeat = LeaseHeartbeatRunner(
            session_factory,
            upload_id,
            attempt_id,
            lease_duration_s=cfg.lease_duration_s,
            heartbeat_interval_s=cfg.heartbeat_interval_s,
        )
        heartbeat.start()

    try:
        if ready_file is not None:
            tmp_path = ready_file.with_suffix(f"{ready_file.suffix}.tmp")
            payload = {
                "upload_id": str(upload_id),
                "attempt_id": str(attempt_id),
                "attempt_count": claim.attempt_count,
                "pid": os.getpid(),
            }
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_path, ready_file)

        if hold_before_commit_s > 0:
            deadline = time.monotonic() + hold_before_commit_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
    finally:
        if heartbeat is not None:
            heartbeat.stop()

    with session_factory() as session, session.begin():
        if simulate_business_rejection is not None:
            return reject_upload_business(
                session,
                upload_id,
                attempt_id,
                error_category=simulate_business_rejection,
            )
        if simulate_technical_error is not None:
            if claim.attempt_count >= cfg.max_attempts:
                return mark_upload_technical_failed(
                    session,
                    upload_id,
                    attempt_id,
                    error_category=simulate_technical_error,
                )
            return mark_upload_retry(
                session,
                upload_id,
                attempt_id,
                error_category=simulate_technical_error,
                enqueue_retry_job=True,
                queue_name=cfg.queue_name,
            )
        return complete_upload_active(
            session,
            upload_id,
            attempt_id,
            normalized_geom_wkt=normalized_geom_wkt,
        )


def build_procrastinate_app(
    database_url: str,
    *,
    config: QueuePocConfig | None = None,
    sync_only: bool = False,
    hold_before_commit_s: float = 0.0,
    enable_heartbeat: bool = True,
    ready_file: Path | None = None,
) -> procrastinate.App:
    """Create a Procrastinate App registered with the POC `ingest_upload` task."""
    cfg = config or QueuePocConfig()
    dsn = to_psycopg_dsn(database_url)
    connector: Any = (
        procrastinate.SyncPsycopgConnector(conninfo=dsn)
        if sync_only
        else procrastinate.PsycopgConnector(conninfo=dsn)
    )
    app = procrastinate.App(connector=connector)

    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    def ingest_upload(upload_id: str) -> dict[str, Any]:
        result = process_upload_attempt(
            session_factory,
            UUID(upload_id),
            config=cfg,
            hold_before_commit_s=hold_before_commit_s,
            enable_heartbeat=enable_heartbeat,
            ready_file=ready_file,
        )
        return {
            "applied": result.applied,
            "upload_id": str(result.upload_id),
            "attempt_id": str(result.attempt_id) if result.attempt_id is not None else None,
            "target_status": result.target_status,
            "fenced_out": result.fenced_out,
            "track_id": result.track_id,
            "graph_revision": result.graph_revision,
        }

    app.task(name=INGEST_UPLOAD_TASK_NAME, queue=cfg.queue_name)(ingest_upload)
    return app


async def _get_stalled_jobs_async(
    database_url: str,
    *,
    seconds_since_heartbeat: int,
    queue: str | None = None,
) -> list[int]:
    dsn = to_psycopg_dsn(database_url)
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn))
    async with app.open_async():
        stalled = await app.job_manager.get_stalled_jobs(
            nb_seconds=seconds_since_heartbeat,
            queue=queue,
        )
        return [int(job.id) for job in stalled if job.id is not None]


def inspect_procrastinate_stalled_jobs(
    database_url: str,
    *,
    seconds_since_heartbeat: int = 30,
    queue: str | None = None,
) -> list[int]:
    """Query Procrastinate's built-in stalled-job detection (`JobManager.get_stalled_jobs`)."""
    if seconds_since_heartbeat < 0:
        raise ValueError("seconds_since_heartbeat must be non-negative.")
    return asyncio.run(
        _get_stalled_jobs_async(
            database_url,
            seconds_since_heartbeat=seconds_since_heartbeat,
            queue=queue,
        )
    )


async def _purge_old_jobs_async(
    database_url: str,
    *,
    nb_hours: int,
    queue: str | None,
    include_failed: bool,
) -> None:
    dsn = to_psycopg_dsn(database_url)
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn))
    async with app.open_async():
        await app.job_manager.delete_old_jobs(
            nb_hours=nb_hours,
            queue=queue,
            include_failed=include_failed,
        )


def purge_old_procrastinate_jobs(
    database_url: str,
    *,
    retention_days: int = 14,
    queue: str | None = None,
    include_failed: bool = False,
) -> int:
    """Delete finished Procrastinate jobs older than `retention_days` and return deleted count."""
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative.")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            before_count = int(
                conn.execute(text("SELECT COUNT(*) FROM procrastinate_jobs")).scalar_one()
            )
        asyncio.run(
            _purge_old_jobs_async(
                database_url,
                nb_hours=retention_days * 24,
                queue=queue,
                include_failed=include_failed,
            )
        )
        with engine.connect() as conn:
            after_count = int(
                conn.execute(text("SELECT COUNT(*) FROM procrastinate_jobs")).scalar_one()
            )
        return max(0, before_count - after_count)
    finally:
        engine.dispose()


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.queue_poc",
        description="T-005 Queue POC subprocess worker and reconciler CLI.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="SQLAlchemy PostgreSQL URL (defaults to DATABASE_URL env var).",
    )
    parser.add_argument(
        "--mode",
        choices=("worker", "process-one", "reconcile"),
        default="worker",
        help="Execution mode: Procrastinate worker, single upload processor, or reconciler.",
    )
    parser.add_argument("--upload-id", default=None, help="Upload UUID for --mode=process-one.")
    parser.add_argument("--queue-name", default=DEFAULT_QUEUE_NAME)
    parser.add_argument("--lease-duration-s", type=float, default=120.0)
    parser.add_argument("--heartbeat-interval-s", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--pending-grace-s", type=float, default=30.0)
    parser.add_argument("--hold-before-commit-s", type=float, default=0.0)
    parser.add_argument(
        "--ready-file",
        default=None,
        help="Optional path written once upload enters PROCESSING (for SIGKILL test sync).",
    )
    parser.add_argument(
        "--no-heartbeat",
        action="store_true",
        help="Disable background lease heartbeat renewal.",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="In worker mode, exit once all currently queued jobs have been processed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint supporting genuine subprocess SIGKILL tests (AC-63, AC-79)."""
    args = _build_cli_parser().parse_args(argv)
    if not args.database_url:
        raise SystemExit("--database-url (or DATABASE_URL env var) is required.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    cfg = QueuePocConfig(
        lease_duration_s=args.lease_duration_s,
        heartbeat_interval_s=args.heartbeat_interval_s,
        max_attempts=args.max_attempts,
        reconcile_pending_grace_s=args.pending_grace_s,
        queue_name=args.queue_name,
    )
    ready_path = Path(args.ready_file) if args.ready_file else None

    if args.mode == "process-one":
        if not args.upload_id:
            raise SystemExit("--upload-id is required for --mode=process-one.")
        engine = create_engine(args.database_url, pool_pre_ping=True)
        try:
            session_factory = sessionmaker(
                bind=engine,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
            result = process_upload_attempt(
                session_factory,
                UUID(args.upload_id),
                config=cfg,
                hold_before_commit_s=args.hold_before_commit_s,
                enable_heartbeat=not args.no_heartbeat,
                ready_file=ready_path,
            )
            print(
                json.dumps(
                    {
                        "applied": result.applied,
                        "upload_id": str(result.upload_id),
                        "attempt_id": (
                            str(result.attempt_id) if result.attempt_id is not None else None
                        ),
                        "target_status": result.target_status,
                        "fenced_out": result.fenced_out,
                        "track_id": result.track_id,
                        "graph_revision": result.graph_revision,
                    }
                )
            )
            return 0
        finally:
            engine.dispose()

    if args.mode == "reconcile":
        engine = create_engine(args.database_url, pool_pre_ping=True)
        try:
            session_factory = sessionmaker(
                bind=engine,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
            with session_factory() as session, session.begin():
                rec = reconcile_stalled_and_orphaned(
                    session,
                    pending_grace_s=cfg.reconcile_pending_grace_s,
                    max_attempts=cfg.max_attempts,
                    queue_name=cfg.queue_name,
                )
            print(
                json.dumps(
                    {
                        "reclaimed_to_retry": [str(u) for u in rec.reclaimed_to_retry],
                        "exhausted_to_technical_failed": [
                            str(u) for u in rec.exhausted_to_technical_failed
                        ],
                        "reenqueued_orphans": [str(u) for u in rec.reenqueued_orphans],
                        "reenqueued_job_ids": list(rec.reenqueued_job_ids),
                        "skipped_healthy_processing": rec.skipped_healthy_processing,
                    }
                )
            )
            return 0
        finally:
            engine.dispose()

    app = build_procrastinate_app(
        args.database_url,
        config=cfg,
        sync_only=False,
        hold_before_commit_s=args.hold_before_commit_s,
        enable_heartbeat=not args.no_heartbeat,
        ready_file=ready_path,
    )
    with app.open():
        app.run_worker(
            queues=[cfg.queue_name],
            wait=not args.one_shot,
            install_signal_handlers=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
