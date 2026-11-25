"""T-003 POC 1: Node snapping, point reduction, parsing, and geometry quality tests.

Pure unit tests run without Docker (`pytest -m "not integration"`).
PostGIS / Alembic integration tests are marked with `@pytest.mark.integration`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from alembic import command
from pyproj import Geod
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.domain.geo.cli import (
    load_topology_expectations,
    parse_threshold_pairs,
    run_benchmark,
)
from app.domain.geo.cli import main as cli_main
from app.domain.geo.geodesy import (
    geodesic_distance_m,
    point_to_segment_distance_m,
    reduce_segment,
    reduce_track,
)
from app.domain.geo.metrics import (
    BENCHMARK_THRESHOLD_PAIRS,
    compute_deviation_metrics,
    compute_percentile,
)
from app.domain.geo.models import (
    ERROR_CODE_DEGENERATE_GEOMETRY,
    ERROR_CODE_EMPTY_FILE,
    ERROR_CODE_FILE_TOO_LARGE,
    ERROR_CODE_INVALID_COORDINATE,
    ERROR_CODE_MALFORMED_XML,
    ERROR_CODE_TOO_MANY_POINTS,
    ERROR_CODE_UNSAFE_XML,
    ERROR_CODE_UNSUPPORTED_FORMAT,
    GeoParseError,
    ParsedTrack,
    SnappingConfig,
    TrackPoint,
    TrackSegment,
)
from app.domain.geo.parser import (
    compute_file_sha256,
    compute_normalized_fingerprint,
    parse_gpx,
    parse_kml,
    parse_track_directory,
    parse_track_file,
)
from app.domain.geo.snapping import (
    GRAPH_WRITER_ADVISORY_LOCK_KEY,
    InMemoryGraph,
    build_normalized_multilinestring_wkt,
    ingest_track_in_memory,
    ingest_track_transaction,
)
from tests.conftest import build_alembic_config

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "geo"
FINGERPRINT_DIR = FIXTURES_DIR / "fingerprint"
INVALID_DIR = FIXTURES_DIR / "invalid"
LABELS_PATH = FIXTURES_DIR / "labels.json"
_GEOD = Geod(ellps="WGS84")


def _move_point(lat: float, lon: float, az_deg: float, dist_m: float) -> TrackPoint:
    lon2, lat2, _ = _GEOD.fwd(lon, lat, az_deg, dist_m)
    return TrackPoint(lat=float(lat2), lon=float(lon2), ele=100.0)


def _make_parsed_track(segments: list[list[TrackPoint]]) -> ParsedTrack:
    track_segs = tuple(
        TrackSegment(index=idx, points=tuple(pts)) for idx, pts in enumerate(segments)
    )
    fp = compute_normalized_fingerprint(track_segs)
    return ParsedTrack(
        source_format="gpx",
        segments=track_segs,
        file_sha256=compute_file_sha256(fp.encode("utf-8")),
        normalized_fingerprint=fp,
        raw_point_count=sum(len(s.points) for s in track_segs),
    )


# ---------------------------------------------------------------------------
# AC-44: GPX & KML parsing, KML lon,lat axis fix, endpoint retention, .twl skip
# ---------------------------------------------------------------------------


def test_kml_axis_order_israel_coordinates() -> None:
    """AC-44: KML lon,lat[,ele] coordinates parse as lat ~32.6, lon ~35.0 (not swapped)."""
    for filename in ("13_israel_kml_valid.kml", "14_israel_kml_gx_track.kml"):
        path = FIXTURES_DIR / filename
        track = parse_track_file(path.name, path.read_bytes())
        assert track.source_format == "kml"
        assert len(track.segments) == 1
        assert len(track.segments[0].points) == 10
        for pt in track.segments[0].points:
            assert 32.5 <= pt.lat <= 32.9, f"Expected Israel latitude, got {pt.lat}"
            assert 34.9 <= pt.lon <= 35.2, f"Expected Israel longitude, got {pt.lon}"
            assert pt.ele is not None


def test_batch_directory_skips_twl_and_corrupt_without_aborting(tmp_path: Path) -> None:
    """AC-44: Batch scan imports valid GPX & KML while skipping .twl and corrupt files."""
    shutil.copy(FIXTURES_DIR / "01_israel_base_forward.gpx", tmp_path / "valid.gpx")
    shutil.copy(FIXTURES_DIR / "13_israel_kml_valid.kml", tmp_path / "valid.kml")
    shutil.copy(INVALID_DIR / "legacy_unsupported.twl", tmp_path / "legacy.twl")
    shutil.copy(INVALID_DIR / "malformed.gpx", tmp_path / "corrupt.gpx")

    parsed, errors = parse_track_directory(tmp_path)

    parsed_names = {p.name: t for p, t in parsed}
    error_codes = {p.name: e.code for p, e in errors}

    assert set(parsed_names.keys()) == {"valid.gpx", "valid.kml"}
    assert error_codes == {
        "legacy.twl": ERROR_CODE_UNSUPPORTED_FORMAT,
        "corrupt.gpx": ERROR_CODE_MALFORMED_XML,
    }
    for track in parsed_names.values():
        first = track.segments[0].points[0]
        assert 32.5 <= first.lat <= 32.9
        assert 34.9 <= first.lon <= 35.2


@pytest.mark.parametrize("min_distance_m", [5.0, 10.0, 20.0, 30.0])
def test_reduction_preserves_first_and_last_endpoints(min_distance_m: float) -> None:
    """AC-44 & AC-65: Point reduction always retains exact first and last points per segment."""
    for filename in (
        "04_israel_sparse.gpx",
        "05_israel_dense.gpx",
        "06_israel_switchbacks.gpx",
        "12_israel_multisegment_2km_gap.gpx",
        "13_israel_kml_valid.kml",
    ):
        path = FIXTURES_DIR / filename
        track = parse_track_file(path.name, path.read_bytes())
        reduced = reduce_track(track, min_distance_m)

        assert len(reduced.segments) == len(track.segments)
        for orig_seg, red_seg in zip(track.segments, reduced.segments, strict=True):
            assert red_seg == reduce_segment(orig_seg, min_distance_m)
            assert red_seg.points[0] == orig_seg.points[0]
            assert red_seg.points[-1] == orig_seg.points[-1]
            assert 2 <= len(red_seg.points) <= len(orig_seg.points)


# ---------------------------------------------------------------------------
# AC-53: Reject corrupt, empty, XXE, billion-laughs, .txt, 1-pt, out-of-range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected_code"),
    [
        ("empty.gpx", ERROR_CODE_EMPTY_FILE),
        ("malformed.gpx", ERROR_CODE_MALFORMED_XML),
        ("xxe_attack.gpx", ERROR_CODE_UNSAFE_XML),
        ("billion_laughs.gpx", ERROR_CODE_UNSAFE_XML),
        ("unsupported.txt", ERROR_CODE_UNSUPPORTED_FORMAT),
        ("legacy_unsupported.twl", ERROR_CODE_UNSUPPORTED_FORMAT),
        ("single_point.gpx", ERROR_CODE_DEGENERATE_GEOMETRY),
        ("out_of_range_lat.gpx", ERROR_CODE_INVALID_COORDINATE),
    ],
)
def test_invalid_fixtures_rejected_with_structured_error(
    filename: str, expected_code: str
) -> None:
    """AC-53: Every invalid/unsafe fixture fails with the exact GeoParseError code."""
    path = INVALID_DIR / filename
    with pytest.raises(GeoParseError) as exc_info:
        parse_track_file(path.name, path.read_bytes())

    assert exc_info.value.code == expected_code
    assert exc_info.value.message.strip() != ""

    # Parser remains stateless and accepts a valid file immediately afterwards.
    valid_path = FIXTURES_DIR / "01_israel_base_forward.gpx"
    valid_track = parse_track_file(valid_path.name, valid_path.read_bytes())
    assert valid_track.raw_point_count == 12


def test_parse_limits_file_too_large_and_too_many_points() -> None:
    """AC-53: Configurable file size and max point count limits are enforced."""
    oversized = b"x" * (2 * 1024 * 1024)
    with pytest.raises(GeoParseError) as size_exc:
        parse_gpx(oversized, max_upload_mb=1)
    assert size_exc.value.code == ERROR_CODE_FILE_TOO_LARGE

    valid_bytes = (FIXTURES_DIR / "01_israel_base_forward.gpx").read_bytes()
    with pytest.raises(GeoParseError) as pts_exc:
        parse_gpx(valid_bytes, max_upload_points=5)
    assert pts_exc.value.code == ERROR_CODE_TOO_MANY_POINTS


def test_parse_gpx_two_identical_points_is_degenerate() -> None:
    """Two identical coordinates produce zero length and fail as DEGENERATE_GEOMETRY."""
    gpx = (
        b'<?xml version="1.0"?>'
        b'<gpx version="1.1"><trk><trkseg>'
        b'<trkpt lat="32.600000" lon="35.000000"/>'
        b'<trkpt lat="32.600000" lon="35.000000"/>'
        b"</trkseg></trk></gpx>"
    )
    with pytest.raises(GeoParseError) as exc_info:
        parse_gpx(gpx)
    assert exc_info.value.code == ERROR_CODE_DEGENERATE_GEOMETRY


def test_parse_gpx_no_elevation_and_non_finite_elevation_stored_as_none() -> None:
    """Missing or non-finite `<ele>` values safely become None."""
    no_ele_track = parse_gpx((FIXTURES_DIR / "11_israel_no_elevation.gpx").read_bytes())
    assert all(pt.ele is None for pt in no_ele_track.segments[0].points)

    nan_ele_gpx = (
        b'<?xml version="1.0"?>'
        b'<gpx version="1.1"><trk><trkseg>'
        b'<trkpt lat="32.600000" lon="35.000000"><ele>NaN</ele></trkpt>'
        b'<trkpt lat="32.601000" lon="35.001000"><ele>bad</ele></trkpt>'
        b"</trkseg></trk></gpx>"
    )
    parsed = parse_gpx(nan_ele_gpx)
    assert [pt.ele for pt in parsed.segments[0].points] == [None, None]


def test_parse_kml_rejects_non_kml_root_and_bad_coord() -> None:
    with pytest.raises(GeoParseError) as exc1:
        parse_kml(b"<gpx/>")
    assert exc1.value.code == ERROR_CODE_MALFORMED_XML

    bad_kml = (
        b'<kml><Document><Placemark><LineString>'
        b"<coordinates>35.0,95.0 35.1,32.6</coordinates>"
        b"</LineString></Placemark></Document></kml>"
    )
    with pytest.raises(GeoParseError) as exc2:
        parse_kml(bad_kml)
    assert exc2.value.code == ERROR_CODE_INVALID_COORDINATE


# ---------------------------------------------------------------------------
# AC-54: Multi-segment GPX gap preservation (no synthetic bridging edge)
# ---------------------------------------------------------------------------


def test_multisegment_2km_gap_preserved_without_bridging_edge() -> None:
    """AC-54: Two disconnected <trkseg> elements 2 km apart stay separate with 0 bridge edges."""
    path = FIXTURES_DIR / "12_israel_multisegment_2km_gap.gpx"
    track = parse_gpx(path.read_bytes())
    assert len(track.segments) == 2

    reduced = reduce_track(track, 20.0)
    assert len(reduced.input_segment_gaps_m) == 1
    assert 1950.0 <= reduced.input_segment_gaps_m[0] <= 2050.0

    graph = InMemoryGraph()
    res = ingest_track_in_memory(graph, track, SnappingConfig(20.0, 20.0))
    assert len(res.snapped_segments) == 2
    assert max(edge.length_m for edge in res.edges) < 100.0


def test_multiple_trk_elements_kept_separate_in_order() -> None:
    """Multiple <trk> elements inside a single GPX are preserved in order as separate segments."""
    gpx = (
        b'<?xml version="1.0"?>'
        b'<gpx version="1.1">'
        b'<trk><trkseg>'
        b'<trkpt lat="32.600000" lon="35.000000"/>'
        b'<trkpt lat="32.601000" lon="35.000000"/>'
        b"</trkseg></trk>"
        b'<trk><trkseg>'
        b'<trkpt lat="32.620000" lon="35.000000"/>'
        b'<trkpt lat="32.621000" lon="35.000000"/>'
        b"</trkseg></trk>"
        b"</gpx>"
    )
    track = parse_gpx(gpx)
    assert len(track.segments) == 2
    assert track.segments[0].index == 0
    assert track.segments[1].index == 1


# ---------------------------------------------------------------------------
# AC-94: Canonical 6-decimal normalized_fingerprint & raw file_sha256
# ---------------------------------------------------------------------------


def test_canonical_fingerprint_and_file_sha256_ac94() -> None:
    """AC-94: Byte-identical & metadata-only diffs share normalized_fingerprint; split differs."""
    t_a = parse_gpx((FINGERPRINT_DIR / "fingerprint_a.gpx").read_bytes())
    t_a2 = parse_gpx((FINGERPRINT_DIR / "fingerprint_a2_identical.gpx").read_bytes())
    t_a3 = parse_gpx((FINGERPRINT_DIR / "fingerprint_a3_metadata_diff.gpx").read_bytes())
    t_a4 = parse_gpx((FINGERPRINT_DIR / "fingerprint_a4_segment_split.gpx").read_bytes())

    # (a) Byte-identical
    assert t_a.file_sha256 == t_a2.file_sha256
    assert t_a.normalized_fingerprint == t_a2.normalized_fingerprint

    # (b) Metadata/elevation/namespace/whitespace modified, coords & boundaries identical
    assert t_a.file_sha256 != t_a3.file_sha256
    assert t_a.normalized_fingerprint == t_a3.normalized_fingerprint

    # (c) Different segment boundaries -> distinct fingerprint
    assert t_a.normalized_fingerprint != t_a4.normalized_fingerprint


def test_canonical_fingerprint_normalizes_negative_zero() -> None:
    """Normalized fingerprint collapses -0.0 and -0.0000001 to 0.000000."""
    seg_pos = [TrackSegment(0, (TrackPoint(0.0, 0.0), TrackPoint(1.0, 1.0)))]
    seg_neg = [TrackSegment(0, (TrackPoint(-0.0, -0.0000001), TrackPoint(1.0, 1.0)))]
    assert compute_normalized_fingerprint(seg_pos) == compute_normalized_fingerprint(seg_neg)


# ---------------------------------------------------------------------------
# Geodesy & Snapping Rules Unit Verification (AC-64)
# ---------------------------------------------------------------------------


def test_geodesic_and_point_to_segment_distances() -> None:
    a = TrackPoint(32.600000, 35.000000)
    b = _move_point(a.lat, a.lon, 90.0, 100.0)
    mid = _move_point(a.lat, a.lon, 90.0, 50.0)
    offset = _move_point(mid.lat, mid.lon, 0.0, 15.0)

    assert abs(geodesic_distance_m(a.lat, a.lon, b.lat, b.lon) - 100.0) < 0.01
    assert abs(point_to_segment_distance_m(offset, a, b) - 15.0) < 0.05
    assert point_to_segment_distance_m(a, a, b) == 0.0


def test_wkt_uses_lon_lat_order() -> None:
    seg = TrackSegment(0, (TrackPoint(32.6, 35.0), TrackPoint(32.61, 35.01)))
    wkt = build_normalized_multilinestring_wkt([seg])
    assert wkt == "MULTILINESTRING((35.0000000 32.6000000, 35.0100000 32.6100000))"


def test_in_memory_snapping_radius_ties_same_tx_reverse_and_self_loop() -> None:
    """AC-64 unit verification: radius boundary, tie-break, same-tx, reverse reuse, self-loop."""
    graph = InMemoryGraph()
    origin = TrackPoint(32.600000, 35.000000, 100.0)
    west = _move_point(origin.lat, origin.lon, 270.0, 10.0)
    east = _move_point(origin.lat, origin.lon, 90.0, 10.0)
    far = _move_point(origin.lat, origin.lon, 90.0, 100.0)

    # Seed two equidistant nodes: west (id=1) and east (id=2)
    west_id = graph.add_node(west)
    east_id = graph.add_node(east)
    assert west_id < east_id

    # Track starts at exact midpoint -> deterministic tie-break must pick lowest id (west_id)
    # Next point is 5m from west (reuses west_id -> self-loop skipped), then goes to far (new node),
    # then returns within 5m of far (same-tx visibility -> reuses far, self-loop skipped),
    # then returns to east (reuses east_id).
    near_west = _move_point(west.lat, west.lon, 0.0, 5.0)
    near_far = _move_point(far.lat, far.lon, 0.0, 5.0)
    track = _make_parsed_track([[origin, near_west, far, near_far, east]])

    res = ingest_track_in_memory(
        graph,
        track,
        SnappingConfig(point_simplification_distance_m=0.0, node_snap_radius_m=20.0),
        difficulty=2,
    )
    assert res.snapped_segments[0][0] == west_id
    assert res.self_loops_skipped == 2
    assert res.nodes_created == 1  # only `far` was new
    assert res.edges_created == 2
    assert res.graph_revision == 1

    # Reverse traversal reuses both edges without creating new ones or changing difficulty
    rev_track = _make_parsed_track([[east, far, origin]])
    rev_res = ingest_track_in_memory(
        graph,
        rev_track,
        SnappingConfig(point_simplification_distance_m=0.0, node_snap_radius_m=20.0),
        difficulty=3,
    )
    assert rev_res.nodes_created == 0
    assert rev_res.edges_created == 0
    assert rev_res.edges_reused == 2
    assert all(edge.initial_difficulty == 2 for edge in graph.edges.values())


def test_in_memory_degenerate_snapping_rolls_back_atomically() -> None:
    """When all points snap to one node, DEGENERATE_GEOMETRY rolls back nodes and revision."""
    graph = InMemoryGraph()
    p1 = TrackPoint(32.600000, 35.000000)
    p2 = _move_point(p1.lat, p1.lon, 90.0, 5.0)
    track = _make_parsed_track([[p1, p2]])

    with pytest.raises(GeoParseError) as exc_info:
        ingest_track_in_memory(
            graph,
            track,
            SnappingConfig(point_simplification_distance_m=0.0, node_snap_radius_m=20.0),
        )
    assert exc_info.value.code == ERROR_CODE_DEGENERATE_GEOMETRY
    assert len(graph.nodes) == 0
    assert len(graph.edges) == 0
    assert graph.revision == 0


# ---------------------------------------------------------------------------
# AC-65 & AC-96: Metrics, 5-Pair Synthetic Benchmark & CLI Runner
# ---------------------------------------------------------------------------


def test_metrics_helpers_and_all_five_threshold_pairs_synthetic_benchmark(
    tmp_path: Path,
) -> None:
    """AC-65 & AC-96: Benchmark all 5 (reduction, snap) pairs on synthetic Israel fixtures."""
    assert compute_percentile([10.0, 20.0, 30.0, 40.0], 50.0) == 25.0
    assert parse_threshold_pairs("5:5,10:10,20:20,20:10,30:20") == list(
        BENCHMARK_THRESHOLD_PAIRS
    )

    # Deviation metric sanity check
    orig = [TrackPoint(32.6, 35.0), _move_point(32.6, 35.0, 90.0, 50.0)]
    max_d, p95_d, mean_d, hausdorff = compute_deviation_metrics(orig, orig)
    assert max_d == 0.0 and p95_d == 0.0 and mean_d == 0.0 and hausdorff == 0.0

    expectations = load_topology_expectations(LABELS_PATH)
    report = run_benchmark(
        input_path=FIXTURES_DIR,
        pairs=BENCHMARK_THRESHOLD_PAIRS,
        in_memory=True,
        expectations=expectations,
    )

    assert report["tracks_loaded"] == 15
    assert report["tracks_skipped"] == 0
    assert len(report["results"]) == 5

    by_pair = {
        (row["simplification_m"], row["snap_radius_m"]): row for row in report["results"]
    }
    for pair in BENCHMARK_THRESHOLD_PAIRS:
        row = by_pair[pair]
        assert row["raw_point_count"] > 5000
        assert row["reduced_point_count"] <= row["raw_point_count"]
        assert row["final_node_count"] > 0
        assert row["final_edge_count"] > 0
        assert row["max_node_spacing_m"] >= row["p95_node_spacing_m"] >= row["p50_node_spacing_m"]
        assert row["max_deviation_m"] >= row["p95_deviation_m"] >= 0.0
        assert row["hausdorff_deviation_m"] >= row["max_deviation_m"]
        assert row["route_quality_ratio"] > 0.0
        assert row["p95_latency_ms"] < 30_000.0
        assert row["peak_rss_mb"] > 0.0

    # Parallel trails are 15m apart: snap=20m causes strictly more false joins than snap=5m
    assert by_pair[(20.0, 20.0)]["false_joins"] > by_pair[(5.0, 5.0)]["false_joins"]

    # CLI end-to-end invocation writing JSON output
    out_json = tmp_path / "snapping_report.json"
    exit_code = cli_main(
        [
            "--input",
            str(FIXTURES_DIR),
            "--in-memory",
            "--labels-json",
            str(LABELS_PATH),
            "--output-json",
            str(out_json),
        ]
    )
    assert exit_code == 0
    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(saved["results"]) == 5


# ---------------------------------------------------------------------------
# Integration Tests (Real PostGIS + Alembic 0001_poc_foundation.py)
# ---------------------------------------------------------------------------


def _prepare_clean_db(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE edges, nodes, tracks RESTART IDENTITY CASCADE"))
        conn.execute(text("UPDATE graph_metadata SET revision = 0 WHERE id = 1"))


@pytest.fixture(scope="module")
def migrated_engine(postgres_db_url: str) -> Engine:
    cfg = build_alembic_config(postgres_db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(postgres_db_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.mark.integration
def test_integration_postgis_snapping_radius_ties_same_tx_and_reverse_reuse(
    migrated_engine: Engine,
) -> None:
    """AC-64: Verify real PostGIS ST_DWithin radius, ties, same-tx visibility, and reverse reuse."""
    _prepare_clean_db(migrated_engine)

    origin = TrackPoint(32.600000, 35.000000, 200.0)
    west = _move_point(origin.lat, origin.lon, 270.0, 10.0)
    east = _move_point(origin.lat, origin.lon, 90.0, 10.0)
    outside = _move_point(origin.lat, origin.lon, 90.0, 120.0)
    near_outside = _move_point(outside.lat, outside.lon, 0.0, 4.0)

    # Seed equidistant west (lower id) and east (higher id)
    with migrated_engine.begin() as conn:
        w_id = conn.execute(
            text(
                "INSERT INTO nodes (geom, ele) VALUES "
                "(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 100.0) RETURNING id"
            ),
            {"lon": west.lon, "lat": west.lat},
        ).scalar_one()
        e_id = conn.execute(
            text(
                "INSERT INTO nodes (geom, ele) VALUES "
                "(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 100.0) RETURNING id"
            ),
            {"lon": east.lon, "lat": east.lat},
        ).scalar_one()
        assert int(w_id) < int(e_id)

    # Ingest track hitting tie-break midpoint, same-tx node revisit, and self-loop skip
    track = _make_parsed_track([[origin, outside, near_outside, east]])
    with migrated_engine.begin() as conn:
        res = ingest_track_transaction(
            conn,
            track,
            SnappingConfig(point_simplification_distance_m=0.0, node_snap_radius_m=20.0),
            difficulty=2,
        )

    assert res.snapped_segments[0][0] == int(w_id)
    assert res.nodes_created == 1
    assert res.self_loops_skipped == 1
    assert res.edges_created == 2
    assert res.graph_revision == 1

    # Reverse traversal reuses edges without mutating geom, length_m, or initial_difficulty
    rev_track = _make_parsed_track([[east, outside, origin]])
    with migrated_engine.begin() as conn:
        rev_res = ingest_track_transaction(
            conn,
            rev_track,
            SnappingConfig(point_simplification_distance_m=0.0, node_snap_radius_m=20.0),
            difficulty=3,
        )

    assert rev_res.nodes_created == 0
    assert rev_res.edges_created == 0
    assert rev_res.edges_reused == 2
    assert rev_res.graph_revision == 2

    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    initial_difficulty,
                    ST_NPoints(geom) AS npts,
                    ABS(length_m - ST_Length(geom::geography)) AS len_diff,
                    (source = target) AS is_self_loop
                FROM edges
                """
            )
        ).fetchall()
        assert len(rows) == 2
        for diff, npts, len_diff, is_self_loop in rows:
            assert int(diff) == 2
            assert int(npts) == 2
            assert float(len_diff) < 0.01
            assert not bool(is_self_loop)

    # Verify no-elevation track inserts NULL ele
    no_ele = parse_gpx((FIXTURES_DIR / "11_israel_no_elevation.gpx").read_bytes())
    with migrated_engine.begin() as conn:
        ingest_track_transaction(conn, no_ele, SnappingConfig(20.0, 20.0))
        null_ele_count = conn.execute(
            text("SELECT COUNT(*) FROM nodes WHERE ele IS NULL")
        ).scalar_one()
        assert int(null_ele_count) >= 2


