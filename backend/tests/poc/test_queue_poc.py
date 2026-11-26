"""Unit and Testcontainers integration tests for T-005 (Queue POC: Procrastinate + fencing).

Pure unit tests run without Docker (`pytest -m "not integration" tests/poc/test_queue_poc.py`).
PostGIS/pgRouting + Procrastinate integration tests are marked with `@pytest.mark.integration`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.workers.queue_poc import (
    CREDIT_BEARING_STATUSES,
    DEFAULT_QUEUE_NAME,
    INGEST_UPLOAD_TASK_NAME,
    IdempotencyConflictError,
    InjectedPostEnqueueError,
    InvalidUploadStateError,
    LeaseHeartbeatRunner,
    QueuePocConfig,
    apply_queue_poc_schema,
    claim_upload_attempt,
    complete_upload_active,
    confirm_upload_and_enqueue,
    count_qualifying_uploads,
    evaluate_queue_poc,
    inspect_procrastinate_stalled_jobs,
    mark_upload_retry,
    mark_upload_technical_failed,
    process_upload_attempt,
    purge_old_procrastinate_jobs,
    query_procrastinate_jobs_for_upload,
    reconcile_stalled_and_orphaned,
    reject_upload_business,
    renew_attempt_lease,
    retry_stalled_procrastinate_jobs,
    to_psycopg_dsn,
)
from tests.conftest import BACKEND_DIR, build_alembic_config

# ---------------------------------------------------------------------------
# Helpers for integration tests
# ---------------------------------------------------------------------------


def _insert_upload_row(
    session: Session,
    *,
    upload_id: UUID | None = None,
    filename: str = "carmel_trail.gpx",
    status: str = "DRAFT",
    overall_difficulty: int | None = None,
    accepted_at_ago_s: float | None = None,
    last_heartbeat_ago_s: float | None = None,
) -> UUID:
    uid = upload_id or uuid4()
    fp = f"fp-{uid.hex}"
    session.execute(
        text(
            """
            INSERT INTO uploads (
                id,
                filename,
                original_ref,
                file_sha256,
                normalized_fingerprint,
                status,
                overall_difficulty,
                accepted_at,
                last_heartbeat_at
            )
            VALUES (
                :id,
                :filename,
                :original_ref,
                :file_sha256,
                :normalized_fingerprint,
                :status,
                :overall_difficulty,
                now() - make_interval(secs => CAST(:accepted_at_ago_s AS double precision)),
                now() - make_interval(secs => CAST(:last_heartbeat_ago_s AS double precision))
            )
            """
        ),
        {
            "id": uid,
            "filename": filename,
            "original_ref": f"uploads/{uid}.gpx",
            "file_sha256": f"sha-{uid.hex}",
            "normalized_fingerprint": fp,
            "status": status,
            "overall_difficulty": overall_difficulty,
            "accepted_at_ago_s": accepted_at_ago_s,
            "last_heartbeat_ago_s": last_heartbeat_ago_s,
        },
    )
    return uid


def _fetch_upload_row(session: Session, upload_id: UUID) -> dict[str, Any]:
    row = session.execute(
        text("SELECT * FROM uploads WHERE id = :upload_id"),
        {"upload_id": upload_id},
    ).mappings().one()
    return dict(row)


def _count_tracks(session: Session) -> int:
    return int(session.execute(text("SELECT COUNT(*) FROM tracks")).scalar_one())


def _get_graph_revision(session: Session) -> int:
    return int(
        session.execute(text("SELECT revision FROM graph_metadata WHERE id = 1")).scalar_one()
    )


def _wait_for_ready_file(
    ready_file: Path,
    proc: subprocess.Popen[str],
    *,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ready_file.exists():
            return dict(json.loads(ready_file.read_text(encoding="utf-8")))
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=5)
            raise AssertionError(
                f"Worker subprocess exited early (rc={proc.returncode}) before writing "
                f"{ready_file}.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
        time.sleep(0.05)
    proc.kill()
    stdout, stderr = proc.communicate(timeout=5)
    raise AssertionError(
        f"Timed out after {timeout_s}s waiting for {ready_file}.\n"
        f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    )


def _spawn_worker_subprocess(
    database_url: str,
    *,
    lease_duration_s: float,
    heartbeat_interval_s: float,
    hold_before_commit_s: float = 0.0,
    ready_file: Path | None = None,
    one_shot: bool = False,
    queue_name: str = DEFAULT_QUEUE_NAME,
) -> subprocess.Popen[str]:
    cmd = [
        sys.executable,
        "-m",
        "app.workers.queue_poc",
        "--database-url",
        database_url,
        "--mode",
        "worker",
        "--queue-name",
        queue_name,
        "--lease-duration-s",
        str(lease_duration_s),
        "--heartbeat-interval-s",
        str(heartbeat_interval_s),
        "--hold-before-commit-s",
        str(hold_before_commit_s),
    ]
    if ready_file is not None:
        cmd.extend(["--ready-file", str(ready_file)])
    if one_shot:
        cmd.append("--one-shot")

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{BACKEND_DIR}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(BACKEND_DIR)
    )
    return subprocess.Popen(
        cmd,
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# ---------------------------------------------------------------------------
# Integration fixtures (session PostgresContainer + migrated schema)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def queue_poc_engine(postgres_db_url: str) -> Generator[Engine, None, None]:
    """Run POC 0001 Alembic migration and apply Procrastinate + POC idempotency schema."""
    alembic_cfg = build_alembic_config(postgres_db_url)
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0001_poc_foundation")
    engine = create_engine(postgres_db_url, pool_pre_ping=True)
    try:
        apply_queue_poc_schema(engine)
        # Verify idempotent re-application succeeds cleanly.
        apply_queue_poc_schema(engine)
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS poc_idempotency_records"))
            conn.execute(
                text("TRUNCATE TABLE uploads, tracks, edges, nodes RESTART IDENTITY CASCADE")
            )
        engine.dispose()


@pytest.fixture()
def session_factory(queue_poc_engine: Engine) -> sessionmaker[Session]:
    """Reset queue POC tables before each integration test and return a sessionmaker."""
    with queue_poc_engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    poc_idempotency_records,
                    uploads,
                    tracks,
                    edges,
                    nodes,
                    procrastinate_events,
                    procrastinate_jobs,
                    procrastinate_workers
                RESTART IDENTITY CASCADE
                """
            )
        )
        conn.execute(text("UPDATE graph_metadata SET revision = 0 WHERE id = 1"))
    return sessionmaker(
        bind=queue_poc_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


# ---------------------------------------------------------------------------
# 1. Pure unit tests (no Docker required)
# ---------------------------------------------------------------------------


def test_queue_poc_config_defaults_and_retry_backoff() -> None:
    cfg = QueuePocConfig()
    assert cfg.lease_duration_s == 120.0
    assert cfg.heartbeat_interval_s == 30.0
    assert cfg.max_attempts == 5
    assert cfg.retry_backoff_base_s == 5.0
    assert cfg.reconcile_pending_grace_s == 30.0
    assert cfg.job_retention_days == 14
    assert cfg.queue_name == DEFAULT_QUEUE_NAME

    assert cfg.retry_delay_s(0) == 5.0
    assert cfg.retry_delay_s(1) == 5.0
    assert cfg.retry_delay_s(2) == 10.0
    assert cfg.retry_delay_s(3) == 20.0
    assert cfg.retry_delay_s(4) == 40.0

    # Fractional timings supported for fast Testcontainers verification.
    fast_cfg = QueuePocConfig(
        lease_duration_s=1.5,
        heartbeat_interval_s=0.3,
        max_attempts=3,
        retry_backoff_base_s=0.0,
        reconcile_pending_grace_s=0.0,
        job_retention_days=1,
    )
    assert fast_cfg.lease_duration_s == 1.5
    assert fast_cfg.retry_delay_s(2) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lease_duration_s": 0.0},
        {"lease_duration_s": -10.0},
        {"lease_duration_s": float("nan")},
        {"lease_duration_s": float("inf")},
        {"lease_duration_s": True},
        {"heartbeat_interval_s": 0.0},
        {"heartbeat_interval_s": -1.0},
        {"heartbeat_interval_s": float("nan")},
        {"heartbeat_interval_s": float("inf")},
        {"heartbeat_interval_s": True},
        {"lease_duration_s": 30.0, "heartbeat_interval_s": 30.0},
        {"lease_duration_s": 30.0, "heartbeat_interval_s": 31.0},
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"max_attempts": True},
        {"max_attempts": False},
        {"retry_backoff_base_s": -0.1},
        {"retry_backoff_base_s": float("nan")},
        {"retry_backoff_base_s": float("inf")},
        {"retry_backoff_base_s": True},
        {"reconcile_pending_grace_s": -1.0},
        {"reconcile_pending_grace_s": float("nan")},
        {"reconcile_pending_grace_s": float("inf")},
        {"reconcile_pending_grace_s": False},
        {"job_retention_days": 0},
        {"job_retention_days": -7},
        {"job_retention_days": True},
        {"queue_name": ""},
        {"queue_name": "   "},
    ],
)
def test_queue_poc_config_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        QueuePocConfig(**kwargs)


