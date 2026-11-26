"""Live PostGIS/pgRouting Testcontainers integration tests for T-008 production schema.

Covers:
1. Full Alembic lifecycle: upgrade head -> downgrade 0001_poc_foundation -> downgrade base
   -> upgrade head, verifying zero leftover tables, views, Procrastinate functions, or types.
2. Live enforcement of every table, FK, ON DELETE rule, CHECK constraint, unique index,
   and partial unique index across all 16 domain tables + edge_difficulty view (AC-33/AC-34).
3. Procrastinate 3 native schema presence and live same-connection transactional deferral
   (commit visibility + atomic rollback) directly on the Alembic-migrated database.
4. Live SQLAlchemy 2.0 + GeoAlchemy2 ORM CRUD round-trip for all 16 models in
   app.core.models + app.configuration.models + edge_difficulty_view.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import procrastinate
import pytest
from alembic import command
from geoalchemy2.elements import WKTElement
from sqlalchemy import Connection, Engine, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.configuration.models import (
    CONFIG_KEY_FREE_ROUTE_CREATIONS,
    CONFIG_KEY_MIN_REQUIRED_TRACKS,
    CONFIG_KEY_NODE_SNAP_RADIUS_M,
    CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M,
    CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS,
    DEFAULT_APP_CONFIG,
    AppConfig,
)
from app.core.models import (
    BatchFile,
    BatchImport,
    Edge,
    EdgeContribution,
    EdgeRating,
    GraphMetadata,
    IdempotencyRecord,
    IngestionRun,
    Node,
    RouteUsage,
    Track,
    Upload,
    UploadSegment,
    User,
    UserSession,
    edge_difficulty_view,
)
from app.workers.queue_poc import SqlAlchemySessionConnector
from tests.conftest import build_alembic_config

pytestmark = pytest.mark.integration

FOUNDATION_TABLES: frozenset[str] = frozenset(
    {"graph_metadata", "nodes", "edges", "tracks", "uploads"}
)
PRODUCTION_ONLY_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "sessions",
        "app_config",
        "upload_segments",
        "batch_imports",
        "batch_files",
        "ingestion_runs",
        "edge_contributions",
        "edge_ratings",
        "route_usage",
        "idempotency_records",
    }
)
PROCRASTINATE_TABLES: frozenset[str] = frozenset(
    {
        "procrastinate_jobs",
        "procrastinate_events",
        "procrastinate_workers",
        "procrastinate_periodic_defers",
    }
)
ALL_PRODUCTION_TABLES: frozenset[str] = (
    FOUNDATION_TABLES | PRODUCTION_ONLY_TABLES | PROCRASTINATE_TABLES
)
FOUNDATION_UPLOAD_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "filename",
        "original_ref",
        "file_sha256",
        "normalized_fingerprint",
        "status",
        "overall_difficulty",
        "active_attempt_id",
        "attempt_count",
        "processing_started_at",
        "last_heartbeat_at",
        "lease_expires_at",
        "error_category",
        "safe_error_message",
        "track_id",
        "created_at",
        "accepted_at",
        "processed_at",
    }
)


def _public_base_tables(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
              AND table_name NOT IN ('alembic_version', 'spatial_ref_sys')
            """
        )
    )
    return {str(row[0]) for row in rows}


def _public_views(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema = 'public'
              AND table_name NOT IN ('geography_columns', 'geometry_columns')
            """
        )
    )
    return {str(row[0]) for row in rows}


def _procrastinate_routines(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT p.proname
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND p.proname LIKE 'procrastinate_%'
            """
        )
    )
    return {str(row[0]) for row in rows}


def _procrastinate_types(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT t.typname
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public'
              AND t.typname LIKE 'procrastinate_%'
            """
        )
    )
    return {str(row[0]) for row in rows}


def _table_columns(conn: Connection, table_name: str) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {str(row[0]) for row in rows}


def _public_indexes(conn: Connection) -> dict[str, str]:
    rows = conn.execute(
        text(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
            """
        )
    )
    return {str(row[0]): str(row[1]) for row in rows}


