from __future__ import annotations

import uuid

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.main import create_app
from tests.conftest import build_alembic_config

pytestmark = pytest.mark.integration

FOUNDATION_TABLES = {"graph_metadata", "nodes", "edges", "tracks", "uploads"}

_EXTENSIONS_SQL = text(
    "SELECT extname FROM pg_extension WHERE extname IN ('postgis', 'pgrouting')"
)
_TABLES_SQL = text(
    """
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN ('graph_metadata', 'nodes', 'edges', 'tracks', 'uploads')
    """
)
_EDGE_GEOM_SQL = (
    "ST_SetSRID(ST_MakeLine(ST_MakePoint(35.0, 32.6), ST_MakePoint(35.001, 32.601)), 4326)"
)


def _installed_extensions(conn: Connection) -> set[str]:
    return {row[0] for row in conn.execute(_EXTENSIONS_SQL)}


def _existing_foundation_tables(conn: Connection) -> set[str]:
    return {row[0] for row in conn.execute(_TABLES_SQL)}


def test_alembic_0001_upgrade_schema_constraints_and_indexes(postgres_db_url: str) -> None:
    alembic_cfg = build_alembic_config(postgres_db_url)
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0001_poc_foundation")

    engine = create_engine(postgres_db_url)
    try:
        with engine.connect() as conn:
            # 1. Verify PostGIS and pgRouting extensions are installed & callable
            assert _installed_extensions(conn) == {"postgis", "pgrouting"}
            assert conn.execute(text("SELECT PostGIS_Version()")).scalar_one()
            assert conn.execute(text("SELECT pgr_version()")).scalar_one()

            # 2. Verify all 5 foundation tables exist
            assert _existing_foundation_tables(conn) == FOUNDATION_TABLES

            # 3. Verify graph_metadata seeded with (1, 0) and singleton CHECK (id = 1)
            meta_rows = conn.execute(
                text("SELECT id, revision FROM graph_metadata ORDER BY id")
            ).fetchall()
            assert meta_rows == [(1, 0)]

            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text("INSERT INTO graph_metadata (id, revision) VALUES (2, 1)")
                )

            # 4. Verify indexes on nodes, edges, and uploads
            indexes = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        """
                        SELECT indexname, indexdef
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND tablename IN ('nodes', 'edges', 'uploads')
                        """
                    )
                )
            }
            assert "ix_nodes_geom_geog" in indexes
            assert "gist" in indexes["ix_nodes_geom_geog"].lower()
            assert "geography" in indexes["ix_nodes_geom_geog"].lower()
            assert "ix_nodes_geom" in indexes
            assert "gist" in indexes["ix_nodes_geom"].lower()

            assert "ix_edges_geom" in indexes
            assert "uq_edges_unordered_endpoints" in indexes
            assert "ix_edges_source" in indexes
            assert "ix_edges_target" in indexes
            assert "ix_uploads_status_lease" in indexes

            # 5. Nodes: insert with ele and NULL ele, verify metric geography query
            node_a = conn.execute(
                text(
                    """
                    INSERT INTO nodes (geom, ele)
                    VALUES (ST_SetSRID(ST_MakePoint(35.0, 32.6), 4326), 420.5)
                    RETURNING id
                    """
                )
            ).scalar_one()
            node_b = conn.execute(
                text(
                    """
                    INSERT INTO nodes (geom, ele)
                    VALUES (ST_SetSRID(ST_MakePoint(35.001, 32.601), 4326), NULL)
                    RETURNING id
                    """
                )
            ).scalar_one()
            node_c = conn.execute(
                text(
                    """
                    INSERT INTO nodes (geom, ele)
                    VALUES (ST_SetSRID(ST_MakePoint(35.002, 32.602), 4326), 435.0)
                    RETURNING id
                    """
                )
            ).scalar_one()

            nearby_count = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM nodes
                    WHERE ST_DWithin(
                        geom::geography,
                        ST_SetSRID(ST_MakePoint(35.0, 32.6), 4326)::geography,
                        250
                    )
                    """
                )
            ).scalar_one()
            assert nearby_count == 2

            # 6. Edges: valid insert + constraint enforcement
            edge_id = conn.execute(
                text(
                    f"""
                    INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
                    VALUES (:src, :tgt, {_EDGE_GEOM_SQL}, 145.2, 2)
                    RETURNING id
                    """
                ),
                {"src": node_a, "tgt": node_b},
            ).scalar_one()
            assert edge_id > 0

            # Reject self-loop (source = target)
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(
                        f"""
                        INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
                        VALUES (:src, :src, {_EDGE_GEOM_SQL}, 10.0, 1)
                        """
                    ),
                    {"src": node_a},
                )

            # Reject unordered duplicate (target, source) when (source, target) exists
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(
                        f"""
                        INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
                        VALUES (:src, :tgt, {_EDGE_GEOM_SQL}, 145.2, 2)
                        """
                    ),
                    {"src": node_b, "tgt": node_a},
                )

            # Reject length_m <= 0
            for bad_len in (0.0, -12.5):
                with pytest.raises(IntegrityError), conn.begin_nested():
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
                            VALUES (:src, :tgt, {_EDGE_GEOM_SQL}, :len_m, 1)
                            """
                        ),
                        {"src": node_a, "tgt": node_c, "len_m": bad_len},
                    )

            # Reject initial_difficulty outside 1..3
            for bad_diff in (0, 4):
                with pytest.raises(IntegrityError), conn.begin_nested():
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
                            VALUES (:src, :tgt, {_EDGE_GEOM_SQL}, 50.0, :diff)
                            """
                        ),
                        {"src": node_a, "tgt": node_c, "diff": bad_diff},
                    )

            # 7. Tracks: valid MultiLineString insert
            track_id = "fp_35.000000_32.600000"
            created_at = conn.execute(
                text(
                    """
                    INSERT INTO tracks (id, normalized_geom)
                    VALUES (
                        :tid,
                        ST_SetSRID(
                            ST_GeomFromText('MULTILINESTRING((35.0 32.6, 35.001 32.601))'),
                            4326
                        )
                    )
                    RETURNING created_at
                    """
                ),
                {"tid": track_id},
            ).scalar_one()
            assert created_at is not None

            # 8. Uploads: valid insert + status & difficulty check constraints
            upload_id = str(uuid.uuid4())
            conn.execute(
                text(
                    """
                    INSERT INTO uploads (
                        id, filename, original_ref, file_sha256,
                        normalized_fingerprint, status, overall_difficulty, track_id
                    )
                    VALUES (
                        :uid, 'carmel.gpx', '/data/uploads/carmel.gpx', 'abc123',
                        :fp, 'PENDING', 2, :tid
                    )
                    """
                ),
                {"uid": upload_id, "fp": track_id, "tid": track_id},
            )

            # Invalid upload status must be rejected
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(
                        """
                        INSERT INTO uploads (
                            id, filename, original_ref, file_sha256,
                            normalized_fingerprint, status
                        )
                        VALUES (
                            :uid, 'bad.gpx', '/data/uploads/bad.gpx', 'def456',
                            'fp_bad', 'UNKNOWN_STATUS'
                        )
                        """
                    ),
                    {"uid": str(uuid.uuid4())},
                )

            # Invalid overall_difficulty outside 1..3 must be rejected
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(
                        """
                        INSERT INTO uploads (
                            id, filename, original_ref, file_sha256,
                            normalized_fingerprint, status, overall_difficulty
                        )
                        VALUES (
                            :uid, 'bad_diff.gpx', '/data/uploads/bad_diff.gpx', 'ghi789',
                            'fp_bad_diff', 'DRAFT', 5
                        )
                        """
                    ),
                    {"uid": str(uuid.uuid4())},
                )

            conn.rollback()
    finally:
        engine.dispose()


def test_alembic_0001_downgrade_base_and_clean_reupgrade(postgres_db_url: str) -> None:
    alembic_cfg = build_alembic_config(postgres_db_url)
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0001_poc_foundation")
    command.downgrade(alembic_cfg, "base")

    engine = create_engine(postgres_db_url)
    try:
        with engine.connect() as conn:
            assert _existing_foundation_tables(conn) == set()
            assert _installed_extensions(conn) == set()

        # Re-upgrade cleanly from base -> 0001_poc_foundation
        command.upgrade(alembic_cfg, "0001_poc_foundation")

        with engine.connect() as conn:
            assert _existing_foundation_tables(conn) == FOUNDATION_TABLES
            assert _installed_extensions(conn) == {"postgis", "pgrouting"}
            meta_rows = conn.execute(
                text("SELECT id, revision FROM graph_metadata ORDER BY id")
            ).fetchall()
            assert meta_rows == [(1, 0)]
    finally:
        engine.dispose()


def test_live_fastapi_health_and_public_config_against_testcontainers(
    postgres_db_url: str,
) -> None:
    alembic_cfg = build_alembic_config(postgres_db_url)
    command.upgrade(alembic_cfg, "head")

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        database_url=postgres_db_url,
        support_email="qa@ontrack.example",
        network_layer_min_zoom=14,
        max_upload_mb=12,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200
        assert health_resp.json() == {"status": "ok", "database": "ok"}

        config_resp = client.get("/api/config/public")
        assert config_resp.status_code == 200
        assert config_resp.json() == {
            "support_email": "qa@ontrack.example",
            "network_layer_min_zoom": 14,
            "max_upload_mb": 12,
        }
        assert postgres_db_url not in config_resp.text


def test_live_fastapi_health_returns_503_against_unreachable_database() -> None:
    unreachable_settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        database_url="postgresql+psycopg://ontrack:ontrack@127.0.0.1:1/ontrack",
    )
    app = create_app(unreachable_settings)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service temporarily unavailable.",
        "details": {"database": "unreachable"},
    }
