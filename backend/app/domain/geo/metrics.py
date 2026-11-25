"""Geometry quality, spacing, deviation, and topology error metrics (AC-65, AC-96)."""

from __future__ import annotations

import math
import resource
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from app.domain.geo.geodesy import (
    geodesic_distance_m,
    point_to_segment_distance_m,
    segment_length_m,
)
from app.domain.geo.models import (
    GeometryQualityMetrics,
    IngestionResult,
    ParsedTrack,
    TrackPoint,
)

BENCHMARK_THRESHOLD_PAIRS: tuple[tuple[float, float], ...] = (
    (5.0, 5.0),
    (10.0, 10.0),
    (20.0, 20.0),
    (20.0, 10.0),
    (30.0, 20.0),
)


@dataclass(frozen=True, slots=True)
class TopologyExpectation:
    """Labeled topology ground-truth for evaluating false joins, missed joins, and redundancy."""

    expected_shared_node_pairs: tuple[tuple[str, str], ...] = ()
    forbidden_shared_node_pairs: tuple[tuple[str, str], ...] = ()
    expected_edge_reuse_tracks: tuple[str, ...] = ()


def compute_percentile(values: Sequence[float], percentile: float) -> float:
    """Compute linear-interpolated percentile (0..100) for a sequence of floats."""
    if not values:
        return 0.0
    if not (0.0 <= percentile <= 100.0):
        raise ValueError("percentile must be between 0 and 100.")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (percentile / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    weight = rank - lo
    return float(ordered[lo] * (1.0 - weight) + ordered[hi] * weight)


def get_peak_rss_mb() -> float:
    """Return current process peak RSS in MiB (normalized across macOS and Linux)."""
    ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return float(ru_maxrss) / (1024.0 * 1024.0)
    return float(ru_maxrss) / 1024.0


def compute_spacing_metrics(spacings_m: Sequence[float]) -> tuple[float, float, float]:
    """Return `(max_m, p50_m, p95_m)` consecutive snapped node spacing in meters."""
    if not spacings_m:
        return (0.0, 0.0, 0.0)
    return (
        float(max(spacings_m)),
        compute_percentile(spacings_m, 50.0),
        compute_percentile(spacings_m, 95.0),
    )


def _min_distance_to_segments_m(
    pt: TrackPoint,
    segments: Sequence[tuple[TrackPoint, TrackPoint]],
    tree: STRtree,
    geoms: Sequence[LineString],
) -> float:
    if not segments:
        return 0.0
    query_pt = Point(pt.lon, pt.lat)
    nearest_idx = int(tree.nearest(query_pt))
    deg_dist = float(query_pt.distance(geoms[nearest_idx]))
    # Account for longitudinal foreshortening (~1/cos(lat)) when querying degree space
    search_radius_deg = deg_dist * 2.0 + 1e-6
    candidate_idxs = tree.query(query_pt.buffer(search_radius_deg))
    best = math.inf
    seen_nearest = False
    for raw_idx in candidate_idxs:
        idx = int(raw_idx)
        if idx == nearest_idx:
            seen_nearest = True
        a, b = segments[idx]
        d = point_to_segment_distance_m(pt, a, b)
        if d < best:
            best = d
    if not seen_nearest:
        a, b = segments[nearest_idx]
        d = point_to_segment_distance_m(pt, a, b)
        if d < best:
            best = d
    return 0.0 if not math.isfinite(best) else best


def _compute_deviation_details(
    original_points: Sequence[TrackPoint],
    snapped_points: Sequence[TrackPoint],
) -> tuple[list[float], float]:
    """Return `(orig_to_snapped_distances_m, hausdorff_m)` without redundant tree builds."""
    if not original_points or not snapped_points:
        return ([], 0.0)

    snapped_segs: list[tuple[TrackPoint, TrackPoint]] = [
        (snapped_points[i], snapped_points[i + 1])
        for i in range(len(snapped_points) - 1)
        if snapped_points[i].lat != snapped_points[i + 1].lat
        or snapped_points[i].lon != snapped_points[i + 1].lon
    ]

    orig_to_snapped: list[float] = []
    if not snapped_segs:
        ref = snapped_points[0]
        orig_to_snapped = [
            geodesic_distance_m(pt.lat, pt.lon, ref.lat, ref.lon) for pt in original_points
        ]
    else:
        snap_geoms = [
            LineString([(a.lon, a.lat), (b.lon, b.lat)]) for a, b in snapped_segs
        ]
        snap_tree = STRtree(snap_geoms)
        for pt in original_points:
            orig_to_snapped.append(
                _min_distance_to_segments_m(pt, snapped_segs, snap_tree, snap_geoms)
            )

    orig_segs: list[tuple[TrackPoint, TrackPoint]] = [
        (original_points[i], original_points[i + 1])
        for i in range(len(original_points) - 1)
        if original_points[i].lat != original_points[i + 1].lat
        or original_points[i].lon != original_points[i + 1].lon
    ]

    snapped_to_orig_max = 0.0
    if not orig_segs:
        ref = original_points[0]
        snapped_to_orig_max = max(
            geodesic_distance_m(pt.lat, pt.lon, ref.lat, ref.lon) for pt in snapped_points
        )
    else:
        orig_geoms = [
            LineString([(a.lon, a.lat), (b.lon, b.lat)]) for a, b in orig_segs
        ]
        orig_tree = STRtree(orig_geoms)
        for pt in snapped_points:
            d = _min_distance_to_segments_m(pt, orig_segs, orig_tree, orig_geoms)
            if d > snapped_to_orig_max:
                snapped_to_orig_max = d

    max_dev = float(max(orig_to_snapped)) if orig_to_snapped else 0.0
    hausdorff = max(max_dev, snapped_to_orig_max)
    return (orig_to_snapped, hausdorff)


def compute_deviation_metrics(
    original_points: Sequence[TrackPoint],
    snapped_points: Sequence[TrackPoint],
) -> tuple[float, float, float, float]:
    """Compute `(max_m, p95_m, mean_m, hausdorff_m)` between original and snapped polylines."""
    orig_to_snapped, hausdorff = _compute_deviation_details(original_points, snapped_points)
    if not orig_to_snapped:
        return (0.0, 0.0, 0.0, 0.0)
    max_dev = float(max(orig_to_snapped))
    p95_dev = compute_percentile(orig_to_snapped, 95.0)
    mean_dev = float(sum(orig_to_snapped) / len(orig_to_snapped))
    return (max_dev, p95_dev, mean_dev, hausdorff)


def evaluate_topology_errors(
    track_results: Mapping[str, IngestionResult],
    expectations: TopologyExpectation | None,
) -> tuple[int, int, int]:
    """Compute `(false_joins, missed_joins, redundant_edges)` from labeled expectations."""
    if expectations is None:
        return (0, 0, 0)

    node_sets: dict[str, set[int]] = {}
    for label, res in track_results.items():
        nodes: set[int] = set()
        for seg_ids in res.snapped_segments:
            nodes.update(seg_ids)
        node_sets[label] = nodes

    false_joins = 0
    for label_a, label_b in expectations.forbidden_shared_node_pairs:
        if label_a in node_sets and label_b in node_sets:
            shared = node_sets[label_a].intersection(node_sets[label_b])
            false_joins += len(shared)

    missed_joins = 0
    for label_a, label_b in expectations.expected_shared_node_pairs:
        if label_a in node_sets and label_b in node_sets:
            shared = node_sets[label_a].intersection(node_sets[label_b])
            if not shared:
                missed_joins += 1

    redundant_edges = 0
    for label in expectations.expected_edge_reuse_tracks:
        if label in track_results:
            redundant_edges += track_results[label].edges_created

    return (false_joins, missed_joins, redundant_edges)


def evaluate_benchmark_pair(
    *,
    simplification_m: float,
    snap_radius_m: float,
    tracks: Mapping[str, ParsedTrack],
    results: Mapping[str, IngestionResult],
    node_coords: Mapping[int, TrackPoint],
    final_node_count: int,
    final_edge_count: int,
    expectations: TopologyExpectation | None = None,
) -> GeometryQualityMetrics:
    """Aggregate AC-65 / AC-96 geometry, topology, and performance metrics across tracks."""
    raw_points = 0
    reduced_points = 0
    nodes_created = 0
    nodes_reused = 0
    edges_created = 0
    edges_reused = 0
    self_loops_skipped = 0
    db_queries_count = 0

    spacings_m: list[float] = []
    all_deviations_m: list[float] = []
    max_hausdorff = 0.0
    total_original_length_m = 0.0
    total_snapped_length_m = 0.0
    latencies_ms: list[float] = []

    for label, res in results.items():
        track = tracks[label]
        raw_points += res.raw_point_count
        reduced_points += res.reduced_point_count
        nodes_created += res.nodes_created
        nodes_reused += res.nodes_reused
        edges_created += res.edges_created
        edges_reused += res.edges_reused
        self_loops_skipped += res.self_loops_skipped
        db_queries_count += res.db_queries_count
        latencies_ms.append(res.elapsed_ms)

        for edge_ref in res.edges:
            spacings_m.append(edge_ref.length_m)

        for seg, snapped_ids in zip(track.segments, res.snapped_segments, strict=False):
            orig_len = segment_length_m(seg.points)
            total_original_length_m += orig_len

            snapped_pts = [node_coords[nid] for nid in snapped_ids if nid in node_coords]
            total_snapped_length_m += segment_length_m(snapped_pts)

            per_point_devs, hausdorff = _compute_deviation_details(
                seg.points, snapped_pts
            )
            all_deviations_m.extend(per_point_devs)
            if hausdorff > max_hausdorff:
                max_hausdorff = hausdorff

    max_sp, p50_sp, p95_sp = compute_spacing_metrics(spacings_m)
    max_dev = float(max(all_deviations_m)) if all_deviations_m else 0.0
    p95_dev = compute_percentile(all_deviations_m, 95.0)
    mean_dev = (
        float(sum(all_deviations_m) / len(all_deviations_m)) if all_deviations_m else 0.0
    )

    false_joins, missed_joins, redundant_edges = evaluate_topology_errors(
        results, expectations
    )
    route_quality_ratio = (
        total_snapped_length_m / total_original_length_m
        if total_original_length_m > 0.0
        else 1.0
    )

    return GeometryQualityMetrics(
        simplification_m=simplification_m,
        snap_radius_m=snap_radius_m,
        raw_point_count=raw_points,
        reduced_point_count=reduced_points,
        final_node_count=final_node_count,
        final_edge_count=final_edge_count,
        nodes_created=nodes_created,
        nodes_reused=nodes_reused,
        edges_created=edges_created,
        edges_reused=edges_reused,
        self_loops_skipped=self_loops_skipped,
        max_node_spacing_m=max_sp,
        p50_node_spacing_m=p50_sp,
        p95_node_spacing_m=p95_sp,
        max_deviation_m=max_dev,
        p95_deviation_m=p95_dev,
        mean_deviation_m=mean_dev,
        hausdorff_deviation_m=max_hausdorff,
        false_joins=false_joins,
        missed_joins=missed_joins,
        redundant_edges=redundant_edges,
        route_quality_ratio=route_quality_ratio,
        p50_latency_ms=compute_percentile(latencies_ms, 50.0),
        p95_latency_ms=compute_percentile(latencies_ms, 95.0),
        peak_rss_mb=get_peak_rss_mb(),
        db_queries_count=db_queries_count,
    )
