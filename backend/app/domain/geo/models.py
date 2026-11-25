"""Domain models and error types for geographic track ingestion and snapping."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

TrackSourceFormat = Literal["gpx", "kml"]

ERROR_CODE_EMPTY_FILE = "EMPTY_FILE"
ERROR_CODE_UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
ERROR_CODE_MALFORMED_XML = "MALFORMED_XML"
ERROR_CODE_UNSAFE_XML = "UNSAFE_XML"
ERROR_CODE_INVALID_COORDINATE = "INVALID_COORDINATE"
ERROR_CODE_DEGENERATE_GEOMETRY = "DEGENERATE_GEOMETRY"
ERROR_CODE_FILE_TOO_LARGE = "FILE_TOO_LARGE"
ERROR_CODE_TOO_MANY_POINTS = "TOO_MANY_POINTS"


class GeoParseError(ValueError):
    """Safe, structured error raised for invalid or unusable track inputs."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def __repr__(self) -> str:
        return f"GeoParseError(code={self.code!r}, message={self.message!r})"


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """Single WGS84 geographic point with optional finite elevation in meters."""

    lat: float
    lon: float
    ele: float | None = None


@dataclass(frozen=True, slots=True)
class TrackSegment:
    """Ordered sequence of points representing one unbroken source polyline segment."""

    index: int
    points: tuple[TrackPoint, ...]


@dataclass(frozen=True, slots=True)
class ParsedTrack:
    """Validated track parsed from GPX or KML with raw and canonical identities."""

    source_format: TrackSourceFormat
    segments: tuple[TrackSegment, ...]
    file_sha256: str
    normalized_fingerprint: str
    raw_point_count: int


@dataclass(frozen=True, slots=True)
class SnappingConfig:
    """Configuration controlling track limits, point reduction, and node snapping."""

    point_simplification_distance_m: float = 20.0
    node_snap_radius_m: float = 20.0
    default_difficulty: int = 1
    max_upload_mb: int = 10
    max_upload_points: int = 50_000

    def __post_init__(self) -> None:
        if self.point_simplification_distance_m < 0:
            raise ValueError("point_simplification_distance_m must be non-negative.")
        if self.node_snap_radius_m < 0:
            raise ValueError("node_snap_radius_m must be non-negative.")
        if self.default_difficulty not in (1, 2, 3):
            raise ValueError("default_difficulty must be 1, 2, or 3.")
        if self.max_upload_mb <= 0:
            raise ValueError("max_upload_mb must be positive.")
        if self.max_upload_points <= 0:
            raise ValueError("max_upload_points must be positive.")


@dataclass(frozen=True, slots=True)
class ReducedTrack:
    """Track after per-segment geodesic point reduction preserving endpoints and gaps."""

    source_format: TrackSourceFormat
    segments: tuple[TrackSegment, ...]
    file_sha256: str
    normalized_fingerprint: str
    raw_point_count: int
    reduced_point_count: int
    min_distance_m: float
    input_segment_gaps_m: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class SnappedEdgeRef:
    """Reference to an undirected graph edge created or reused during ingestion."""

    edge_id: int
    source_node_id: int
    target_node_id: int
    length_m: float
    created: bool


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Summary of one atomic track ingestion transaction."""

    track_id: str
    graph_revision: int
    raw_point_count: int
    reduced_point_count: int
    nodes_created: int
    nodes_reused: int
    edges_created: int
    edges_reused: int
    self_loops_skipped: int
    zero_length_edges_skipped: int
    snapped_segments: tuple[tuple[int, ...], ...]
    edges: tuple[SnappedEdgeRef, ...]
    elapsed_ms: float
    db_queries_count: int


@dataclass(frozen=True, slots=True)
class GeometryQualityMetrics:
    """Aggregated AC-65 / AC-96 geometry, topology, and performance metrics."""

    simplification_m: float
    snap_radius_m: float
    raw_point_count: int
    reduced_point_count: int
    final_node_count: int
    final_edge_count: int
    nodes_created: int
    nodes_reused: int
    edges_created: int
    edges_reused: int
    self_loops_skipped: int
    max_node_spacing_m: float
    p50_node_spacing_m: float
    p95_node_spacing_m: float
    max_deviation_m: float
    p95_deviation_m: float
    mean_deviation_m: float
    hausdorff_deviation_m: float
    false_joins: int
    missed_joins: int
    redundant_edges: int
    route_quality_ratio: float
    p50_latency_ms: float
    p95_latency_ms: float
    peak_rss_mb: float
    db_queries_count: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize metrics to a JSON-compatible dictionary."""
        return asdict(self)
