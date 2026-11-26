"""Unit tests for T-008 production ORM metadata, FR-022 defaults, and Alembic chain."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from geoalchemy2 import Geometry
from procrastinate.schema import SchemaManager
from sqlalchemy import CheckConstraint, Table, UniqueConstraint, select

from app.configuration import (
    CANONICAL_APP_CONFIG_KEYS,
    CONFIG_KEY_FREE_ROUTE_CREATIONS,
    CONFIG_KEY_MIN_REQUIRED_TRACKS,
    CONFIG_KEY_NODE_SNAP_RADIUS_M,
    CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M,
    CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS,
    DEFAULT_APP_CONFIG,
    AppConfig,
)
from app.core import models as core_models
from app.core.config import Settings
from app.core.db import Base
from app.core.models import (
    VIEW_METADATA,
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

BACKEND_DIR = Path(__file__).resolve().parents[2]
MIGRATION_0002_PATH = BACKEND_DIR / "alembic" / "versions" / "0002_production_schema.py"

EXPECTED_DOMAIN_TABLES: frozenset[str] = frozenset(
    {
        "graph_metadata",
        "users",
        "sessions",
        "app_config",
        "nodes",
        "edges",
        "tracks",
        "uploads",
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


def _check_constraint_map(table_name: str) -> dict[str, str]:
    table = Base.metadata.tables[table_name]
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _unique_constraint_columns(table_name: str) -> dict[str, tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {
        str(constraint.name): tuple(col.name for col in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _index_map(table_name: str) -> dict[str, Any]:
    table = Base.metadata.tables[table_name]
    return {str(index.name): index for index in table.indexes}


def _fk_targets(table_name: str) -> dict[str, tuple[str, str | None]]:
    table = Base.metadata.tables[table_name]
    result: dict[str, tuple[str, str | None]] = {}
    for fk in table.foreign_keys:
        result[fk.parent.name] = (fk.target_fullname, fk.ondelete)
    return result


def test_base_metadata_contains_all_16_production_domain_tables_and_excludes_views() -> None:
    assert core_models.AppConfig is AppConfig
    assert frozenset(Base.metadata.tables.keys()) == EXPECTED_DOMAIN_TABLES
    assert "edge_difficulty" not in Base.metadata.tables
    assert "edge_difficulty" in VIEW_METADATA.tables

    # Every exported ORM class binds to the shared Base.metadata
    model_classes = (
        GraphMetadata,
        User,
        UserSession,
        AppConfig,
        Node,
        Edge,
        Track,
        Upload,
        UploadSegment,
        BatchImport,
        BatchFile,
        IngestionRun,
        EdgeContribution,
        EdgeRating,
        RouteUsage,
        IdempotencyRecord,
    )
    assert len(model_classes) == 16
    for cls in model_classes:
        assert isinstance(cls.__table__, Table)
        assert cls.__table__.metadata is Base.metadata


def test_app_config_canonical_fr022_keys_and_confirmed_defaults() -> None:
    expected_keys = frozenset(
        {
            "MIN_REQUIRED_TRACKS",
            "FREE_ROUTE_CREATIONS",
            "REJECTED_TRACK_GRACE_PERIOD_HOURS",
            "POINT_SIMPLIFICATION_DISTANCE_M",
            "NODE_SNAP_RADIUS_M",
        }
    )
    assert expected_keys == CANONICAL_APP_CONFIG_KEYS
    assert frozenset(DEFAULT_APP_CONFIG.keys()) == expected_keys

    # Gate 1 (PRD §10.1) & T-007 synthetic POC-locked defaults
    assert DEFAULT_APP_CONFIG[CONFIG_KEY_MIN_REQUIRED_TRACKS] == 1
    assert DEFAULT_APP_CONFIG[CONFIG_KEY_FREE_ROUTE_CREATIONS] == 0
    assert DEFAULT_APP_CONFIG[CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS] == 24
    assert DEFAULT_APP_CONFIG[CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M] == 20.0
    assert DEFAULT_APP_CONFIG[CONFIG_KEY_NODE_SNAP_RADIUS_M] == 20.0

    # Verify alignment with Settings T-007 synthetic lock defaults
    settings = Settings(_env_file=None)
    assert (
        DEFAULT_APP_CONFIG[CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M]
        == settings.default_point_simplification_distance_m
    )
    assert DEFAULT_APP_CONFIG[CONFIG_KEY_NODE_SNAP_RADIUS_M] == settings.default_node_snap_radius_m

    # DEFAULT_APP_CONFIG must be immutable
    with pytest.raises(TypeError):
        DEFAULT_APP_CONFIG[CONFIG_KEY_FREE_ROUTE_CREATIONS] = 5  # type: ignore[index]


def test_users_sessions_and_app_config_schema_constraints_and_indexes() -> None:
    # users
    users_tbl = Base.metadata.tables["users"]
    assert users_tbl.c.username.unique is True
    assert users_tbl.c.user_type.nullable is False
    assert users_tbl.c.preferred_language.nullable is False
    users_checks = _check_constraint_map("users")
    assert "ck_users_user_type_valid" in users_checks
    assert "regular" in users_checks["ck_users_user_type_valid"]
    assert "system" in users_checks["ck_users_user_type_valid"]
    assert "inactive" in users_checks["ck_users_user_type_valid"]
    assert "ck_users_preferred_language_valid" in users_checks
    assert "he" in users_checks["ck_users_preferred_language_valid"]
    assert "en" in users_checks["ck_users_preferred_language_valid"]

    users_indexes = _index_map("users")
    assert "uq_users_email_lower" in users_indexes
    assert users_indexes["uq_users_email_lower"].unique is True

    # sessions
    sessions_tbl = Base.metadata.tables["sessions"]
    assert [col.name for col in sessions_tbl.primary_key.columns] == ["token_hash"]
    assert sessions_tbl.c.session_id.unique is True
    assert sessions_tbl.c.session_id.nullable is False
    assert _fk_targets("sessions")["user_id"] == ("users.id", "CASCADE")
    assert "ck_sessions_expires_after_created" in _check_constraint_map("sessions")
    sessions_indexes = _index_map("sessions")
    assert {"ix_sessions_user_expires", "ix_sessions_expires_at"}.issubset(sessions_indexes)

    # app_config
    config_tbl = Base.metadata.tables["app_config"]
    assert [col.name for col in config_tbl.primary_key.columns] == ["key"]
    assert config_tbl.c.value.nullable is False
    assert config_tbl.c.updated_by.nullable is True
    assert _fk_targets("app_config")["updated_by"] == ("users.id", "SET NULL")


def test_geospatial_graph_models_and_edge_difficulty_view() -> None:
    # graph_metadata
    assert "ck_graph_metadata_singleton" in _check_constraint_map("graph_metadata")

    # nodes
    nodes_tbl = Base.metadata.tables["nodes"]
    assert isinstance(nodes_tbl.c.geom.type, Geometry)
    assert nodes_tbl.c.geom.type.geometry_type == "POINT"
    assert nodes_tbl.c.geom.type.srid == 4326
    assert nodes_tbl.c.ele.nullable is True
    nodes_indexes = _index_map("nodes")
    assert {"ix_nodes_geom_geog", "ix_nodes_geom"} == set(nodes_indexes)
    assert nodes_indexes["ix_nodes_geom_geog"].dialect_options["postgresql"]["using"] == "gist"
    assert nodes_indexes["ix_nodes_geom"].dialect_options["postgresql"]["using"] == "gist"

    # edges
    edges_tbl = Base.metadata.tables["edges"]
    assert isinstance(edges_tbl.c.geom.type, Geometry)
    assert edges_tbl.c.geom.type.geometry_type == "LINESTRING"
    assert edges_tbl.c.geom.type.srid == 4326
    edges_checks = _check_constraint_map("edges")
    assert {
        "ck_edges_length_m_positive",
        "ck_edges_initial_difficulty_range",
        "ck_edges_no_self_loop",
    }.issubset(edges_checks)
    edges_indexes = _index_map("edges")
    assert {
        "ix_edges_geom",
        "uq_edges_unordered_endpoints",
        "ix_edges_source",
        "ix_edges_target",
    } == set(edges_indexes)
    assert edges_indexes["uq_edges_unordered_endpoints"].unique is True

    # tracks & upload_segments
    tracks_tbl = Base.metadata.tables["tracks"]
    assert isinstance(tracks_tbl.c.normalized_geom.type, Geometry)
    assert tracks_tbl.c.normalized_geom.type.geometry_type == "MULTILINESTRING"
    assert tracks_tbl.c.normalized_geom.type.srid == 4326

    upload_segments_tbl = Base.metadata.tables["upload_segments"]
    assert isinstance(upload_segments_tbl.c.geom.type, Geometry)
    assert upload_segments_tbl.c.geom.type.geometry_type == "LINESTRING"
    assert upload_segments_tbl.c.geom.type.srid == 4326
    assert _fk_targets("upload_segments")["upload_id"] == ("uploads.id", "CASCADE")
    assert _unique_constraint_columns("upload_segments")["uq_upload_segments_upload_index"] == (
        "upload_id",
        "segment_index",
    )

    # edge_difficulty Core Table selectable
    assert edge_difficulty_view.name == "edge_difficulty"
    assert list(edge_difficulty_view.c.keys()) == [
        "edge_id",
        "avg_difficulty",
        "report_count",
        "display_level",
    ]
    compiled_sql = str(select(edge_difficulty_view))
    assert "FROM edge_difficulty" in compiled_sql


def test_uploads_ingestion_runs_and_provenance_constraints() -> None:
    # uploads: user_id strictly NOT NULL, partial unique credit fingerprint, draft expiry index
    uploads_tbl = Base.metadata.tables["uploads"]
    assert uploads_tbl.c.user_id.nullable is False
    assert _fk_targets("uploads")["user_id"] == ("users.id", None)
    assert _fk_targets("uploads")["track_id"] == ("tracks.id", None)
    uploads_checks = _check_constraint_map("uploads")
    assert {
        "ck_uploads_status_valid",
        "ck_uploads_overall_difficulty_range",
        "ck_uploads_attempt_count_non_negative",
        "ck_uploads_rejection_code_valid",
    }.issubset(uploads_checks)
    uploads_indexes = _index_map("uploads")
    assert {
        "ix_uploads_status_lease",
        "uq_uploads_user_credit_fingerprint",
        "ix_uploads_user_created_at",
        "ix_uploads_user_status",
        "ix_uploads_draft_expires",
    } == set(uploads_indexes)
    credit_idx = uploads_indexes["uq_uploads_user_credit_fingerprint"]
    assert credit_idx.unique is True
    where_clause = str(credit_idx.dialect_options["postgresql"]["where"])
    for status in ("PENDING", "PROCESSING", "RETRY", "TECHNICAL_FAILED", "ACTIVE"):
        assert status in where_clause
    assert "DRAFT" not in where_clause
    assert "REJECTED" not in where_clause

    # ingestion_runs: XOR check + partial uniques
    runs_checks = _check_constraint_map("ingestion_runs")
    assert "ck_ingestion_runs_source_ref" in runs_checks
    assert "ck_ingestion_runs_source_type_valid" in runs_checks
    assert "ck_ingestion_runs_graph_revision_before_non_negative" in runs_checks
    runs_indexes = _index_map("ingestion_runs")
    assert runs_indexes["uq_ingestion_runs_upload_id"].unique is True
    assert runs_indexes["uq_ingestion_runs_batch_file_id"].unique is True
    assert "ix_ingestion_runs_track_id" in runs_indexes
    assert _fk_targets("ingestion_runs")["upload_id"] == ("uploads.id", "CASCADE")
    assert _fk_targets("ingestion_runs")["batch_file_id"] == ("batch_files.id", "CASCADE")
    assert _fk_targets("ingestion_runs")["track_id"] == ("tracks.id", None)

    # edge_contributions & edge_ratings composite primary keys
    contrib_tbl = Base.metadata.tables["edge_contributions"]
    assert [col.name for col in contrib_tbl.primary_key.columns] == ["edge_id", "ingestion_run_id"]
    assert {"ix_edge_contributions_track_id", "ix_edge_contributions_ingestion_run_id"} == set(
        _index_map("edge_contributions")
    )

    ratings_tbl = Base.metadata.tables["edge_ratings"]
    assert [col.name for col in ratings_tbl.primary_key.columns] == ["edge_id", "user_id"]
    assert "ck_edge_ratings_difficulty_range" in _check_constraint_map("edge_ratings")
    assert _fk_targets("edge_ratings")["edge_id"] == ("edges.id", "CASCADE")
    assert _fk_targets("edge_ratings")["user_id"] == ("users.id", "CASCADE")


def test_route_usage_idempotency_and_batch_import_constraints() -> None:
    # route_usage
    assert _unique_constraint_columns("route_usage")["uq_route_usage_user_request"] == (
        "user_id",
        "request_id",
    )
    assert "ck_route_usage_state_valid" in _check_constraint_map("route_usage")
    route_indexes = _index_map("route_usage")
    assert {"ix_route_usage_user_state_expires", "ix_route_usage_replay_expires_at"} == set(
        route_indexes
    )

    # idempotency_records (QA_REPORT §4 carry-forward: key length 1..255 + expires_at)
    idem_tbl = Base.metadata.tables["idempotency_records"]
    assert [col.name for col in idem_tbl.primary_key.columns] == ["user_id", "operation", "key"]
    assert idem_tbl.c.expires_at.nullable is False
    idem_checks = _check_constraint_map("idempotency_records")
    assert "ck_idempotency_records_key_length" in idem_checks
    assert "char_length(key) BETWEEN 1 AND 255" in idem_checks["ck_idempotency_records_key_length"]
    assert "ix_idempotency_records_expires_at" in _index_map("idempotency_records")

    # batch_imports & batch_files
    batch_import_checks = _check_constraint_map("batch_imports")
    assert {
        "ck_batch_imports_source_valid",
        "ck_batch_imports_status_valid",
        "ck_batch_imports_total_files_non_negative",
        "ck_batch_imports_imported_count_non_negative",
        "ck_batch_imports_skipped_count_non_negative",
        "ck_batch_imports_failed_count_non_negative",
    }.issubset(batch_import_checks)
    assert "ix_batch_imports_status" in _index_map("batch_imports")

    assert _unique_constraint_columns("batch_files")["uq_batch_files_batch_source_ref"] == (
        "batch_id",
        "source_ref",
    )
    assert {"ix_batch_files_batch_status", "ix_batch_files_status_lease"} == set(
        _index_map("batch_files")
    )


def test_alembic_revision_chain_and_0002_procrastinate_coverage() -> None:
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    script = ScriptDirectory.from_config(alembic_cfg)

    heads = script.get_heads()
    assert heads == ["0002_production_schema"]

    revisions = list(script.walk_revisions(base="base", head="heads"))
    assert [rev.revision for rev in revisions] == [
        "0002_production_schema",
        "0001_poc_foundation",
    ]
    assert revisions[0].down_revision == "0001_poc_foundation"
    assert revisions[1].down_revision is None

    # Inspect 0002 migration module to verify every Procrastinate 3 function/table/type is dropped
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION_0002_PATH)
    assert spec is not None and spec.loader is not None
    migration_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration_mod)

    procrastinate_sql = SchemaManager.get_schema()
    schema_functions = {
        line.split("CREATE FUNCTION ", 1)[1].split("(", 1)[0].strip()
        for line in procrastinate_sql.splitlines()
        if line.startswith("CREATE FUNCTION ")
    }
    schema_tables = {
        line.split("CREATE TABLE ", 1)[1].split("(", 1)[0].strip()
        for line in procrastinate_sql.splitlines()
        if line.startswith("CREATE TABLE ")
    }
    schema_types = {
        line.split("CREATE TYPE ", 1)[1].split(" AS", 1)[0].strip()
        for line in procrastinate_sql.splitlines()
        if line.startswith("CREATE TYPE ")
    }

    assert set(migration_mod._PROCRASTINATE_FUNCTIONS) == schema_functions
    assert set(migration_mod._PROCRASTINATE_TABLES) == schema_tables
    assert set(migration_mod._PROCRASTINATE_TYPES) == schema_types

    # Verify all 5 canonical FR-022 keys appear in the 0002 migration seed SQL
    source_text = MIGRATION_0002_PATH.read_text(encoding="utf-8")
    for key in CANONICAL_APP_CONFIG_KEYS:
        assert f"'{key}'" in source_text
    assert "exec_driver_sql" in source_text
    assert '.replace("%", "%%")' in source_text

    # Regression check: doubled '%' round-trips cleanly through psycopg v3's query parser
    # when SQLAlchemy's exec_driver_sql supplies an empty parameter mapping.
    from psycopg._queries import _split_query

    escaped_sql = procrastinate_sql.replace("%", "%%")
    parts = _split_query(escaped_sql.encode("utf-8"), "utf-8")
    assert len(parts) == 1
    assert b"".join(part.pre for part in parts).decode("utf-8") == procrastinate_sql
