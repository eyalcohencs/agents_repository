"""Initial POC foundation schema with PostGIS and pgRouting extensions.

Revision ID: 0001_poc_foundation
Revises:
Create Date: 2026-11-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_poc_foundation"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgrouting;")

    op.execute(
        """
        CREATE TABLE graph_metadata (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            revision BIGINT NOT NULL DEFAULT 0
        );
        """
    )
    op.execute(
        """
        INSERT INTO graph_metadata (id, revision)
        VALUES (1, 0)
        ON CONFLICT (id) DO NOTHING;
        """
    )

    op.execute(
        """
        CREATE TABLE nodes (
            id BIGSERIAL PRIMARY KEY,
            geom geometry(Point, 4326) NOT NULL,
            ele REAL NULL
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_nodes_geom_geog ON nodes USING GIST ((geom::geography));"
    )
    op.execute("CREATE INDEX ix_nodes_geom ON nodes USING GIST (geom);")

    op.execute(
        """
        CREATE TABLE edges (
            id BIGSERIAL PRIMARY KEY,
            source BIGINT NOT NULL REFERENCES nodes(id),
            target BIGINT NOT NULL REFERENCES nodes(id),
            geom geometry(LineString, 4326) NOT NULL,
            length_m REAL NOT NULL CHECK (length_m > 0),
            initial_difficulty SMALLINT NOT NULL CHECK (initial_difficulty BETWEEN 1 AND 3),
            CONSTRAINT ck_edges_no_self_loop CHECK (source <> target)
        );
        """
    )
    op.execute("CREATE INDEX ix_edges_geom ON edges USING GIST (geom);")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_edges_unordered_endpoints
        ON edges (LEAST(source, target), GREATEST(source, target));
        """
    )
    op.execute("CREATE INDEX ix_edges_source ON edges (source);")
    op.execute("CREATE INDEX ix_edges_target ON edges (target);")

    op.execute(
        """
        CREATE TABLE tracks (
            id TEXT PRIMARY KEY,
            normalized_geom geometry(MultiLineString, 4326) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE uploads (
            id UUID PRIMARY KEY,
            filename TEXT NOT NULL,
            original_ref TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            normalized_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'DRAFT',
                    'PENDING',
                    'PROCESSING',
                    'RETRY',
                    'TECHNICAL_FAILED',
                    'ACTIVE',
                    'REJECTED'
                )
            ),
            overall_difficulty SMALLINT NULL CHECK (overall_difficulty BETWEEN 1 AND 3),
            active_attempt_id UUID NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            processing_started_at TIMESTAMPTZ NULL,
            last_heartbeat_at TIMESTAMPTZ NULL,
            lease_expires_at TIMESTAMPTZ NULL,
            error_category TEXT NULL,
            safe_error_message TEXT NULL,
            track_id TEXT NULL REFERENCES tracks(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            accepted_at TIMESTAMPTZ NULL,
            processed_at TIMESTAMPTZ NULL
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_uploads_status_lease ON uploads (status, lease_expires_at);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS uploads;")
    op.execute("DROP TABLE IF EXISTS tracks;")
    op.execute("DROP TABLE IF EXISTS edges;")
    op.execute("DROP TABLE IF EXISTS nodes;")
    op.execute("DROP TABLE IF EXISTS graph_metadata;")
    op.execute("DROP EXTENSION IF EXISTS pgrouting;")
    op.execute("DROP EXTENSION IF EXISTS postgis;")