@pytest.mark.parametrize(
    ("input_url", "expected_dsn"),
    [
        (
            "postgresql+psycopg://user:secret@localhost:5432/ontrack",
            "postgresql://user:secret@localhost:5432/ontrack",
        ),
        (
            "postgresql+psycopg2://user:secret@db.internal:5432/ontrack?sslmode=require",
            "postgresql://user:secret@db.internal:5432/ontrack?sslmode=require",
        ),
        (
            "postgres://user:secret@localhost:5432/ontrack",
            "postgresql://user:secret@localhost:5432/ontrack",
        ),
        (
            "postgresql://user:secret@localhost:5432/ontrack",
            "postgresql://user:secret@localhost:5432/ontrack",
        ),
    ],
)
def test_to_psycopg_dsn_normalizes_sqlalchemy_urls(input_url: str, expected_dsn: str) -> None:
    assert to_psycopg_dsn(input_url) == expected_dsn


def test_evaluate_queue_poc_decision_rubric() -> None:
    go_eval = evaluate_queue_poc(
        same_tx_enqueue_verified=True,
        rollback_atomicity_verified=True,
        idempotent_confirm_and_delivery_verified=True,
        sigkill_recovery_verified=True,
        healthy_long_job_preserved=True,
        universal_fencing_verified=True,
        orphan_reconciler_verified=True,
        procrastinate_stalled_jobs_verified=True,
        job_retention_verified=True,
    )
    assert go_eval.decision == "GO_PROCRASTINATE"
    assert go_eval.to_dict()["decision"] == "GO_PROCRASTINATE"

    # Any failed criterion without justification falls back to RabbitMQ + Celery + Outbox.
    fallback_rmq = evaluate_queue_poc(
        same_tx_enqueue_verified=True,
        rollback_atomicity_verified=True,
        idempotent_confirm_and_delivery_verified=True,
        sigkill_recovery_verified=False,
        healthy_long_job_preserved=True,
        universal_fencing_verified=True,
        orphan_reconciler_verified=True,
        procrastinate_stalled_jobs_verified=True,
        job_retention_verified=True,
    )
    assert fallback_rmq.decision == "FALLBACK_RABBITMQ_CELERY_OUTBOX"
    assert "sigkill_recovery" in fallback_rmq.rationale

    # Whitespace-only justification must not select SKIP LOCKED.
    blank_just = evaluate_queue_poc(
        same_tx_enqueue_verified=True,
        rollback_atomicity_verified=True,
        idempotent_confirm_and_delivery_verified=True,
        sigkill_recovery_verified=False,
        healthy_long_job_preserved=True,
        universal_fencing_verified=True,
        orphan_reconciler_verified=True,
        procrastinate_stalled_jobs_verified=True,
        job_retention_verified=True,
        custom_skip_locked_justification="   ",
    )
    assert blank_just.decision == "FALLBACK_RABBITMQ_CELERY_OUTBOX"

    # Explicit written justification selects FALLBACK_SKIP_LOCKED.
    skip_locked = evaluate_queue_poc(
        same_tx_enqueue_verified=True,
        rollback_atomicity_verified=True,
        idempotent_confirm_and_delivery_verified=True,
        sigkill_recovery_verified=False,
        healthy_long_job_preserved=True,
        universal_fencing_verified=True,
        orphan_reconciler_verified=True,
        procrastinate_stalled_jobs_verified=True,
        job_retention_verified=True,
        custom_skip_locked_justification="Broker unavailable in single-node deployment.",
    )
    assert skip_locked.decision == "FALLBACK_SKIP_LOCKED"
    assert "Broker unavailable" in skip_locked.rationale
    assert "sigkill_recovery" in skip_locked.rationale


