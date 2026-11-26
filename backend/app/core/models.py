"""Production SQLAlchemy 2.0 + GeoAlchemy2 ORM models for OnTrack v2 (ARCHITECTURE.md §7).

All domain table models inherit from ``app.core.db.Base``. The ``AppConfig`` model is
defined in ``app.configuration.models`` and re-exported here so importing this module
registers all 16 domain tables on ``Base.metadata``. The ``edge_difficulty`` SQL view
is exposed as a Core ``Table`` selectable bound to a separate ``VIEW_METADATA`` instance
so it is never mistaken for a regular table during metadata inspection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    REAL,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.configuration.models import AppConfig
from app.core.db import Base


class GraphMetadata(Base):
    """Single canonical graph revision row (``id = 1``), updated last before commit."""

    __tablename__ = "graph_metadata"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )


class User(Base):
    """Registered user account with role and UI language preference."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "user_type IN ('regular', 'system', 'inactive')",
            name="user_type_valid",
        ),
        CheckConstraint(
            "preferred_language IN ('he', 'en')",
            name="preferred_language_valid",
        ),
        Index("uq_users_email_lower", text("LOWER(email)"), unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    user_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="regular",
        server_default=text("'regular'"),
    )
    preferred_language: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="he",
        server_default=text("'he'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class UserSession(Base):
    """Server-side opaque session record with revocation and rejection grace tracking."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_sessions_user_expires", "user_id", "expires_at"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Node(Base):
    """Trail graph vertex stored in WGS84 (EPSG:4326) with optional elevation."""

    __tablename__ = "nodes"
    __table_args__ = (
        Index("ix_nodes_geom_geog", text("(geom::geography)"), postgresql_using="gist"),
        Index("ix_nodes_geom", "geom", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    geom: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    ele: Mapped[float | None] = mapped_column(REAL, nullable=True)


class Edge(Base):
    """Undirected trail graph edge connecting two distinct nodes with 2-point geometry."""

    __tablename__ = "edges"
    __table_args__ = (
        CheckConstraint("length_m > 0", name="length_m_positive"),
        CheckConstraint("initial_difficulty BETWEEN 1 AND 3", name="initial_difficulty_range"),
        CheckConstraint("source <> target", name="no_self_loop"),
        Index("ix_edges_geom", "geom", postgresql_using="gist"),
        Index(
            "uq_edges_unordered_endpoints",
            text("LEAST(source, target)"),
            text("GREATEST(source, target)"),
            unique=True,
        ),
        Index("ix_edges_source", "source"),
        Index("ix_edges_target", "target"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[int] = mapped_column(BigInteger, ForeignKey("nodes.id"), nullable=False)
    target: Mapped[int] = mapped_column(BigInteger, ForeignKey("nodes.id"), nullable=False)
    geom: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
        nullable=False,
    )
    length_m: Mapped[float] = mapped_column(REAL, nullable=False)
    initial_difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class Track(Base):
    """Canonical deduplicated track geometry keyed by normalized_fingerprint."""

    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    normalized_geom: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=False),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Upload(Base):
    """User track submission lifecycle record with worker lease and fencing fields."""

    __tablename__ = "uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'DRAFT', 'PENDING', 'PROCESSING', 'RETRY', "
            "'TECHNICAL_FAILED', 'ACTIVE', 'REJECTED'"
            ")",
            name="status_valid",
        ),
        CheckConstraint(
            "overall_difficulty BETWEEN 1 AND 3",
            name="overall_difficulty_range",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint(
            "rejection_code IS NULL OR rejection_code IN ("
            "'DEGENERATE_GEOMETRY', 'OUTSIDE_SERVICE_AREA', 'QUALITY_RULE_FAILED'"
            ")",
            name="rejection_code_valid",
        ),
        Index("ix_uploads_status_lease", "status", "lease_expires_at"),
        Index(
            "uq_uploads_user_credit_fingerprint",
            "user_id",
            "normalized_fingerprint",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING', 'PROCESSING', 'RETRY', 'TECHNICAL_FAILED', 'ACTIVE')"
            ),
        ),
        Index("ix_uploads_user_created_at", "user_id", text("created_at DESC")),
        Index("ix_uploads_user_status", "user_id", "status"),
        Index(
            "ix_uploads_draft_expires",
            "draft_expires_at",
            postgresql_where=text("status = 'DRAFT'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    original_ref: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    overall_difficulty: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    ingestion_config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    active_attempt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    rejection_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tracks.id"), nullable=True)
    draft_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UploadSegment(Base):
    """Normalized preview/source segment geometry belonging to an upload."""

    __tablename__ = "upload_segments"
    __table_args__ = (
        CheckConstraint("segment_index >= 0", name="segment_index_non_negative"),
        UniqueConstraint("upload_id", "segment_index", name="uq_upload_segments_upload_index"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    upload_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("uploads.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    geom: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
        nullable=False,
    )


class BatchImport(Base):
    """Administrator-initiated bulk track import job."""

    __tablename__ = "batch_imports"
    __table_args__ = (
        CheckConstraint("source IN ('cloud', 'server')", name="source_valid"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')",
            name="status_valid",
        ),
        CheckConstraint("total_files >= 0", name="total_files_non_negative"),
        CheckConstraint("imported_count >= 0", name="imported_count_non_negative"),
        CheckConstraint("skipped_count >= 0", name="skipped_count_non_negative"),
        CheckConstraint("failed_count >= 0", name="failed_count_non_negative"),
        Index("ix_batch_imports_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    initiated_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    total_files: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    imported_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    failed_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BatchFile(Base):
    """Per-file durable work item inside a batch import with lease/fencing fields."""

    __tablename__ = "batch_files"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'RETRY', 'COMPLETED', 'SKIPPED', 'FAILED')",
            name="status_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        UniqueConstraint("batch_id", "source_ref", name="uq_batch_files_batch_source_ref"),
        Index("ix_batch_files_batch_status", "batch_id", "status"),
        Index("ix_batch_files_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("batch_imports.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    active_attempt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ingestion_config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tracks.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IngestionRun(Base):
    """Per-submission ingestion provenance record decoupling tracks from runs (ARCH-17)."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint("source_type IN ('upload', 'batch_file')", name="source_type_valid"),
        CheckConstraint("graph_revision_before >= 0", name="graph_revision_before_non_negative"),
        CheckConstraint(
            "(source_type = 'upload' AND upload_id IS NOT NULL AND batch_file_id IS NULL) "
            "OR (source_type = 'batch_file' AND batch_file_id IS NOT NULL AND upload_id IS NULL)",
            name="source_ref",
        ),
        Index(
            "uq_ingestion_runs_upload_id",
            "upload_id",
            unique=True,
            postgresql_where=text("upload_id IS NOT NULL"),
        ),
        Index(
            "uq_ingestion_runs_batch_file_id",
            "batch_file_id",
            unique=True,
            postgresql_where=text("batch_file_id IS NOT NULL"),
        ),
        Index("ix_ingestion_runs_track_id", "track_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    upload_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("uploads.id", ondelete="CASCADE"),
        nullable=True,
    )
    batch_file_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("batch_files.id", ondelete="CASCADE"),
        nullable=True,
    )
    track_id: Mapped[str] = mapped_column(Text, ForeignKey("tracks.id"), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    graph_revision_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EdgeContribution(Base):
    """Provenance link mapping each graph edge to contributing ingestion runs."""

    __tablename__ = "edge_contributions"
    __table_args__ = (
        Index("ix_edge_contributions_track_id", "track_id"),
        Index("ix_edge_contributions_ingestion_run_id", "ingestion_run_id"),
    )

    edge_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("edges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ingestion_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    track_id: Mapped[str] = mapped_column(Text, ForeignKey("tracks.id"), nullable=False)


class EdgeRating(Base):
    """Explicit community difficulty report (one editable report per user per edge)."""

    __tablename__ = "edge_ratings"
    __table_args__ = (
        CheckConstraint("difficulty BETWEEN 1 AND 3", name="difficulty_range"),
    )

    edge_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("edges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class RouteUsage(Base):
    """Free-allowance reservation, settlement, and idempotent route replay record."""

    __tablename__ = "route_usage"
    __table_args__ = (
        CheckConstraint(
            "state IN ('RESERVED', 'CONSUMED', 'RELEASED', 'EXPIRED', 'NON_ALLOWANCE')",
            name="state_valid",
        ),
        UniqueConstraint("user_id", "request_id", name="uq_route_usage_user_request"),
        Index("ix_route_usage_user_state_expires", "user_id", "state", "expires_at"),
        Index(
            "ix_route_usage_replay_expires_at",
            "replay_expires_at",
            postgresql_where=text("replay_expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    coord_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    replay_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyRecord(Base):
    """Scoped idempotency replay record with bounded key length and expiry."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("char_length(key) BETWEEN 1 AND 255", name="key_length"),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    operation: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Separate metadata for SQL views so Base.metadata only contains real tables.
VIEW_METADATA = MetaData()

edge_difficulty_view = Table(
    "edge_difficulty",
    VIEW_METADATA,
    Column("edge_id", BigInteger, primary_key=True),
    Column("avg_difficulty", Double, nullable=False),
    Column("report_count", BigInteger, nullable=False),
    Column("display_level", SmallInteger, nullable=False),
)


__all__ = [
    "AppConfig",
    "BatchFile",
    "BatchImport",
    "Edge",
    "EdgeContribution",
    "EdgeRating",
    "GraphMetadata",
    "IdempotencyRecord",
    "IngestionRun",
    "Node",
    "RouteUsage",
    "Track",
    "Upload",
    "UploadSegment",
    "User",
    "UserSession",
    "VIEW_METADATA",
    "edge_difficulty_view",
]