@pytest.mark.integration
def test_integration_postgis_multisegment_gap_and_degenerate_rollback(
    migrated_engine: Engine,
) -> None:
    """AC-54 & DEGENERATE_GEOMETRY: Multi-segment gap preserved; degenerate ingestion rolls back."""
    _prepare_clean_db(migrated_engine)

    multi = parse_gpx((FIXTURES_DIR / "12_israel_multisegment_2km_gap.gpx").read_bytes())
    with migrated_engine.begin() as conn:
        res = ingest_track_transaction(conn, multi, SnappingConfig(20.0, 20.0))
        assert len(res.snapped_segments) == 2
        max_len = conn.execute(text("SELECT MAX(length_m) FROM edges")).scalar_one()
        num_geoms = conn.execute(
            text("SELECT ST_NumGeometries(normalized_geom) FROM tracks WHERE id = :id"),
            {"id": multi.normalized_fingerprint},
        ).scalar_one()
        assert float(max_len) < 500.0
        assert int(num_geoms) == 2

    # Degenerate track collapsing into single node rolls back savepoint cleanly
    p1 = TrackPoint(32.655000, 35.055000)
    p2 = _move_point(p1.lat, p1.lon, 90.0, 5.0)
    degenerate = _make_parsed_track([[p1, p2]])

    with migrated_engine.begin() as conn:
        nodes_before = int(conn.execute(text("SELECT COUNT(*) FROM nodes")).scalar_one())
        rev_before = int(
            conn.execute(text("SELECT revision FROM graph_metadata WHERE id = 1")).scalar_one()
        )
        with pytest.raises(GeoParseError) as exc_info:
            ingest_track_transaction(conn, degenerate, SnappingConfig(0.0, 20.0))
        assert exc_info.value.code == ERROR_CODE_DEGENERATE_GEOMETRY

        nodes_after = int(conn.execute(text("SELECT COUNT(*) FROM nodes")).scalar_one())
        rev_after = int(
            conn.execute(text("SELECT revision FROM graph_metadata WHERE id = 1")).scalar_one()
        )
        assert nodes_after == nodes_before
        assert rev_after == rev_before