def test_lease_heartbeat_runner_validates_timings() -> None:
    dummy_factory: Any = object()
    with pytest.raises(ValueError):
        LeaseHeartbeatRunner(
            dummy_factory,
            uuid4(),
            uuid4(),
            lease_duration_s=0.0,
            heartbeat_interval_s=0.1,
        )
    with pytest.raises(ValueError):
        LeaseHeartbeatRunner(
            dummy_factory,
            uuid4(),
            uuid4(),
            lease_duration_s=2.0,
            heartbeat_interval_s=2.0,
        )
    with pytest.raises(ValueError):
        LeaseHeartbeatRunner(
            dummy_factory,
            uuid4(),
            uuid4(),
            lease_duration_s=2.0,
            heartbeat_interval_s=-0.5,
        )


def test_credit_bearing_statuses_match_architecture_spec() -> None:
    expected = frozenset({"PENDING", "PROCESSING", "RETRY", "TECHNICAL_FAILED", "ACTIVE"})
    assert expected == CREDIT_BEARING_STATUSES
    assert "DRAFT" not in CREDIT_BEARING_STATUSES
    assert "REJECTED" not in CREDIT_BEARING_STATUSES


# ---------------------------------------------------------------------------
# 2. Testcontainers integration tests (AC-58, AC-59, AC-60, AC-63, AC-79, AC-80)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ac58_same_tx_enqueue_and_immediate_provisional_credit(
    queue_poc_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    """AC-58: Upload confirmation and Procrastinate job share one PostgreSQL transaction."""
    with session_factory() as session, session.begin():
        uid = _insert_upload_row(session)

    # Confirm inside an explicit uncommitted transaction and prove another connection sees
    # neither the PENDING transition nor the Procrastinate job until commit.
    with session_factory() as session, session.begin():
        result = confirm_upload_and_enqueue(session, uid, overall_difficulty=2)
        current_xid = str(session.execute(text("SELECT pg_current_xact_id()::text")).scalar_one())
        assert result.xact_id == current_xid
        assert result.status == "PENDING"
        assert result.overall_difficulty == 2
        assert result.procrastinate_job_id is not None
        assert result.idempotency_replayed is False

        # Independent connection must still see DRAFT and zero Procrastinate jobs pre-commit.
        with session_factory() as observer:
            obs_upload = _fetch_upload_row(observer, uid)
            assert obs_upload["status"] == "DRAFT"
            assert count_qualifying_uploads(observer) == 0
            assert query_procrastinate_jobs_for_upload(observer, uid) == []

    # After commit, both the PENDING upload (provisional credit) and Procrastinate job are visible,
    # while the shared trail network remains unchanged with worker paused.
    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "PENDING"
        assert row["overall_difficulty"] == 2
        assert row["accepted_at"] is not None
        assert count_qualifying_uploads(verifier) == 1
        assert _count_tracks(verifier) == 0
        assert _get_graph_revision(verifier) == 0

        jobs = query_procrastinate_jobs_for_upload(verifier, uid)
        assert len(jobs) == 1
        assert jobs[0]["id"] == result.procrastinate_job_id
        assert jobs[0]["status"] == "todo"
        assert jobs[0]["task_name"] == INGEST_UPLOAD_TASK_NAME
        assert jobs[0]["queue_name"] == DEFAULT_QUEUE_NAME


