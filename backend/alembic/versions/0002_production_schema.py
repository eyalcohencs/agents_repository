"""Final production database schema and Procrastinate 3 job queue tables.

Revision ID: 0002_production_schema
Revises: 0001_poc_foundation
Create Date: 2026-11-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from procrastinate.schema import SchemaManager

revision: str = "0002_production_schema"
down_revision: str | Sequence[str] | None = "0001_poc_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROCRASTINATE_TABLES: tuple[str, ...] = (
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_jobs",
    "procrastinate_workers",
)

_PROCRASTINATE_FUNCTIONS: tuple[str, ...] = (
    "procrastinate_defer_jobs_v2",
    "procrastinate_defer_periodic_job_v3",
    "procrastinate_fetch_job_v2",
    "procrastinate_finish_job_v1",
    "procrastinate_cancel_job_v1",
    "procrastinate_retry_job_v1",
    "procrastinate_retry_job_v2",
    "procrastinate_notify_queue_job_inserted_v1",
    "procrastinate_notify_queue_abort_job_v1",
    "procrastinate_trigger_function_status_events_insert_v1",
    "procrastinate_trigger_function_status_events_update_v1",
    "procrastinate_trigger_function_scheduled_events_v1",
    "procrastinate_trigger_abort_requested_events_procedure_v1",
    "procrastinate_unlink_periodic_defers_v1",
    "procrastinate_register_worker_v1",
    "procrastinate_unregister_worker_v1",
    "procrastinate_update_heartbeat_v1",
    "procrastinate_prune_stalled_workers_v1",
)

_PROCRASTINATE_TYPES: tuple[str, ...] = (
    "procrastinate_job_to_defer_v2",
    "procrastinate_lock_mode",
    "procrastinate_job_event_type",
    "procrastinate_job_status",
)


def upgrade() -> None:
    # 1. users
    op.execute(
        """
        CREATE TABLE users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            user_type TEXT NOT NULL DEFAULT 'regular'
                CONSTRAINT ck_users_user_type_valid
                    CHECK (user_type IN ('regular', 'system', 'inactive')),
            preferred_language TEXT NOT NULL DEFAULT 'he'
                CONSTRAINT ck_users_preferred_language_valid
                    CHECK (preferred_language IN ('he', 'en')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (LOWER(email));")

    # 2. sessions
    op.execute(
        """
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            session_id UUID NOT NULL UNIQUE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ NULL,
            grace_until TIMESTAMPTZ NULL,
            CONSTRAINT ck_sessions_expires_after_created CHECK (expires_at > created_at)
        );
        """
    )
    op.execute("CREATE INDEX ix_sessions_user_expires ON sessions (user_id, expires_at);")
    op.execute("CREATE INDEX ix_sessions_expires_at ON sessions (expires_at);")

    # 3. app_config + initial FR-022 seed rows (Gate 1 confirmed + T-007 synthetic lock)
    op.execute(
        """
        CREATE TABLE app_config (
            key TEXT PRIMARY KEY,
            value JSONB NOT NULL,
            updated_by BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        INSERT INTO app_config (key, value)
        VALUES
            ('MIN_REQUIRED_TRACKS', '1'::jsonb),
            ('FREE_ROUTE_CREATIONS', '0'::jsonb),
            ('REJECTED_TRACK_GRACE_PERIOD_HOURS', '24'::jsonb),
            ('POINT_SIMPLIFICATION_DISTANCE_M', '20.0'::jsonb),
            ('NODE_SNAP_RADIUS_M', '20.0'::jsonb)
        ON CONFLICT (key) DO NOTHING;
        """
    )

    # 4. ALTER uploads with production columns, constraints, and indexes
    op.execute(
        """
        ALTER TABLE uploads
            ADD COLUMN user_id BIGINT NOT NULL REFERENCES users(id),
            ADD COLUMN ingestion_config_snapshot JSONB NULL,
            ADD COLUMN rejection_code TEXT NULL,
            ADD COLUMN draft_expires_at TIMESTAMPTZ NULL,
            ADD CONSTRAINT ck_uploads_attempt_count_non_negative
                CHECK (attempt_count >= 0),
            ADD CONSTRAINT ck_uploads_rejection_code_valid
                CHECK (
                    rejection_code IS NULL
                    OR rejection_code IN (
                        'DEGENERATE_GEOMETRY',
                        'OUTSIDE_SERVICE_AREA',
                        'QUALITY_RULE_FAILED'
                    )
                );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_uploads_user_credit_fingerprint
        ON uploads (user_id, normalized_fingerprint)
        WHERE status IN ('PENDING', 'PROCESSING', 'RETRY', 'TECHNICAL_FAILED', 'ACTIVE');
        """
    )
    op.execute("CREATE INDEX ix_uploads_user_created_at ON uploads (user_id, created_at DESC);")
    op.execute("CREATE INDEX ix_uploads_user_status ON uploads (user_id, status);")
    op.execute(
        """
        CREATE INDEX ix_uploads_draft_expires
        ON uploads (draft_expires_at)
        WHERE status = 'DRAFT';
        """
    )

    # 5. upload_segments
    op.execute(
        """
        CREATE TABLE upload_segments (
            id BIGSERIAL PRIMARY KEY,
            upload_id UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
            segment_index INTEGER NOT NULL
                CONSTRAINT ck_upload_segments_segment_index_non_negative
                    CHECK (segment_index >= 0),
            geom geometry(LineString, 4326) NOT NULL,
            CONSTRAINT uq_upload_segments_upload_index UNIQUE (upload_id, segment_index)
        );
        """
    )

    # 6. batch_imports
    op.execute(
        """
        CREATE TABLE batch_imports (
            id UUID PRIMARY KEY,
            initiated_by BIGINT NOT NULL REFERENCES users(id),
            source TEXT NOT NULL
                CONSTRAINT ck_batch_imports_source_valid
                    CHECK (source IN ('cloud', 'server')),
            status TEXT NOT NULL
                CONSTRAINT ck_batch_imports_status_valid
                    CHECK (
                        status IN (
                            'QUEUED',
                            'RUNNING',
                            'COMPLETED',
                            'COMPLETED_WITH_ERRORS',
                            'FAILED'
                        )
                    ),
            total_files INTEGER NOT NULL DEFAULT 0
                CONSTRAINT ck_batch_imports_total_files_non_negative
                    CHECK (total_files >= 0),
            imported_count INTEGER NOT NULL DEFAULT 0
                CONSTRAINT ck_batch_imports_imported_count_non_negative
                    CHECK (imported_count >= 0),
            skipped_count INTEGER NOT NULL DEFAULT 0
                CONSTRAINT ck_batch_imports_skipped_count_non_negative
                    CHECK (skipped_count >= 0),
            failed_count INTEGER NOT NULL DEFAULT 0
                CONSTRAINT ck_batch_imports_failed_count_non_negative
                    CHECK (failed_count >= 0),
            email_sent_at TIMESTAMPTZ NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ NULL
        );
        """
    )
    op.execute("CREATE INDEX ix_batch_imports_status ON batch_imports (status);")

    # 7. batch_files
    op.execute(
        """
        CREATE TABLE batch_files (
            id BIGSERIAL PRIMARY KEY,
            batch_id UUID NOT NULL REFERENCES batch_imports(id) ON DELETE CASCADE,
            source_ref TEXT NOT NULL,
            file_sha256 TEXT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CONSTRAINT ck_batch_files_status_valid
                    CHECK (
                        status IN (
                            'PENDING',
                            'PROCESSING',
                            'RETRY',
                            'COMPLETED',
                            'SKIPPED',
                            'FAILED'
                        )
                    ),
            attempt_count INTEGER NOT NULL DEFAULT 0
                CONSTRAINT ck_batch_files_attempt_count_non_negative
                    CHECK (attempt_count >= 0),
            active_attempt_id UUID NULL,
            processing_started_at TIMESTAMPTZ NULL,
            last_heartbeat_at TIMESTAMPTZ NULL,
            lease_expires_at TIMESTAMPTZ NULL,
            ingestion_config_snapshot JSONB NULL,
            error_category TEXT NULL,
            safe_reason TEXT NULL,
            track_id TEXT NULL REFERENCES tracks(id),
            started_at TIMESTAMPTZ NULL,
            finished_at TIMESTAMPTZ NULL,
            CONSTRAINT uq_batch_files_batch_source_ref UNIQUE (batch_id, source_ref)
        );
        """
    )
    op.execute("CREATE INDEX ix_batch_files_batch_status ON batch_files (batch_id, status);")
    op.execute(
        "CREATE INDEX ix_batch_files_status_lease ON batch_files (status, lease_expires_at);"
    )

    # 8. ingestion_runs
    op.execute(
        """
        CREATE TABLE ingestion_runs (
            id UUID PRIMARY KEY,
            source_type TEXT NOT NULL
                CONSTRAINT ck_ingestion_runs_source_type_valid
                    CHECK (source_type IN ('upload', 'batch_file')),
            upload_id UUID NULL REFERENCES uploads(id) ON DELETE CASCADE,
            batch_file_id BIGINT NULL REFERENCES batch_files(id) ON DELETE CASCADE,
            track_id TEXT NOT NULL REFERENCES tracks(id),
            config_snapshot JSONB NOT NULL,
            graph_revision_before BIGINT NOT NULL
                CONSTRAINT ck_ingestion_runs_graph_revision_before_non_negative
                    CHECK (graph_revision_before >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ NULL,
            CONSTRAINT ck_ingestion_runs_source_ref CHECK (
                (source_type = 'upload' AND upload_id IS NOT NULL AND batch_file_id IS NULL)
                OR (
                    source_type = 'batch_file'
                    AND batch_file_id IS NOT NULL
                    AND upload_id IS NULL
                )
            )
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ingestion_runs_upload_id
        ON ingestion_runs (upload_id)
        WHERE upload_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ingestion_runs_batch_file_id
        ON ingestion_runs (batch_file_id)
        WHERE batch_file_id IS NOT NULL;
        """
    )
    op.execute("CREATE INDEX ix_ingestion_runs_track_id ON ingestion_runs (track_id);")

    # 9. edge_contributions
    op.execute(
        """
        CREATE TABLE edge_contributions (
            edge_id BIGINT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
            ingestion_run_id UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
            track_id TEXT NOT NULL REFERENCES tracks(id),
            PRIMARY KEY (edge_id, ingestion_run_id)
        );
        """
    )
    op.execute("CREATE INDEX ix_edge_contributions_track_id ON edge_contributions (track_id);")
    op.execute(
        """
        CREATE INDEX ix_edge_contributions_ingestion_run_id
        ON edge_contributions (ingestion_run_id);
        """
    )

    # 10. edge_ratings
    op.execute(
        """
        CREATE TABLE edge_ratings (
            edge_id BIGINT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            difficulty SMALLINT NOT NULL
                CONSTRAINT ck_edge_ratings_difficulty_range
                    CHECK (difficulty BETWEEN 1 AND 3),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (edge_id, user_id)
        );
        """
    )

    # 11. edge_difficulty view
    op.execute(
        """
        CREATE VIEW edge_difficulty AS
        SELECT
            e.id AS edge_id,
            COALESCE(
                AVG(r.difficulty)::double precision,
                e.initial_difficulty::double precision
            ) AS avg_difficulty,
            COUNT(r.edge_id)::bigint AS report_count,
            (
                CASE
                    WHEN COALESCE(
                        AVG(r.difficulty)::double precision,
                        e.initial_difficulty::double precision
                    ) < 1.5 THEN 1
                    WHEN COALESCE(
                        AVG(r.difficulty)::double precision,
                        e.initial_difficulty::double precision
                    ) < 2.5 THEN 2
                    ELSE 3
                END
            )::smallint AS display_level
        FROM edges e
        LEFT JOIN edge_ratings r ON r.edge_id = e.id
        GROUP BY e.id, e.initial_difficulty;
        """
    )

    # 12. route_usage
    op.execute(
        """
        CREATE TABLE route_usage (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_id UUID NOT NULL,
            coord_fingerprint TEXT NOT NULL,
            attempt_id UUID NOT NULL,
            state TEXT NOT NULL
                CONSTRAINT ck_route_usage_state_valid
                    CHECK (
                        state IN (
                            'RESERVED',
                            'CONSUMED',
                            'RELEASED',
                            'EXPIRED',
                            'NON_ALLOWANCE'
                        )
                    ),
            expires_at TIMESTAMPTZ NULL,
            response_json JSONB NULL,
            replay_expires_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settled_at TIMESTAMPTZ NULL,
            CONSTRAINT uq_route_usage_user_request UNIQUE (user_id, request_id)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_route_usage_user_state_expires
        ON route_usage (user_id, state, expires_at);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_route_usage_replay_expires_at
        ON route_usage (replay_expires_at)
        WHERE replay_expires_at IS NOT NULL;
        """
    )

    # 13. idempotency_records
    op.execute(
        """
        CREATE TABLE idempotency_records (
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            key TEXT NOT NULL
                CONSTRAINT ck_idempotency_records_key_length
                    CHECK (char_length(key) BETWEEN 1 AND 255),
            payload_fingerprint TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            response_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (user_id, operation, key)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_idempotency_records_expires_at ON idempotency_records (expires_at);"
    )

    # 14. Procrastinate 3 schema (exec_driver_sql avoids SQLAlchemy parsing PL/pgSQL := / ::)
    if context.is_offline_mode():
        op.execute(SchemaManager.get_schema())
    else:
        bind = op.get_bind()
        existing: str | None = bind.execute(
            sa.text("SELECT to_regclass('public.procrastinate_jobs')")
        ).scalar_one()
        if existing is None:
            bind.exec_driver_sql(SchemaManager.get_schema())


def downgrade() -> None:
    # Drop Procrastinate tables (CASCADE removes triggers and indexes), functions, and types
    for table_name in _PROCRASTINATE_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE;")
    for function_name in _PROCRASTINATE_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function_name} CASCADE;")
    for type_name in _PROCRASTINATE_TYPES:
        op.execute(f"DROP TYPE IF EXISTS {type_name};")

    # Drop view and domain tables added in 0002 in reverse dependency order
    op.execute("DROP VIEW IF EXISTS edge_difficulty;")
    op.execute("DROP TABLE IF EXISTS idempotency_records;")
    op.execute("DROP TABLE IF EXISTS route_usage;")
    op.execute("DROP TABLE IF EXISTS edge_ratings;")
    op.execute("DROP TABLE IF EXISTS edge_contributions;")
    op.execute("DROP TABLE IF EXISTS ingestion_runs;")
    op.execute("DROP TABLE IF EXISTS batch_files;")
    op.execute("DROP TABLE IF EXISTS batch_imports;")
    op.execute("DROP TABLE IF EXISTS upload_segments;")

    # Restore uploads table to exact 0001_poc_foundation shape
    op.execute("DROP INDEX IF EXISTS ix_uploads_draft_expires;")
    op.execute("DROP INDEX IF EXISTS ix_uploads_user_status;")
    op.execute("DROP INDEX IF EXISTS ix_uploads_user_created_at;")
    op.execute("DROP INDEX IF EXISTS uq_uploads_user_credit_fingerprint;")
    op.execute(
        """
        ALTER TABLE uploads
            DROP CONSTRAINT IF EXISTS ck_uploads_rejection_code_valid,
            DROP CONSTRAINT IF EXISTS ck_uploads_attempt_count_non_negative,
            DROP COLUMN IF EXISTS draft_expires_at,
            DROP COLUMN IF EXISTS rejection_code,
            DROP COLUMN IF EXISTS ingestion_config_snapshot,
            DROP COLUMN IF EXISTS user_id;
        """
    )

    op.execute("DROP TABLE IF EXISTS app_config;")
    op.execute("DROP TABLE IF EXISTS sessions;")
    op.execute("DROP TABLE IF EXISTS users;")