@pytest.mark.integration
def test_integration_postgis_advisory_lock_blocks_concurrent_writer(
    migrated_engine: Engine,
) -> None:
    """Verify GRAPH_WRITER_ADVISORY_LOCK_KEY serializes graph writers across connections."""
    _prepare_clean_db(migrated_engine)
    track = parse_gpx((FIXTURES_DIR / "01_israel_base_forward.gpx").read_bytes())

    with migrated_engine.begin() as conn1:
        ingest_track_transaction(conn1, track, SnappingConfig(20.0, 20.0))
        # While conn1 transaction is still open, conn2 cannot acquire the same advisory lock
        with migrated_engine.connect() as conn2:
            acquired = conn2.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": GRAPH_WRITER_ADVISORY_LOCK_KEY},
            ).scalar_one()
            assert acquired is False

    # After conn1 commits, conn2 acquires the lock cleanly
    with migrated_engine.begin() as conn2:
        acquired_after = conn2.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": GRAPH_WRITER_ADVISORY_LOCK_KEY},
        ).scalar_one()
        assert acquired_after is True


@pytest.mark.integration
def test_integration_postgis_benchmark_five_pairs_and_5000pt_stress(
    migrated_engine: Engine,
    postgres_db_url: str,
) -> None:
    """AC-65 & AC-96: Run all 5 threshold pairs + 5,000-pt stress track on real PostGIS."""
    expectations = load_topology_expectations(LABELS_PATH)
    report = run_benchmark(
        input_path=FIXTURES_DIR,
        pairs=BENCHMARK_THRESHOLD_PAIRS,
        database_url=postgres_db_url,
        reset_db=True,
        expectations=expectations,
    )

    assert report["tracks_loaded"] == 15
    assert report["tracks_skipped"] == 0
    assert len(report["results"]) == 5

    for row in report["results"]:
        assert row["final_node_count"] > 0
        assert row["final_edge_count"] > 0
        assert row["p95_latency_ms"] <= 30_000.0
        assert row["db_queries_count"] > 0