@pytest.fixture(scope="module")
def prod_engine(postgres_db_url: str) -> Generator[Engine, None, None]:
    """Upgrade to Alembic head cleanly (removing any prior 0001-only POC state)."""
    alembic_cfg = build_alembic_config(postgres_db_url)
    # Upgrade -> downgrade 0001 -> downgrade base -> upgrade head guarantees a fresh 0002 state
    # even when run in the same session after 0001-pinned POC modules.
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "0001_poc_foundation")
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(postgres_db_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def db_session_factory(prod_engine: Engine) -> sessionmaker[Session]:
    """Reset mutable domain/queue tables while preserving seeded graph_metadata & app_config."""
    with prod_engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    idempotency_records,
                    route_usage,
                    edge_ratings,
                    edge_contributions,
                    ingestion_runs,
                    batch_files,
                    batch_imports,
                    upload_segments,
                    uploads,
                    tracks,
                    edges,
                    nodes,
                    sessions,
                    users,
                    procrastinate_events,
                    procrastinate_periodic_defers,
                    procrastinate_jobs,
                    procrastinate_workers
                RESTART IDENTITY CASCADE
                """
            )
        )
        conn.execute(text("UPDATE graph_metadata SET revision = 0 WHERE id = 1"))
        conn.execute(text("DELETE FROM app_config"))
        conn.execute(
            text(
                """
                INSERT INTO app_config (key, value)
                VALUES
                    ('MIN_REQUIRED_TRACKS', '1'::jsonb),
                    ('FREE_ROUTE_CREATIONS', '0'::jsonb),
                    ('REJECTED_TRACK_GRACE_PERIOD_HOURS', '24'::jsonb),
                    ('POINT_SIMPLIFICATION_DISTANCE_M', '20.0'::jsonb),
                    ('NODE_SNAP_RADIUS_M', '20.0'::jsonb)
                """
            )
        )
    return sessionmaker(
        bind=prod_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


def _create_user(
    session: Session,
    *,
    username: str,
    email: str,
    user_type: str = "regular",
    preferred_language: str | None = None,
) -> User:
    kwargs: dict[str, Any] = {
        "username": username,
        "email": email,
        "first_name": "Dana",
        "last_name": "Cohen",
        "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA",
        "user_type": user_type,
    }
    if preferred_language is not None:
        kwargs["preferred_language"] = preferred_language
    user = User(**kwargs)
    session.add(user)
    session.flush()
    return user


def _create_edge(
    session: Session,
    *,
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
    length_m: float = 120.0,
    initial_difficulty: int = 1,
) -> tuple[Node, Node, Edge]:
    node_a = Node(geom=WKTElement(f"POINT({lon1} {lat1})", srid=4326), ele=250.0)
    node_b = Node(geom=WKTElement(f"POINT({lon2} {lat2})", srid=4326), ele=260.0)
    session.add_all([node_a, node_b])
    session.flush()
    edge = Edge(
        source=node_a.id,
        target=node_b.id,
        geom=WKTElement(f"LINESTRING({lon1} {lat1}, {lon2} {lat2})", srid=4326),
        length_m=length_m,
        initial_difficulty=initial_difficulty,
    )
    session.add(edge)
    session.flush()
    return node_a, node_b, edge


# ---------------------------------------------------------------------------
# 1. Full Alembic lifecycle: head -> 0001 -> base -> head
# ---------------------------------------------------------------------------


def test_alembic_full_lifecycle_upgrade_downgrade_and_zero_leftovers(
    postgres_db_url: str,
) -> None:
    alembic_cfg = build_alembic_config(postgres_db_url)
    engine = create_engine(postgres_db_url, pool_pre_ping=True)
    try:
        # Phase 1: upgrade head
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            ver: Any = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "0002_production_schema"
            assert _public_base_tables(conn) == ALL_PRODUCTION_TABLES
            assert _public_views(conn) == {"edge_difficulty"}
            assert len(_procrastinate_routines(conn)) == 18
            assert len(_procrastinate_types(conn)) >= 4

            # Verify expected production indexes are present
            indexes = _public_indexes(conn)
            for expected_idx in (
                "uq_users_email_lower",
                "ix_sessions_user_expires",
                "ix_sessions_expires_at",
                "uq_uploads_user_credit_fingerprint",
                "ix_uploads_user_created_at",
                "ix_uploads_user_status",
                "ix_uploads_draft_expires",
                "ix_batch_imports_status",
                "ix_batch_files_batch_status",
                "ix_batch_files_status_lease",
                "uq_ingestion_runs_upload_id",
                "uq_ingestion_runs_batch_file_id",
                "ix_ingestion_runs_track_id",
                "ix_edge_contributions_track_id",
                "ix_edge_contributions_ingestion_run_id",
                "ix_route_usage_user_state_expires",
                "ix_route_usage_replay_expires_at",
                "ix_idempotency_records_expires_at",
            ):
                assert expected_idx in indexes, f"Missing index {expected_idx}"

        # Phase 2: downgrade 0001_poc_foundation -> zero 0002 / Procrastinate leftovers
        command.downgrade(alembic_cfg, "0001_poc_foundation")
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "0001_poc_foundation"
            assert _public_base_tables(conn) == FOUNDATION_TABLES
            assert _public_views(conn) == set()
            assert _procrastinate_routines(conn) == set()
            assert _procrastinate_types(conn) == set()
            assert _table_columns(conn, "uploads") == FOUNDATION_UPLOAD_COLUMNS
            indexes_0001 = _public_indexes(conn)
            for removed_idx in (
                "uq_uploads_user_credit_fingerprint",
                "ix_uploads_user_created_at",
                "ix_uploads_user_status",
                "ix_uploads_draft_expires",
            ):
                assert removed_idx not in indexes_0001

        # Phase 3: downgrade base -> zero tables & extensions
        command.downgrade(alembic_cfg, "base")
        with engine.connect() as conn:
            assert _public_base_tables(conn) == set()
            assert _public_views(conn) == set()
            assert _procrastinate_routines(conn) == set()
            assert _procrastinate_types(conn) == set()
            ext = {
                str(r[0])
                for r in conn.execute(
                    text(
                        "SELECT extname FROM pg_extension "
                        "WHERE extname IN ('postgis', 'pgrouting')"
                    )
                )
            }
            assert ext == set()

        # Phase 4: clean re-upgrade base -> head (exercises 0001 + 0002 + Procrastinate fresh)
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "0002_production_schema"
            assert _public_base_tables(conn) == ALL_PRODUCTION_TABLES
            assert _public_views(conn) == {"edge_difficulty"}
            assert len(_procrastinate_routines(conn)) == 18
            assert conn.execute(text("SELECT PostGIS_Version()")).scalar_one()
            assert conn.execute(text("SELECT pgr_version()")).scalar_one()
            assert conn.execute(text("SELECT id, revision FROM graph_metadata")).fetchall() == [
                (1, 0)
            ]
            seeded_cfg = {
                str(r[0]): r[1]
                for r in conn.execute(text("SELECT key, value FROM app_config")).fetchall()
            }
            assert seeded_cfg == dict(DEFAULT_APP_CONFIG)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. Users, Sessions & AppConfig constraints, defaults & cascades
# ---------------------------------------------------------------------------


def test_users_sessions_and_app_config_constraints_and_cascades(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as session, session.begin():
        # Verify all 5 Gate 1 / T-007 seeded app_config defaults directly in DB
        cfg_rows = session.execute(select(AppConfig)).scalars().all()
        cfg_map = {row.key: row.value for row in cfg_rows}
        assert cfg_map == {
            CONFIG_KEY_MIN_REQUIRED_TRACKS: 1,
            CONFIG_KEY_FREE_ROUTE_CREATIONS: 0,
            CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS: 24,
            CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M: 20.0,
            CONFIG_KEY_NODE_SNAP_RADIUS_M: 20.0,
        }

        # Users: preferred_language server default 'he' and user_type default 'regular'
        raw_user_row = session.execute(
            text(
                """
                INSERT INTO users (username, email, first_name, last_name, password_hash)
                VALUES ('alice', 'Alice@Example.COM', 'Alice', 'Trail', 'hash1')
                RETURNING id, user_type, preferred_language, created_at
                """
            )
        ).one()
        alice_id = int(raw_user_row[0])
        assert raw_user_row[1] == "regular"
        assert raw_user_row[2] == "he"
        assert raw_user_row[3] is not None

        # Case-insensitive email uniqueness (uq_users_email_lower)
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    """
                    INSERT INTO users (username, email, first_name, last_name, password_hash)
                    VALUES ('alice2', 'alice@example.com', 'Alice', 'Dup', 'hash2')
                    """
                )
            )

        # Username uniqueness
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    """
                    INSERT INTO users (username, email, first_name, last_name, password_hash)
                    VALUES ('alice', 'unique@example.com', 'Alice', 'Dup', 'hash2')
                    """
                )
            )

        # Invalid user_type rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            _create_user(session, username="badtype", email="badtype@example.com", user_type="root")

        # Invalid preferred_language rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            _create_user(
                session,
                username="badlang",
                email="badlang@example.com",
                preferred_language="fr",
            )

        # Valid system + inactive users and 'en' language
        admin_user = _create_user(
            session,
            username="sysadmin",
            email="admin@example.com",
            user_type="system",
            preferred_language="en",
        )
        inactive_user = _create_user(
            session,
            username="disableduser",
            email="disabled@example.com",
            user_type="inactive",
            preferred_language="he",
        )
        assert admin_user.preferred_language == "en"
        assert inactive_user.user_type == "inactive"

        # app_config.updated_by FK -> users(id) ON DELETE SET NULL
        cfg_min = session.get(AppConfig, CONFIG_KEY_MIN_REQUIRED_TRACKS)
        assert cfg_min is not None
        cfg_min.updated_by = admin_user.id
        session.flush()

        # Sessions: valid row + expires_at > created_at check + unique session_id
        now = datetime.now(UTC)
        sid = uuid4()
        sess = UserSession(
            token_hash="sha256_token_1",
            session_id=sid,
            user_id=alice_id,
            created_at=now,
            expires_at=now + timedelta(hours=12),
            grace_until=now + timedelta(hours=24),
        )
        session.add(sess)
        session.flush()

        # expires_at == created_at rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                UserSession(
                    token_hash="sha256_token_eq",
                    session_id=uuid4(),
                    user_id=alice_id,
                    created_at=now,
                    expires_at=now,
                )
            )
            session.flush()

        # expires_at < created_at rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                UserSession(
                    token_hash="sha256_token_past",
                    session_id=uuid4(),
                    user_id=alice_id,
                    created_at=now,
                    expires_at=now - timedelta(seconds=1),
                )
            )
            session.flush()

        # Duplicate session_id rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                UserSession(
                    token_hash="sha256_token_dup_sid",
                    session_id=sid,
                    user_id=alice_id,
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            session.flush()

        # Deleting alice cascades to sessions
        session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": alice_id})
        session.flush()
        remaining_sessions = session.execute(
            select(UserSession).where(UserSession.user_id == alice_id)
        ).scalars().all()
        assert remaining_sessions == []

        # Deleting admin_user sets app_config.updated_by to NULL (ON DELETE SET NULL)
        session.delete(admin_user)
        session.flush()
        session.expire(cfg_min)
        reloaded_cfg = session.get(AppConfig, CONFIG_KEY_MIN_REQUIRED_TRACKS)
        assert reloaded_cfg is not None
        assert reloaded_cfg.updated_by is None


# ---------------------------------------------------------------------------
# 3. Uploads & UploadSegments: NOT NULL user_id, partial credit fingerprint, cascade
# ---------------------------------------------------------------------------


def test_uploads_and_upload_segments_constraints_and_partial_uniqueness(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as session, session.begin():
        user_a = _create_user(session, username="rider_a", email="rider_a@example.com")
        user_b = _create_user(session, username="rider_b", email="rider_b@example.com")

        # uploads.user_id is strictly NOT NULL
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    """
                    INSERT INTO uploads (
                        id, filename, original_ref, file_sha256,
                        normalized_fingerprint, status
                    )
                    VALUES (:id, 'x.gpx', 'uploads/x.gpx', 'sha', 'fp', 'DRAFT')
                    """
                ),
                {"id": uuid4()},
            )

        # Invalid rejection_code rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                Upload(
                    id=uuid4(),
                    user_id=user_a.id,
                    filename="bad_rej.gpx",
                    original_ref="uploads/bad_rej.gpx",
                    file_sha256="sha_bad_rej",
                    normalized_fingerprint="fp_bad_rej",
                    status="REJECTED",
                    rejection_code="UNKNOWN_CODE",
                )
            )
            session.flush()

        # Negative attempt_count rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                Upload(
                    id=uuid4(),
                    user_id=user_a.id,
                    filename="neg_attempt.gpx",
                    original_ref="uploads/neg_attempt.gpx",
                    file_sha256="sha_neg",
                    normalized_fingerprint="fp_neg",
                    status="DRAFT",
                    attempt_count=-1,
                )
            )
            session.flush()

        # Partial unique credit-bearing fingerprint:
        # 1) Same user + same fingerprint allows multiple DRAFT and REJECTED rows
        shared_fp = "fp_carmel_canonical_6dp"
        draft_1 = Upload(
            id=uuid4(),
            user_id=user_a.id,
            filename="carmel_draft1.gpx",
            original_ref="uploads/carmel_draft1.gpx",
            file_sha256="sha1",
            normalized_fingerprint=shared_fp,
            status="DRAFT",
            draft_expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        draft_2 = Upload(
            id=uuid4(),
            user_id=user_a.id,
            filename="carmel_draft2.gpx",
            original_ref="uploads/carmel_draft2.gpx",
            file_sha256="sha2",
            normalized_fingerprint=shared_fp,
            status="DRAFT",
        )
        rejected_1 = Upload(
            id=uuid4(),
            user_id=user_a.id,
            filename="carmel_rej1.gpx",
            original_ref="uploads/carmel_rej1.gpx",
            file_sha256="sha3",
            normalized_fingerprint=shared_fp,
            status="REJECTED",
            rejection_code="DEGENERATE_GEOMETRY",
        )
        rejected_2 = Upload(
            id=uuid4(),
            user_id=user_a.id,
            filename="carmel_rej2.gpx",
            original_ref="uploads/carmel_rej2.gpx",
            file_sha256="sha4",
            normalized_fingerprint=shared_fp,
            status="REJECTED",
            rejection_code="QUALITY_RULE_FAILED",
        )
        session.add_all([draft_1, draft_2, rejected_1, rejected_2])
        session.flush()

        # 2) Adding one credit-bearing row (PENDING) alongside DRAFT/REJECTED succeeds
        credit_a = Upload(
            id=uuid4(),
            user_id=user_a.id,
            filename="carmel_active.gpx",
            original_ref="uploads/carmel_active.gpx",
            file_sha256="sha5",
            normalized_fingerprint=shared_fp,
            status="PENDING",
            overall_difficulty=2,
        )
        session.add(credit_a)
        session.flush()

        # 3) Any second credit-bearing status for SAME user + SAME fingerprint is rejected
        for credit_status in ("PENDING", "PROCESSING", "RETRY", "TECHNICAL_FAILED", "ACTIVE"):
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    Upload(
                        id=uuid4(),
                        user_id=user_a.id,
                        filename=f"dup_{credit_status}.gpx",
                        original_ref=f"uploads/dup_{credit_status}.gpx",
                        file_sha256=f"sha_dup_{credit_status}",
                        normalized_fingerprint=shared_fp,
                        status=credit_status,
                        overall_difficulty=1,
                    )
                )
                session.flush()

        # 4) Cross-user duplicate with SAME fingerprint in ACTIVE status is ALLOWED (Gate 1)
        credit_b = Upload(
            id=uuid4(),
            user_id=user_b.id,
            filename="carmel_user_b.gpx",
            original_ref="uploads/carmel_user_b.gpx",
            file_sha256="sha6",
            normalized_fingerprint=shared_fp,
            status="ACTIVE",
            overall_difficulty=3,
        )
        session.add(credit_b)
        session.flush()

        # upload_segments: valid segments, negative/duplicate index rejected, cascade delete
        seg0 = UploadSegment(
            upload_id=draft_1.id,
            segment_index=0,
            geom=WKTElement("LINESTRING(35.0 32.6, 35.01 32.61)", srid=4326),
        )
        seg1 = UploadSegment(
            upload_id=draft_1.id,
            segment_index=1,
            geom=WKTElement("LINESTRING(35.02 32.62, 35.03 32.63)", srid=4326),
        )
        session.add_all([seg0, seg1])
        session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                UploadSegment(
                    upload_id=draft_1.id,
                    segment_index=-1,
                    geom=WKTElement("LINESTRING(35.0 32.6, 35.01 32.61)", srid=4326),
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                UploadSegment(
                    upload_id=draft_1.id,
                    segment_index=0,
                    geom=WKTElement("LINESTRING(35.04 32.64, 35.05 32.65)", srid=4326),
                )
            )
            session.flush()

        session.delete(draft_1)
        session.flush()
        remaining_segs = session.execute(
            select(UploadSegment).where(UploadSegment.upload_id == draft_1.id)
        ).scalars().all()
        assert remaining_segs == []


# ---------------------------------------------------------------------------
# 4. BatchImports, BatchFiles, IngestionRuns (XOR + partial uniques) & EdgeContributions
# ---------------------------------------------------------------------------


def test_batch_imports_files_ingestion_runs_and_multi_run_edge_contributions(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as session, session.begin():
        admin = _create_user(
            session, username="batch_admin", email="batch_admin@example.com", user_type="system"
        )
        user_a = _create_user(session, username="contrib_a", email="contrib_a@example.com")
        user_b = _create_user(session, username="contrib_b", email="contrib_b@example.com")

        track = Track(
            id="fp_shared_track_multi_run",
            normalized_geom=WKTElement(
                "MULTILINESTRING((35.0 32.6, 35.002 32.602))", srid=4326
            ),
        )
        session.add(track)
        _, _, edge = _create_edge(
            session,
            lon1=35.0,
            lat1=32.6,
            lon2=35.002,
            lat2=32.602,
            length_m=285.4,
            initial_difficulty=2,
        )

        # batch_imports check constraints
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                BatchImport(
                    id=uuid4(),
                    initiated_by=admin.id,
                    source="ftp",
                    status="QUEUED",
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                BatchImport(
                    id=uuid4(),
                    initiated_by=admin.id,
                    source="server",
                    status="UNKNOWN",
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                BatchImport(
                    id=uuid4(),
                    initiated_by=admin.id,
                    source="cloud",
                    status="RUNNING",
                    failed_count=-1,
                )
            )
            session.flush()

        batch_1 = BatchImport(
            id=uuid4(),
            initiated_by=admin.id,
            source="server",
            status="RUNNING",
            total_files=2,
        )
        batch_2 = BatchImport(
            id=uuid4(),
            initiated_by=admin.id,
            source="cloud",
            status="QUEUED",
            total_files=1,
        )
        session.add_all([batch_1, batch_2])
        session.flush()

        # batch_files constraints & unique (batch_id, source_ref)
        bf_1 = BatchFile(
            batch_id=batch_1.id,
            source_ref="carmel/trail_01.gpx",
            file_sha256="sha_bf1",
            status="COMPLETED",
            track_id=track.id,
        )
        bf_2 = BatchFile(
            batch_id=batch_1.id,
            source_ref="carmel/trail_02.gpx",
            status="PENDING",
        )
        # Same source_ref in a DIFFERENT batch is allowed
        bf_other_batch = BatchFile(
            batch_id=batch_2.id,
            source_ref="carmel/trail_01.gpx",
            status="PENDING",
        )
        session.add_all([bf_1, bf_2, bf_other_batch])
        session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                BatchFile(
                    batch_id=batch_1.id,
                    source_ref="carmel/trail_01.gpx",
                    status="PENDING",
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                BatchFile(
                    batch_id=batch_1.id,
                    source_ref="carmel/bad_status.gpx",
                    status="BOGUS",
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                BatchFile(
                    batch_id=batch_1.id,
                    source_ref="carmel/neg_attempt.gpx",
                    attempt_count=-1,
                )
            )
            session.flush()

        # Two user uploads sharing the same canonical track_id (cross-user duplicate)
        up_a = Upload(
            id=uuid4(),
            user_id=user_a.id,
            filename="trail.gpx",
            original_ref="uploads/a.gpx",
            file_sha256="sha_a",
            normalized_fingerprint=track.id,
            status="ACTIVE",
            overall_difficulty=2,
            track_id=track.id,
        )
        up_b = Upload(
            id=uuid4(),
            user_id=user_b.id,
            filename="trail.gpx",
            original_ref="uploads/b.gpx",
            file_sha256="sha_b",
            normalized_fingerprint=track.id,
            status="ACTIVE",
            overall_difficulty=3,
            track_id=track.id,
        )
        session.add_all([up_a, up_b])
        session.flush()

        # ingestion_runs XOR check violations
        for bad_source_type, bad_uid, bad_bfid in (
            ("upload", None, None),
            ("upload", up_a.id, bf_1.id),
            ("upload", None, bf_1.id),
            ("batch_file", None, None),
            ("batch_file", up_a.id, bf_1.id),
            ("batch_file", up_a.id, None),
            ("invalid_type", up_a.id, None),
        ):
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    IngestionRun(
                        id=uuid4(),
                        source_type=bad_source_type,
                        upload_id=bad_uid,
                        batch_file_id=bad_bfid,
                        track_id=track.id,
                        config_snapshot={"point_simplification_distance_m": 20.0},
                        graph_revision_before=0,
                    )
                )
                session.flush()

        # Negative graph_revision_before rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                IngestionRun(
                    id=uuid4(),
                    source_type="upload",
                    upload_id=up_a.id,
                    batch_file_id=None,
                    track_id=track.id,
                    config_snapshot={"node_snap_radius_m": 20.0},
                    graph_revision_before=-1,
                )
            )
            session.flush()

        # Valid multi-run provenance: 2 upload runs + 1 batch_file run for the SAME track_id
        run_up_a = IngestionRun(
            id=uuid4(),
            source_type="upload",
            upload_id=up_a.id,
            batch_file_id=None,
            track_id=track.id,
            config_snapshot={"point_simplification_distance_m": 20.0, "node_snap_radius_m": 20.0},
            graph_revision_before=0,
            completed_at=datetime.now(UTC),
        )
        run_up_b = IngestionRun(
            id=uuid4(),
            source_type="upload",
            upload_id=up_b.id,
            batch_file_id=None,
            track_id=track.id,
            config_snapshot={"point_simplification_distance_m": 20.0, "node_snap_radius_m": 10.0},
            graph_revision_before=1,
            completed_at=datetime.now(UTC),
        )
        run_bf_1 = IngestionRun(
            id=uuid4(),
            source_type="batch_file",
            upload_id=None,
            batch_file_id=bf_1.id,
            track_id=track.id,
            config_snapshot={"point_simplification_distance_m": 20.0, "node_snap_radius_m": 20.0},
            graph_revision_before=2,
            completed_at=datetime.now(UTC),
        )
        session.add_all([run_up_a, run_up_b, run_bf_1])
        session.flush()

        # Partial unique uq_ingestion_runs_upload_id rejects duplicate upload_id
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                IngestionRun(
                    id=uuid4(),
                    source_type="upload",
                    upload_id=up_a.id,
                    batch_file_id=None,
                    track_id=track.id,
                    config_snapshot={},
                    graph_revision_before=3,
                )
            )
            session.flush()

        # Partial unique uq_ingestion_runs_batch_file_id rejects duplicate batch_file_id
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                IngestionRun(
                    id=uuid4(),
                    source_type="batch_file",
                    upload_id=None,
                    batch_file_id=bf_1.id,
                    track_id=track.id,
                    config_snapshot={},
                    graph_revision_before=3,
                )
            )
            session.flush()

        # edge_contributions: same edge contributed by all 3 runs (even with same track_id!)
        session.add_all(
            [
                EdgeContribution(edge_id=edge.id, ingestion_run_id=run_up_a.id, track_id=track.id),
                EdgeContribution(edge_id=edge.id, ingestion_run_id=run_up_b.id, track_id=track.id),
                EdgeContribution(edge_id=edge.id, ingestion_run_id=run_bf_1.id, track_id=track.id),
            ]
        )
        session.flush()

        # Duplicate (edge_id, ingestion_run_id) rejected by composite PK
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                EdgeContribution(edge_id=edge.id, ingestion_run_id=run_up_a.id, track_id=track.id)
            )
            session.flush()

        contrib_count: Any = session.execute(
            text("SELECT COUNT(*) FROM edge_contributions WHERE edge_id = :eid"),
            {"eid": edge.id},
        ).scalar_one()
        assert int(contrib_count) == 3

        # Cascade: deleting batch_1 deletes bf_1 -> cascades to run_bf_1 -> deletes its contrib
        session.delete(batch_1)
        session.flush()
        remaining_after_cascade: Any = session.execute(
            text("SELECT COUNT(*) FROM edge_contributions WHERE edge_id = :eid"),
            {"eid": edge.id},
        ).scalar_one()
        assert int(remaining_after_cascade) == 2


# ---------------------------------------------------------------------------
# 5. EdgeRatings & edge_difficulty view (AC-33 / AC-34 boundary math)
# ---------------------------------------------------------------------------


def test_edge_ratings_and_edge_difficulty_view_ac33_ac34_boundary_math(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as session, session.begin():
        u1 = _create_user(session, username="rater1", email="rater1@example.com")
        u2 = _create_user(session, username="rater2", email="rater2@example.com")
        u3 = _create_user(session, username="rater3", email="rater3@example.com")

        _, _, edge_init1 = _create_edge(
            session, lon1=35.0, lat1=32.6, lon2=35.001, lat2=32.6, initial_difficulty=1
        )
        _, _, edge_init2 = _create_edge(
            session, lon1=35.01, lat1=32.6, lon2=35.011, lat2=32.6, initial_difficulty=2
        )
        _, _, edge_init3 = _create_edge(
            session, lon1=35.02, lat1=32.6, lon2=35.021, lat2=32.6, initial_difficulty=3
        )

        # Invalid rating difficulty outside 1..3 rejected
        for bad_diff in (0, 4, -1):
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(EdgeRating(edge_id=edge_init1.id, user_id=u1.id, difficulty=bad_diff))
                session.flush()

        def _fetch_view_row(edge_id: int) -> tuple[float, int, int]:
            row = session.execute(
                select(
                    edge_difficulty_view.c.avg_difficulty,
                    edge_difficulty_view.c.report_count,
                    edge_difficulty_view.c.display_level,
                ).where(edge_difficulty_view.c.edge_id == edge_id)
            ).one()
            return float(row[0]), int(row[1]), int(row[2])

        # 0-rating edges: report_count MUST be 0 (not 1), avg_difficulty == initial_difficulty
        assert _fetch_view_row(edge_init1.id) == (1.0, 0, 1)
        assert _fetch_view_row(edge_init2.id) == (2.0, 0, 2)
        assert _fetch_view_row(edge_init3.id) == (3.0, 0, 3)

        # Gate 1 + AC-33: initial_difficulty=3 is EXCLUDED from mean once explicit rating exists
        r1 = EdgeRating(edge_id=edge_init3.id, user_id=u1.id, difficulty=1)
        session.add(r1)
        session.flush()
        assert _fetch_view_row(edge_init3.id) == (1.0, 1, 1)

        # Duplicate (edge_id, user_id) rejected by composite PK
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(EdgeRating(edge_id=edge_init3.id, user_id=u1.id, difficulty=3))
            session.flush()

        # AC-34 boundary: ratings (1, 2) -> avg = 1.5 -> display_level = 2 (1.5 rounds to 2)
        r2 = EdgeRating(edge_id=edge_init3.id, user_id=u2.id, difficulty=2)
        session.add(r2)
        session.flush()
        avg, cnt, lvl = _fetch_view_row(edge_init3.id)
        assert avg == pytest.approx(1.5)
        assert cnt == 2
        assert lvl == 2

        # AC-34 boundary: ratings (1, 1, 2) -> avg = 4/3 = 1.3333... < 1.5 -> display_level = 1
        r3 = EdgeRating(edge_id=edge_init3.id, user_id=u3.id, difficulty=1)
        session.add(r3)
        session.flush()
        avg, cnt, lvl = _fetch_view_row(edge_init3.id)
        assert avg == pytest.approx(4.0 / 3.0)
        assert cnt == 3
        assert lvl == 1

        # AC-34 boundary: ratings (2, 2, 3) -> avg = 7/3 = 2.3333... < 2.5 -> display_level = 2
        r1.difficulty = 2
        r2.difficulty = 2
        r3.difficulty = 3
        session.flush()
        avg, cnt, lvl = _fetch_view_row(edge_init3.id)
        assert avg == pytest.approx(7.0 / 3.0)
        assert cnt == 3
        assert lvl == 2

        # AC-34 boundary: ratings (2, 3) -> avg = 2.5 -> display_level = 3 (2.5 rounds to 3)
        session.delete(r1)
        session.flush()
        avg, cnt, lvl = _fetch_view_row(edge_init3.id)
        assert avg == pytest.approx(2.5)
        assert cnt == 2
        assert lvl == 3

        # Re-rating: u2 updates 2 -> 1 -> ratings (1, 3) -> avg = 2.0 -> display_level = 2
        r2.difficulty = 1
        session.flush()
        avg, cnt, lvl = _fetch_view_row(edge_init3.id)
        assert avg == pytest.approx(2.0)
        assert cnt == 2
        assert lvl == 2


# ---------------------------------------------------------------------------
# 6. RouteUsage & IdempotencyRecords constraints
# ---------------------------------------------------------------------------


def test_route_usage_and_idempotency_records_constraints(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as session, session.begin():
        u1 = _create_user(session, username="route_u1", email="route_u1@example.com")
        u2 = _create_user(session, username="route_u2", email="route_u2@example.com")
        now = datetime.now(UTC)

        # route_usage: valid states + unique (user_id, request_id)
        req_id = uuid4()
        ru1 = RouteUsage(
            user_id=u1.id,
            request_id=req_id,
            coord_fingerprint="32.600000,35.000000->32.700000,35.100000",
            attempt_id=uuid4(),
            state="RESERVED",
            expires_at=now + timedelta(minutes=2),
        )
        # Same request_id for a different user is allowed
        ru2 = RouteUsage(
            user_id=u2.id,
            request_id=req_id,
            coord_fingerprint="32.600000,35.000000->32.700000,35.100000",
            attempt_id=uuid4(),
            state="NON_ALLOWANCE",
            response_json={"segments": []},
            replay_expires_at=now + timedelta(minutes=10),
            settled_at=now,
        )
        session.add_all([ru1, ru2])
        session.flush()

        # Duplicate (user_id, request_id) rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                RouteUsage(
                    user_id=u1.id,
                    request_id=req_id,
                    coord_fingerprint="other",
                    attempt_id=uuid4(),
                    state="CONSUMED",
                )
            )
            session.flush()

        # Invalid state rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                RouteUsage(
                    user_id=u1.id,
                    request_id=uuid4(),
                    coord_fingerprint="other",
                    attempt_id=uuid4(),
                    state="INVALID_STATE",
                )
            )
            session.flush()

        # idempotency_records: key length boundaries 1 and 255 accepted; 0 and 256 rejected
        rec_min = IdempotencyRecord(
            user_id=u1.id,
            operation="upload_confirm",
            key="k",
            payload_fingerprint="fp1",
            status_code=202,
            response_json={"status": "PENDING"},
            expires_at=now + timedelta(hours=24),
        )
        rec_max = IdempotencyRecord(
            user_id=u1.id,
            operation="upload_confirm",
            key="x" * 255,
            payload_fingerprint="fp2",
            status_code=202,
            response_json={"status": "PENDING"},
            expires_at=now + timedelta(hours=24),
        )
        # Same key allowed for different operation or different user
        rec_diff_op = IdempotencyRecord(
            user_id=u1.id,
            operation="other_op",
            key="k",
            payload_fingerprint="fp3",
            status_code=200,
            response_json={},
            expires_at=now + timedelta(hours=24),
        )
        rec_diff_user = IdempotencyRecord(
            user_id=u2.id,
            operation="upload_confirm",
            key="k",
            payload_fingerprint="fp4",
            status_code=202,
            response_json={},
            expires_at=now + timedelta(hours=24),
        )
        session.add_all([rec_min, rec_max, rec_diff_op, rec_diff_user])
        session.flush()

        # Duplicate PK (user_id, operation, key) rejected
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                IdempotencyRecord(
                    user_id=u1.id,
                    operation="upload_confirm",
                    key="k",
                    payload_fingerprint="fp_dup",
                    status_code=202,
                    response_json={},
                    expires_at=now + timedelta(hours=24),
                )
            )
            session.flush()

        # Key length 0 and 256 rejected
        for bad_key in ("", "y" * 256):
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    IdempotencyRecord(
                        user_id=u1.id,
                        operation="upload_confirm",
                        key=bad_key,
                        payload_fingerprint="fp_bad",
                        status_code=202,
                        response_json={},
                        expires_at=now + timedelta(hours=24),
                    )
                )
                session.flush()

        # Deleting u1 cascades to both route_usage and idempotency_records
        session.delete(u1)
        session.flush()
        assert (
            session.execute(select(RouteUsage).where(RouteUsage.user_id == u1.id)).scalars().all()
            == []
        )
        assert (
            session.execute(select(IdempotencyRecord).where(IdempotencyRecord.user_id == u1.id))
            .scalars()
            .all()
            == []
        )


# ---------------------------------------------------------------------------
# 7. Procrastinate 3 live same-connection transactional deferral on 0002 DB
# ---------------------------------------------------------------------------


def test_procrastinate_3_same_connection_transactional_deferral_and_rollback(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Verify Procrastinate 3 schema from Alembic 0002 supports same-tx defer + rollback."""
    with db_session_factory() as session, session.begin():
        user = _create_user(session, username="queue_rider", email="queue_rider@example.com")
        user_id = user.id

    def _defer_on_session(session: Session, upload_id: str) -> int:
        connector = SqlAlchemySessionConnector(session)
        proc_app = procrastinate.App(connector=connector)

        @proc_app.task(name="ontrack.qa.ingest_upload", queue="uploads")
        def _ingest_task(target_upload_id: str) -> None:  # pragma: no cover
            del target_upload_id

        with proc_app.open():
            job_id = _ingest_task.configure(
                queueing_lock=f"upload:{upload_id}"
            ).defer(target_upload_id=upload_id)
        return int(job_id)

    # Case A: Commit atomicity — uncommitted row + job invisible to observer, visible after commit
    uid_commit = uuid4()
    with db_session_factory() as session, session.begin():
        session.add(
            Upload(
                id=uid_commit,
                user_id=user_id,
                filename="commit_test.gpx",
                original_ref="uploads/commit_test.gpx",
                file_sha256="sha_commit",
                normalized_fingerprint="fp_commit",
                status="PENDING",
                overall_difficulty=2,
            )
        )
        session.flush()
        committed_job_id = _defer_on_session(session, str(uid_commit))
        assert committed_job_id > 0

        # Observer on an independent connection sees NEITHER the upload NOR the Procrastinate job
        with db_session_factory() as observer:
            assert observer.get(Upload, uid_commit) is None
            obs_jobs: Any = observer.execute(
                text("SELECT COUNT(*) FROM procrastinate_jobs WHERE id = :jid"),
                {"jid": committed_job_id},
            ).scalar_one()
            assert int(obs_jobs) == 0

    with db_session_factory() as verifier:
        assert verifier.get(Upload, uid_commit) is not None
        job_row = verifier.execute(
            text(
                "SELECT id, status, queue_name, task_name, args "
                "FROM procrastinate_jobs WHERE id = :jid"
            ),
            {"jid": committed_job_id},
        ).one()
        assert int(job_row[0]) == committed_job_id
        assert str(job_row[1]) == "todo"
        assert str(job_row[2]) == "uploads"
        assert str(job_row[3]) == "ontrack.qa.ingest_upload"
        assert job_row[4] == {"target_upload_id": str(uid_commit)}

    # Case B: Rollback atomicity — failure after deferral rolls back BOTH upload & job
    uid_rollback = uuid4()
    rolled_back_job_id: int | None = None
    with (
        pytest.raises(RuntimeError, match="injected_post_defer_fault"),
        db_session_factory() as session,
        session.begin(),
    ):
        session.add(
            Upload(
                id=uid_rollback,
                user_id=user_id,
                filename="rollback_test.gpx",
                original_ref="uploads/rollback_test.gpx",
                file_sha256="sha_rb",
                normalized_fingerprint="fp_rb",
                status="PENDING",
                overall_difficulty=1,
            )
        )
        session.flush()
        rolled_back_job_id = _defer_on_session(session, str(uid_rollback))
        assert rolled_back_job_id > 0
        raise RuntimeError("injected_post_defer_fault")

    assert rolled_back_job_id is not None
    with db_session_factory() as verifier:
        assert verifier.get(Upload, uid_rollback) is None
        rb_jobs: Any = verifier.execute(
            text("SELECT COUNT(*) FROM procrastinate_jobs WHERE id = :jid"),
            {"jid": rolled_back_job_id},
        ).scalar_one()
        assert int(rb_jobs) == 0


# ---------------------------------------------------------------------------
# 8. Live SQLAlchemy ORM CRUD round-trip for ALL 16 models + edge_difficulty_view
# ---------------------------------------------------------------------------


def test_orm_crud_round_trip_all_16_models_and_edge_difficulty_view(
    db_session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    session_token = "orm_token_hash_001"
    upload_id = uuid4()
    batch_id = uuid4()
    run_id = uuid4()
    request_id = uuid4()

    with db_session_factory() as session, session.begin():
        # 1. GraphMetadata (Read + Update)
        gm = session.get(GraphMetadata, 1)
        assert gm is not None
        assert gm.revision == 0
        gm.revision = 5

        # 2. User (Create + Read + Update)
        user = _create_user(
            session,
            username="orm_tester",
            email="orm_tester@example.com",
            user_type="system",
            preferred_language="he",
        )
        user.preferred_language = "en"
        user_id = user.id

        # 3. UserSession (Create)
        user_session = UserSession(
            token_hash=session_token,
            session_id=uuid4(),
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        session.add(user_session)

        # 4. AppConfig (Read + Update)
        cfg = session.get(AppConfig, CONFIG_KEY_FREE_ROUTE_CREATIONS)
        assert cfg is not None
        cfg.value = 3
        cfg.updated_by = user_id

        # 5. Node & 6. Edge (Create)
        node_a, node_b, edge = _create_edge(
            session,
            lon1=35.05,
            lat1=32.65,
            lon2=35.06,
            lat2=32.66,
            length_m=1420.5,
            initial_difficulty=2,
        )
        node_a_id, node_b_id, edge_id = node_a.id, node_b.id, edge.id

        # 7. Track (Create)
        track = Track(
            id="fp_orm_roundtrip",
            normalized_geom=WKTElement(
                "MULTILINESTRING((35.05 32.65, 35.06 32.66))", srid=4326
            ),
        )
        session.add(track)

        # 8. Upload (Create)
        upload = Upload(
            id=upload_id,
            user_id=user_id,
            filename="orm_roundtrip.gpx",
            original_ref="uploads/orm_roundtrip.gpx",
            file_sha256="sha_orm_rt",
            normalized_fingerprint=track.id,
            status="ACTIVE",
            overall_difficulty=2,
            ingestion_config_snapshot={
                "point_simplification_distance_m": 20.0,
                "node_snap_radius_m": 20.0,
            },
            attempt_count=1,
            track_id=track.id,
            accepted_at=now,
            processed_at=now,
        )
        session.add(upload)
        session.flush()

        # 9. UploadSegment (Create)
        segment = UploadSegment(
            upload_id=upload_id,
            segment_index=0,
            geom=WKTElement("LINESTRING(35.05 32.65, 35.06 32.66)", srid=4326),
        )
        session.add(segment)

        # 10. BatchImport & 11. BatchFile (Create)
        batch = BatchImport(
            id=batch_id,
            initiated_by=user_id,
            source="server",
            status="COMPLETED",
            total_files=1,
            imported_count=1,
            finished_at=now,
        )
        session.add(batch)
        session.flush()

        batch_file = BatchFile(
            batch_id=batch_id,
            source_ref="legacy/track_01.gpx",
            file_sha256="sha_legacy_01",
            status="COMPLETED",
            attempt_count=1,
            ingestion_config_snapshot={"node_snap_radius_m": 20.0},
            track_id=track.id,
            started_at=now,
            finished_at=now,
        )
        session.add(batch_file)
        session.flush()
        batch_file_id = batch_file.id

        # 12. IngestionRun (Create)
        run = IngestionRun(
            id=run_id,
            source_type="upload",
            upload_id=upload_id,
            batch_file_id=None,
            track_id=track.id,
            config_snapshot={"point_simplification_distance_m": 20.0},
            graph_revision_before=4,
            completed_at=now,
        )
        session.add(run)
        session.flush()

        # 13. EdgeContribution (Create)
        contrib = EdgeContribution(
            edge_id=edge_id,
            ingestion_run_id=run_id,
            track_id=track.id,
        )
        session.add(contrib)

        # 14. EdgeRating (Create)
        rating = EdgeRating(edge_id=edge_id, user_id=user_id, difficulty=3)
        session.add(rating)

        # 15. RouteUsage (Create)
        usage = RouteUsage(
            user_id=user_id,
            request_id=request_id,
            coord_fingerprint="32.65,35.05->32.66,35.06",
            attempt_id=uuid4(),
            state="CONSUMED",
            response_json={"distance_m": 1420.5},
            replay_expires_at=now + timedelta(minutes=15),
            settled_at=now,
        )
        session.add(usage)

        # 16. IdempotencyRecord (Create)
        idem = IdempotencyRecord(
            user_id=user_id,
            operation="upload_confirm",
            key="idem-orm-roundtrip-1",
            payload_fingerprint="payload_fp_1",
            status_code=202,
            response_json={"upload_id": str(upload_id)},
            expires_at=now + timedelta(hours=24),
        )
        session.add(idem)

    # Read back every single model & edge_difficulty_view in a fresh Session
    with db_session_factory() as reader:
        gm_read = reader.get(GraphMetadata, 1)
        assert gm_read is not None and gm_read.revision == 5

        user_read = reader.get(User, user_id)
        assert user_read is not None and user_read.preferred_language == "en"

        sess_read = reader.get(UserSession, session_token)
        assert sess_read is not None and sess_read.user_id == user_id

        cfg_read = reader.get(AppConfig, CONFIG_KEY_FREE_ROUTE_CREATIONS)
        assert cfg_read is not None and cfg_read.value == 3 and cfg_read.updated_by == user_id

        assert reader.get(Node, node_a_id) is not None
        assert reader.get(Node, node_b_id) is not None
        edge_read = reader.get(Edge, edge_id)
        assert edge_read is not None and edge_read.initial_difficulty == 2

        track_read = reader.get(Track, "fp_orm_roundtrip")
        assert track_read is not None

        upload_read = reader.get(Upload, upload_id)
        assert upload_read is not None and upload_read.status == "ACTIVE"
        assert upload_read.ingestion_config_snapshot == {
            "point_simplification_distance_m": 20.0,
            "node_snap_radius_m": 20.0,
        }

        segs = reader.execute(
            select(UploadSegment).where(UploadSegment.upload_id == upload_id)
        ).scalars().all()
        assert len(segs) == 1 and segs[0].segment_index == 0

        batch_read = reader.get(BatchImport, batch_id)
        assert batch_read is not None and batch_read.imported_count == 1

        bf_read = reader.get(BatchFile, batch_file_id)
        assert bf_read is not None and bf_read.source_ref == "legacy/track_01.gpx"

        run_read = reader.get(IngestionRun, run_id)
        assert run_read is not None and run_read.graph_revision_before == 4

        contrib_read = reader.get(EdgeContribution, (edge_id, run_id))
        assert contrib_read is not None and contrib_read.track_id == "fp_orm_roundtrip"

        rating_read = reader.get(EdgeRating, (edge_id, user_id))
        assert rating_read is not None and rating_read.difficulty == 3

        usage_read = reader.execute(
            select(RouteUsage).where(
                RouteUsage.user_id == user_id, RouteUsage.request_id == request_id
            )
        ).scalar_one()
        assert usage_read.state == "CONSUMED"
        assert usage_read.response_json == {"distance_m": 1420.5}

        idem_read = reader.get(
            IdempotencyRecord, (user_id, "upload_confirm", "idem-orm-roundtrip-1")
        )
        assert idem_read is not None and idem_read.status_code == 202

        view_row = reader.execute(
            select(edge_difficulty_view).where(edge_difficulty_view.c.edge_id == edge_id)
        ).mappings().one()
        assert view_row["edge_id"] == edge_id
        assert float(view_row["avg_difficulty"]) == pytest.approx(3.0)
        assert int(view_row["report_count"]) == 1
        assert int(view_row["display_level"]) == 3