@pytest.mark.integration
def test_ac59_post_enqueue_failure_rolls_back_atomically(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-59: Post-enqueue error rolls back BOTH uploads status and Procrastinate job."""
    with session_factory() as session, session.begin():
        uid = _insert_upload_row(session)

    with (
        pytest.raises(InjectedPostEnqueueError),
        session_factory() as session,
        session.begin(),
    ):
        confirm_upload_and_enqueue(
            session,
            uid,
            overall_difficulty=3,
            idempotency_key="idem-ac59-rollback",
            inject_post_enqueue_error=True,
        )

    # Verify complete rollback: DRAFT preserved, zero credit, zero jobs, zero idempotency rows.
    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "DRAFT"
        assert row["overall_difficulty"] is None
        assert row["accepted_at"] is None
        assert count_qualifying_uploads(verifier) == 0
        assert query_procrastinate_jobs_for_upload(verifier, uid) == []
        idem_count = int(
            verifier.execute(text("SELECT COUNT(*) FROM poc_idempotency_records")).scalar_one()
        )
        assert idem_count == 0

    # Retry with the exact same Idempotency-Key after the fault clears succeeds cleanly.
    with session_factory() as session, session.begin():
        retry_result = confirm_upload_and_enqueue(
            session,
            uid,
            overall_difficulty=3,
            idempotency_key="idem-ac59-rollback",
            inject_post_enqueue_error=False,
        )
        assert retry_result.status == "PENDING"
        assert retry_result.idempotency_replayed is False
        assert retry_result.procrastinate_job_id is not None

    with session_factory() as verifier:
        assert count_qualifying_uploads(verifier) == 1
        assert len(query_procrastinate_jobs_for_upload(verifier, uid)) == 1


@pytest.mark.integration
def test_ac60a_idempotent_confirm_replay_and_payload_mismatch(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-60(a): Same Idempotency-Key replays cleanly; mismatched payload raises conflict."""
    with session_factory() as session, session.begin():
        uid1 = _insert_upload_row(session, filename="track1.gpx")
        uid2 = _insert_upload_row(session, filename="track2.gpx")

    # Invalid difficulty values rejected before mutation.
    for bad_diff in (0, 4, True):
        with (
            pytest.raises(InvalidUploadStateError),
            session_factory() as session,
            session.begin(),
        ):
            confirm_upload_and_enqueue(session, uid1, overall_difficulty=bad_diff)

    with session_factory() as session, session.begin():
        first = confirm_upload_and_enqueue(
            session,
            uid1,
            overall_difficulty=2,
            idempotency_key="confirm-idem-key-1",
        )
        assert first.idempotency_replayed is False
        assert first.procrastinate_job_id is not None

    # Identical replay returns original response without creating a second job.
    with session_factory() as session, session.begin():
        replayed = confirm_upload_and_enqueue(
            session,
            uid1,
            overall_difficulty=2,
            idempotency_key="confirm-idem-key-1",
        )
        assert replayed.idempotency_replayed is True
        assert replayed.upload_id == uid1
        assert replayed.overall_difficulty == 2
        assert replayed.procrastinate_job_id == first.procrastinate_job_id

    # Reusing key with mismatched difficulty is rejected.
    with (
        pytest.raises(IdempotencyConflictError),
        session_factory() as session,
        session.begin(),
    ):
        confirm_upload_and_enqueue(
            session,
            uid1,
            overall_difficulty=3,
            idempotency_key="confirm-idem-key-1",
        )

    # Reusing key with mismatched upload_id is rejected and leaves uid2 untouched.
    with (
        pytest.raises(IdempotencyConflictError),
        session_factory() as session,
        session.begin(),
    ):
        confirm_upload_and_enqueue(
            session,
            uid2,
            overall_difficulty=2,
            idempotency_key="confirm-idem-key-1",
        )

    with session_factory() as verifier:
        assert len(query_procrastinate_jobs_for_upload(verifier, uid1)) == 1
        assert _fetch_upload_row(verifier, uid2)["status"] == "DRAFT"
        assert count_qualifying_uploads(verifier) == 1


@pytest.mark.integration
def test_ac60b_idempotent_double_delivery_creates_zero_duplicates(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-60(b): Double delivery of the same upload creates 1 track and 1 graph revision bump."""
    with session_factory() as session, session.begin():
        uid1 = _insert_upload_row(session, filename="double_delivery.gpx")
        uid2 = _insert_upload_row(session, filename="concurrent_claim.gpx")
        confirm_upload_and_enqueue(session, uid1, overall_difficulty=2)
        confirm_upload_and_enqueue(session, uid2, overall_difficulty=1)

    first = process_upload_attempt(session_factory, uid1, enable_heartbeat=False)
    assert first.applied is True
    assert first.fenced_out is False
    assert first.target_status == "ACTIVE"
    assert first.graph_revision == 1

    # Simulate at-least-once redelivery after completion.
    second = process_upload_attempt(session_factory, uid1, enable_heartbeat=False)
    assert second.applied is False
    assert second.fenced_out is False
    assert second.target_status == "ACTIVE"

    with session_factory() as verifier:
        row1 = _fetch_upload_row(verifier, uid1)
        assert row1["status"] == "ACTIVE"
        assert row1["attempt_count"] == 1
        assert _count_tracks(verifier) == 1
        assert _get_graph_revision(verifier) == 1

    # Simulate duplicate delivery while uid2 is actively PROCESSING under another worker.
    with session_factory() as session, session.begin():
        claim2 = claim_upload_attempt(session, uid2, lease_duration_s=60.0)
        assert claim2.claimed is True
        assert claim2.attempt_count == 1

    dup_during_processing = process_upload_attempt(session_factory, uid2, enable_heartbeat=False)
    assert dup_during_processing.applied is False
    assert dup_during_processing.target_status == "PROCESSING"

    with session_factory() as verifier:
        assert _fetch_upload_row(verifier, uid2)["attempt_count"] == 1
        assert _count_tracks(verifier) == 1
        assert _get_graph_revision(verifier) == 1


@pytest.mark.integration
def test_ac63_and_ac79a_sigkill_worker_recovery_and_stalled_job_detection(
    postgres_db_url: str,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """AC-63 & AC-79(a): SIGKILL mid-PROCESSING rolls back cleanly, expires, and recovers."""
    with session_factory() as session, session.begin():
        uid = _insert_upload_row(session, filename="sigkill_recovery.gpx")
        confirm_res = confirm_upload_and_enqueue(session, uid, overall_difficulty=2)
        assert confirm_res.procrastinate_job_id is not None

    ready_file = tmp_path / "worker_attempt_1_ready.json"
    worker1 = _spawn_worker_subprocess(
        postgres_db_url,
        lease_duration_s=1.4,
        heartbeat_interval_s=0.35,
        hold_before_commit_s=15.0,
        ready_file=ready_file,
        one_shot=False,
    )
    try:
        marker = _wait_for_ready_file(ready_file, worker1, timeout_s=20.0)
        assert marker["upload_id"] == str(uid)
        assert marker["attempt_count"] == 1
        attempt_1_id = UUID(marker["attempt_id"])

        with session_factory() as verifier:
            row = _fetch_upload_row(verifier, uid)
            assert row["status"] == "PROCESSING"
            assert UUID(str(row["active_attempt_id"])) == attempt_1_id
            assert _count_tracks(verifier) == 0
            assert _get_graph_revision(verifier) == 0

        # Abruptly terminate worker process with real SIGKILL mid-PROCESSING.
        os.kill(worker1.pid, signal.SIGKILL)
        worker1.wait(timeout=10)
        assert worker1.returncode == -signal.SIGKILL
    finally:
        if worker1.poll() is None:
            worker1.kill()
            worker1.wait(timeout=5)

    # Confirm zero partial graph writes persisted after SIGKILL.
    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "PROCESSING"
        assert _count_tracks(verifier) == 0
        assert _get_graph_revision(verifier) == 0

    # Wait past lease_duration_s so both DB lease and Procrastinate worker heartbeat expire.
    time.sleep(1.7)

    # Procrastinate's built-in heartbeat-based stalled-job detection must spot the killed job.
    stalled_job_ids = inspect_procrastinate_stalled_jobs(
        postgres_db_url,
        seconds_since_heartbeat=1.0,
        queue=DEFAULT_QUEUE_NAME,
    )
    assert confirm_res.procrastinate_job_id in stalled_job_ids

    # Reconciler reclaims expired PROCESSING -> RETRY and enqueues a recovery job atomically.
    with session_factory() as session, session.begin():
        rec = reconcile_stalled_and_orphaned(session, pending_grace_s=0.0, max_attempts=3)
        assert rec.reclaimed_to_retry == (uid,)
        assert rec.exhausted_to_technical_failed == ()
        assert len(rec.reenqueued_job_ids) == 1

    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "RETRY"
        assert row["active_attempt_id"] is None
        assert row["error_category"] == "WORKER_LEASE_EXPIRED"
        assert count_qualifying_uploads(verifier) == 1

    # Also verify Procrastinate's retry_stalled_procrastinate_jobs API resets the dead doing job
    # cleanly without causing duplicate ingestion once the second worker processes the queue.
    retried_ids = retry_stalled_procrastinate_jobs(
        postgres_db_url,
        seconds_since_heartbeat=1.0,
        queue=DEFAULT_QUEUE_NAME,
    )
    assert confirm_res.procrastinate_job_id in retried_ids

    # Spawn a healthy recovery worker subprocess (--one-shot) to complete the recovered upload.
    worker2 = _spawn_worker_subprocess(
        postgres_db_url,
        lease_duration_s=30.0,
        heartbeat_interval_s=5.0,
        hold_before_commit_s=0.0,
        one_shot=True,
    )
    stdout2, stderr2 = worker2.communicate(timeout=20)
    assert worker2.returncode == 0, (
        f"Recovery worker failed:\nSTDOUT:\n{stdout2}\nSTDERR:\n{stderr2}"
    )

    # Upload completed ACTIVE on attempt 2 with zero duplicate tracks and single revision bump.
    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "ACTIVE"
        assert row["attempt_count"] == 2
        assert row["active_attempt_id"] is None
        assert row["track_id"] == row["normalized_fingerprint"]
        assert row["processed_at"] is not None
        assert _count_tracks(verifier) == 1
        assert _get_graph_revision(verifier) == 1
        assert count_qualifying_uploads(verifier) == 1


@pytest.mark.integration
def test_ac63_technical_retry_exhaustion_moves_to_technical_failed_with_credit(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-63: Technical retry exhaustion transitions to TECHNICAL_FAILED while retaining credit."""
    with session_factory() as session, session.begin():
        uid_reconciler = _insert_upload_row(session, filename="exhaust_reconciler.gpx")
        uid_handler = _insert_upload_row(session, filename="exhaust_handler.gpx")
        confirm_upload_and_enqueue(session, uid_reconciler, overall_difficulty=2)
        confirm_upload_and_enqueue(session, uid_handler, overall_difficulty=1)

    # Path A: Reconciler exhaustion after max_attempts=2 expired leases.
    for expected_attempt in (1, 2):
        with session_factory() as session, session.begin():
            claim = claim_upload_attempt(session, uid_reconciler, lease_duration_s=10.0)
            assert claim.claimed is True
            assert claim.attempt_count == expected_attempt
            session.execute(
                text(
                    "UPDATE uploads SET lease_expires_at = now() - interval '5 seconds' "
                    "WHERE id = :uid"
                ),
                {"uid": uid_reconciler},
            )
        with session_factory() as session, session.begin():
            rec = reconcile_stalled_and_orphaned(session, pending_grace_s=60.0, max_attempts=2)
            if expected_attempt == 1:
                assert rec.reclaimed_to_retry == (uid_reconciler,)
                assert rec.exhausted_to_technical_failed == ()
            else:
                assert rec.reclaimed_to_retry == ()
                assert rec.exhausted_to_technical_failed == (uid_reconciler,)

    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid_reconciler)
        assert row["status"] == "TECHNICAL_FAILED"
        assert row["attempt_count"] == 2
        assert row["active_attempt_id"] is None
        assert row["processed_at"] is not None
        assert row["safe_error_message"] is not None
        assert "credit" in str(row["safe_error_message"]).lower()

    # Path B: Worker handler transient technical error hitting max_attempts=2.
    cfg = QueuePocConfig(max_attempts=2)
    r1 = process_upload_attempt(
        session_factory,
        uid_handler,
        config=cfg,
        enable_heartbeat=False,
        simulate_technical_error="TRANSIENT_STORAGE_ERROR",
    )
    assert r1.applied is True
    assert r1.target_status == "RETRY"

    r2 = process_upload_attempt(
        session_factory,
        uid_handler,
        config=cfg,
        enable_heartbeat=False,
        simulate_technical_error="TRANSIENT_STORAGE_ERROR",
    )
    assert r2.applied is True
    assert r2.target_status == "TECHNICAL_FAILED"

    with session_factory() as verifier:
        assert _fetch_upload_row(verifier, uid_handler)["status"] == "TECHNICAL_FAILED"
        # Both TECHNICAL_FAILED uploads retain provisional route-creation credit.
        assert count_qualifying_uploads(verifier) == 2


@pytest.mark.integration
def test_ac79b_healthy_long_running_job_is_not_reclaimed(
    postgres_db_url: str,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """AC-79(b): Healthy job running longer than lease_duration_s with heartbeats is preserved."""
    with session_factory() as session, session.begin():
        uid = _insert_upload_row(session, filename="long_running_healthy.gpx")
        confirm_upload_and_enqueue(session, uid, overall_difficulty=3)

    ready_file = tmp_path / "long_job_ready.json"
    lease_s = 1.6
    heartbeat_s = 0.35
    hold_s = 3.2

    worker = _spawn_worker_subprocess(
        postgres_db_url,
        lease_duration_s=lease_s,
        heartbeat_interval_s=heartbeat_s,
        hold_before_commit_s=hold_s,
        ready_file=ready_file,
        one_shot=True,
    )
    try:
        marker = _wait_for_ready_file(ready_file, worker, timeout_s=20.0)
        assert marker["upload_id"] == str(uid)

        # Wait past lease_duration_s while the job is still running and renewing heartbeats.
        time.sleep(2.1)

        with session_factory() as session, session.begin():
            row = _fetch_upload_row(session, uid)
            assert row["status"] == "PROCESSING"
            assert row["last_heartbeat_at"] > row["processing_started_at"]
            db_now: Any = session.execute(text("SELECT now()")).scalar_one()
            assert row["lease_expires_at"] > db_now
            assert (db_now - row["processing_started_at"]).total_seconds() > lease_s

            # Reconciler must NOT reclaim based on processing_started_at.
            rec = reconcile_stalled_and_orphaned(session, pending_grace_s=0.0, max_attempts=5)
            assert rec.reclaimed_to_retry == ()
            assert rec.exhausted_to_technical_failed == ()
            assert rec.skipped_healthy_processing == 1

        # Procrastinate's built-in heartbeat stalled-job check must also see zero stalled jobs.
        stalled = inspect_procrastinate_stalled_jobs(
            postgres_db_url,
            seconds_since_heartbeat=lease_s,
            queue=DEFAULT_QUEUE_NAME,
        )
        assert stalled == []

        stdout, stderr = worker.communicate(timeout=20)
        assert worker.returncode == 0, (
            f"Long-running worker failed:\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)

    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "ACTIVE"
        assert row["attempt_count"] == 1
        assert _count_tracks(verifier) == 1
        assert _get_graph_revision(verifier) == 1


@pytest.mark.integration
def test_ac80_universal_stale_worker_fencing_across_all_transitions(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-80: Stale active_attempt_id is fenced out of ACTIVE, REJECTED, RETRY, TECHNICAL_FAILED."""
    with session_factory() as session, session.begin():
        uid = _insert_upload_row(session, filename="fenced_upload.gpx")
        confirm_upload_and_enqueue(session, uid, overall_difficulty=2)

    # Worker A claims attempt 1, then pauses past lease expiry.
    with session_factory() as session, session.begin():
        claim_a = claim_upload_attempt(session, uid, lease_duration_s=5.0)
        assert claim_a.claimed is True
        assert claim_a.attempt_id is not None
        attempt_a = claim_a.attempt_id
        session.execute(
            text(
                "UPDATE uploads SET lease_expires_at = now() - interval '10 seconds' "
                "WHERE id = :uid"
            ),
            {"uid": uid},
        )

    # Reconciler moves upload to RETRY and Worker B takes ownership with attempt_b.
    with session_factory() as session, session.begin():
        rec = reconcile_stalled_and_orphaned(session, pending_grace_s=60.0, max_attempts=5)
        assert rec.reclaimed_to_retry == (uid,)

    with session_factory() as session, session.begin():
        claim_b = claim_upload_attempt(session, uid, lease_duration_s=60.0)
        assert claim_b.claimed is True
        assert claim_b.attempt_id is not None
        attempt_b = claim_b.attempt_id
        assert attempt_b != attempt_a
        assert claim_b.attempt_count == 2
        jobs_before = len(query_procrastinate_jobs_for_upload(session, uid))

    # Stale Worker A wakes up and attempts lease renewal + ALL FOUR state transitions.
    with session_factory() as session, session.begin():
        assert renew_attempt_lease(session, uid, attempt_a, lease_duration_s=60.0) is False

        res_active = complete_upload_active(session, uid, attempt_a)
        assert res_active.applied is False
        assert res_active.fenced_out is True
        assert res_active.target_status == "ACTIVE"

        res_rejected = reject_upload_business(session, uid, attempt_a)
        assert res_rejected.applied is False
        assert res_rejected.fenced_out is True
        assert res_rejected.target_status == "REJECTED"

        res_retry = mark_upload_retry(session, uid, attempt_a, enqueue_retry_job=True)
        assert res_retry.applied is False
        assert res_retry.fenced_out is True
        assert res_retry.target_status == "RETRY"

        res_failed = mark_upload_technical_failed(session, uid, attempt_a)
        assert res_failed.applied is False
        assert res_failed.fenced_out is True
        assert res_failed.target_status == "TECHNICAL_FAILED"

    # Worker B's ownership, tracks table, graph revision, and job count must be completely intact.
    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "PROCESSING"
        assert UUID(str(row["active_attempt_id"])) == attempt_b
        assert row["attempt_count"] == 2
        assert _count_tracks(verifier) == 0
        assert _get_graph_revision(verifier) == 0
        assert len(query_procrastinate_jobs_for_upload(verifier, uid)) == jobs_before

    # Worker B completes cleanly.
    with session_factory() as session, session.begin():
        res_b = complete_upload_active(session, uid, attempt_b)
        assert res_b.applied is True
        assert res_b.fenced_out is False
        assert res_b.graph_revision == 1

    # Even after completion, stale attempts trying any transition remain fenced out.
    with session_factory() as session, session.begin():
        assert complete_upload_active(session, uid, attempt_a).fenced_out is True
        assert reject_upload_business(session, uid, attempt_a).fenced_out is True
        assert mark_upload_retry(session, uid, attempt_a).fenced_out is True
        assert mark_upload_technical_failed(session, uid, attempt_a).fenced_out is True

    with session_factory() as verifier:
        row = _fetch_upload_row(verifier, uid)
        assert row["status"] == "ACTIVE"
        assert _count_tracks(verifier) == 1
        assert _get_graph_revision(verifier) == 1


@pytest.mark.integration
def test_orphan_reconciler_job_retention_and_go_evaluation(
    postgres_db_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    """Verify orphaned PENDING/RETRY recovery, Procrastinate retention purge, and GO verdict."""
    # 1. Seed aged orphaned PENDING & RETRY rows plus a fresh PENDING row within grace period.
    with session_factory() as session, session.begin():
        orphan_pending = _insert_upload_row(
            session,
            filename="orphan_pending.gpx",
            status="PENDING",
            overall_difficulty=2,
            accepted_at_ago_s=120.0,
        )
        orphan_retry = _insert_upload_row(
            session,
            filename="orphan_retry.gpx",
            status="RETRY",
            overall_difficulty=3,
            accepted_at_ago_s=180.0,
            last_heartbeat_ago_s=120.0,
        )
        fresh_pending = _insert_upload_row(
            session,
            filename="fresh_pending.gpx",
            status="PENDING",
            overall_difficulty=1,
            accepted_at_ago_s=1.0,
        )

    with session_factory() as session, session.begin():
        rec1 = reconcile_stalled_and_orphaned(session, pending_grace_s=30.0, max_attempts=5)
        assert set(rec1.reenqueued_orphans) == {orphan_pending, orphan_retry}
        assert len(rec1.reenqueued_job_ids) == 2
        assert fresh_pending not in rec1.reenqueued_orphans

    # Re-running immediately must be idempotent (already has todo jobs).
    with session_factory() as session, session.begin():
        rec2 = reconcile_stalled_and_orphaned(session, pending_grace_s=30.0, max_attempts=5)
        assert rec2.reenqueued_orphans == ()
        assert rec2.reenqueued_job_ids == ()

    # 2. Run a one-shot Procrastinate worker so both recovered orphan jobs complete ('succeeded').
    worker = _spawn_worker_subprocess(
        postgres_db_url,
        lease_duration_s=30.0,
        heartbeat_interval_s=5.0,
        hold_before_commit_s=0.0,
        one_shot=True,
    )
    stdout, stderr = worker.communicate(timeout=20)
    assert worker.returncode == 0, f"Worker failed:\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"

    with session_factory() as verifier:
        jobs_p = query_procrastinate_jobs_for_upload(verifier, orphan_pending)
        jobs_r = query_procrastinate_jobs_for_upload(verifier, orphan_retry)
        assert len(jobs_p) == 1 and jobs_p[0]["status"] == "succeeded"
        assert len(jobs_r) == 1 and jobs_r[0]["status"] == "succeeded"
        old_job_id = int(jobs_p[0]["id"])
        recent_job_id = int(jobs_r[0]["id"])

    # 3. Backdate only the first job's Procrastinate events past 14-day retention and purge.
    with session_factory() as session, session.begin():
        session.execute(
            text(
                "UPDATE procrastinate_events "
                "SET at = now() - interval '20 days' "
                "WHERE job_id = :job_id"
            ),
            {"job_id": old_job_id},
        )

    deleted_count = purge_old_procrastinate_jobs(
        postgres_db_url,
        retention_days=14,
        queue=DEFAULT_QUEUE_NAME,
        include_failed=False,
    )
    assert deleted_count == 1

    with session_factory() as verifier:
        remaining_ids = [
            int(row[0])
            for row in verifier.execute(
                text("SELECT id FROM procrastinate_jobs ORDER BY id")
            ).all()
        ]
        assert old_job_id not in remaining_ids
        assert recent_job_id in remaining_ids

    # 4. Measure same-tx confirm+enqueue latency and emit structured GO_PROCRASTINATE verdict.
    latencies_ms: list[float] = []
    for idx in range(5):
        with session_factory() as session, session.begin():
            uid = _insert_upload_row(session, filename=f"bench_{idx}.gpx")
        t0 = time.perf_counter()
        with session_factory() as session, session.begin():
            confirm_upload_and_enqueue(session, uid, overall_difficulty=2)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    latencies_sorted = sorted(latencies_ms)
    p50_ms = latencies_sorted[len(latencies_sorted) // 2]
    max_ms = latencies_sorted[-1]

    evaluation = evaluate_queue_poc(
        same_tx_enqueue_verified=True,
        rollback_atomicity_verified=True,
        idempotent_confirm_and_delivery_verified=True,
        sigkill_recovery_verified=True,
        healthy_long_job_preserved=True,
        universal_fencing_verified=True,
        orphan_reconciler_verified=True,
        procrastinate_stalled_jobs_verified=True,
        job_retention_verified=True,
    )
    assert evaluation.decision == "GO_PROCRASTINATE"
    print(
        json.dumps(
            {
                "t005_verdict": evaluation.to_dict(),
                "confirm_enqueue_latency_ms": {
                    "samples": [round(val, 2) for val in latencies_ms],
                    "p50_ms": round(p50_ms, 2),
                    "max_ms": round(max_ms, 2),
                },
            }
        )
    )
