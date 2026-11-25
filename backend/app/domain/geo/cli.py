"""Reusable CLI benchmark runner for node snapping & geometry quality (T-003 / T-028)."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from app.domain.geo.metrics import (
    TopologyExpectation,
    evaluate_benchmark_pair,
)
from app.domain.geo.models import (
    GeometryQualityMetrics,
    GeoParseError,
    IngestionResult,
    ParsedTrack,
    SnappingConfig,
    TrackPoint,
)
from app.domain.geo.parser import parse_track_directory, parse_track_file
from app.domain.geo.snapping import (
    InMemoryGraph,
    ingest_track_in_memory,
    ingest_track_transaction,
)

DEFAULT_PAIRS_ARG = "5:5,10:10,20:20,20:10,30:20"


def parse_threshold_pairs(raw: str) -> list[tuple[float, float]]:
    """Parse comma-separated `reduction:snap` meter pairs."""
    pairs: list[tuple[float, float]] = []
    for token in raw.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        parts = cleaned.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid pair '{cleaned}'; expected '<reduction_m>:<snap_m>'.")
        red_m = float(parts[0].strip())
        snap_m = float(parts[1].strip())
        if red_m < 0 or snap_m < 0:
            raise ValueError(f"Negative threshold in pair '{cleaned}'.")
        pairs.append((red_m, snap_m))
    if not pairs:
        raise ValueError("At least one reduction:snap pair must be provided.")
    return pairs


def load_topology_expectations(path: Path | None) -> TopologyExpectation | None:
    """Load optional labeled topology ground-truth expectations from JSON."""
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TopologyExpectation(
        expected_shared_node_pairs=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in raw.get("expected_shared_node_pairs", [])
        ),
        forbidden_shared_node_pairs=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in raw.get("forbidden_shared_node_pairs", [])
        ),
        expected_edge_reuse_tracks=tuple(
            str(name) for name in raw.get("expected_edge_reuse_tracks", [])
        ),
    )


def _reset_graph_tables(conn: Connection) -> None:
    conn.execute(text("TRUNCATE TABLE edges, nodes, tracks RESTART IDENTITY CASCADE"))
    conn.execute(text("UPDATE graph_metadata SET revision = 0 WHERE id = 1"))


def _fetch_node_coords_and_counts(
    conn: Connection,
) -> tuple[dict[int, TrackPoint], int, int]:
    node_rows = conn.execute(
        text("SELECT id, ST_Y(geom) AS lat, ST_X(geom) AS lon, ele FROM nodes")
    ).fetchall()
    node_coords: dict[int, TrackPoint] = {
        int(row[0]): TrackPoint(
            lat=float(row[1]),
            lon=float(row[2]),
            ele=float(row[3]) if row[3] is not None else None,
        )
        for row in node_rows
    }
    edge_count_row = conn.execute(text("SELECT COUNT(*) FROM edges")).fetchone()
    edge_count = int(edge_count_row[0]) if edge_count_row is not None else 0
    return node_coords, len(node_coords), edge_count


def load_tracks_from_input(
    input_path: Path,
    *,
    recursive: bool = False,
) -> tuple[list[tuple[str, ParsedTrack]], list[dict[str, str]]]:
    """Load valid GPX/KML tracks from a file or directory, recording skipped files."""
    if input_path.is_file():
        try:
            track = parse_track_file(input_path.name, input_path.read_bytes())
            return [(input_path.name, track)], []
        except GeoParseError as exc:
            return [], [{"file": input_path.name, "code": exc.code, "message": exc.message}]

    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    parsed, errors = parse_track_directory(input_path, recursive=recursive)
    loaded = [(p.name, t) for p, t in parsed]
    skipped = [
        {"file": p.name, "code": exc.code, "message": exc.message}
        for p, exc in errors
    ]
    return loaded, skipped


def run_benchmark(
    *,
    input_path: Path,
    pairs: Sequence[tuple[float, float]],
    database_url: str | None = None,
    in_memory: bool = False,
    reset_db: bool = False,
    expectations: TopologyExpectation | None = None,
    recursive: bool = False,
) -> dict[str, Any]:
    """Run snapping benchmark over all threshold pairs and return a structured report."""
    loaded_tracks, skipped_files = load_tracks_from_input(input_path, recursive=recursive)
    tracks_by_label: dict[str, ParsedTrack] = dict(loaded_tracks)

    pair_metrics: list[GeometryQualityMetrics] = []
    ingestion_errors: list[dict[str, Any]] = []

    if in_memory:
        for red_m, snap_m in pairs:
            graph = InMemoryGraph()
            config = SnappingConfig(
                point_simplification_distance_m=red_m,
                node_snap_radius_m=snap_m,
            )
            results: dict[str, IngestionResult] = {}
            for label, track in loaded_tracks:
                try:
                    results[label] = ingest_track_in_memory(graph, track, config)
                except GeoParseError as exc:
                    ingestion_errors.append(
                        {
                            "pair": f"{red_m}:{snap_m}",
                            "file": label,
                            "code": exc.code,
                            "message": exc.message,
                        }
                    )
            metrics = evaluate_benchmark_pair(
                simplification_m=red_m,
                snap_radius_m=snap_m,
                tracks=tracks_by_label,
                results=results,
                node_coords=graph.nodes,
                final_node_count=len(graph.nodes),
                final_edge_count=len(graph.edges),
                expectations=expectations,
            )
            pair_metrics.append(metrics)
    else:
        if not database_url:
            raise ValueError(
                "Either --database-url (or DATABASE_URL env var) or --in-memory is required."
            )
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            for red_m, snap_m in pairs:
                if reset_db:
                    with engine.begin() as conn:
                        _reset_graph_tables(conn)

                config = SnappingConfig(
                    point_simplification_distance_m=red_m,
                    node_snap_radius_m=snap_m,
                )
                results = {}
                for label, track in loaded_tracks:
                    try:
                        with engine.begin() as conn:
                            results[label] = ingest_track_transaction(conn, track, config)
                    except GeoParseError as exc:
                        ingestion_errors.append(
                            {
                                "pair": f"{red_m}:{snap_m}",
                                "file": label,
                                "code": exc.code,
                                "message": exc.message,
                            }
                        )

                with engine.connect() as conn:
                    node_coords, node_count, edge_count = _fetch_node_coords_and_counts(conn)

                metrics = evaluate_benchmark_pair(
                    simplification_m=red_m,
                    snap_radius_m=snap_m,
                    tracks=tracks_by_label,
                    results=results,
                    node_coords=node_coords,
                    final_node_count=node_count,
                    final_edge_count=edge_count,
                    expectations=expectations,
                )
                pair_metrics.append(metrics)
        finally:
            engine.dispose()

    return {
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "mode": "in_memory" if in_memory else "postgis",
        },
        "input": str(input_path),
        "tracks_loaded": len(loaded_tracks),
        "tracks_skipped": len(skipped_files),
        "skipped_files": skipped_files,
        "ingestion_errors": ingestion_errors,
        "results": [m.to_dict() for m in pair_metrics],
    }


def _format_summary_table(report: dict[str, Any]) -> str:
    lines = [
        f"Snapping Benchmark ({report['environment']['mode']}): "
        f"{report['tracks_loaded']} loaded, {report['tracks_skipped']} skipped",
        (
            f"{'Pair(m)':<9} {'Raw':>6} {'Red':>6} {'Nodes':>6} {'Edges':>6} "
            f"{'MaxSp':>7} {'P95Sp':>7} {'MaxDev':>7} {'P95Dev':>7} "
            f"{'FJ':>4} {'MJ':>4} {'RE':>4} {'RQ':>6} {'P95ms':>8}"
        ),
        "-" * 102,
    ]
    for row in report["results"]:
        pair = f"{row['simplification_m']:g}/{row['snap_radius_m']:g}"
        lines.append(
            f"{pair:<9} {row['raw_point_count']:>6} {row['reduced_point_count']:>6} "
            f"{row['final_node_count']:>6} {row['final_edge_count']:>6} "
            f"{row['max_node_spacing_m']:>7.1f} {row['p95_node_spacing_m']:>7.1f} "
            f"{row['max_deviation_m']:>7.1f} {row['p95_deviation_m']:>7.1f} "
            f"{row['false_joins']:>4} {row['missed_joins']:>4} {row['redundant_edges']:>4} "
            f"{row['route_quality_ratio']:>6.3f} {row['p95_latency_ms']:>8.2f}"
        )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.domain.geo.cli",
        description="Run OnTrack v2 node snapping & geometry quality benchmark.",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to a GPX/KML file or directory of track files.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL SQLAlchemy URL (defaults to DATABASE_URL env var).",
    )
    parser.add_argument(
        "--in-memory",
        action="store_true",
        help="Run using pure-Python WGS84 reference graph instead of PostgreSQL.",
    )
    parser.add_argument(
        "--pairs",
        default=DEFAULT_PAIRS_ARG,
        help=f"Comma-separated reduction:snap meter pairs (default: {DEFAULT_PAIRS_ARG}).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path to write full JSON benchmark report.",
    )
    parser.add_argument(
        "--labels-json",
        type=Path,
        default=None,
        help="Optional path to JSON file defining labeled topology expectations.",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Truncate graph tables and reset revision=0 before each threshold pair.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively scan subdirectories when --input is a directory.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        pairs = parse_threshold_pairs(args.pairs)
        expectations = load_topology_expectations(args.labels_json)
        report = run_benchmark(
            input_path=args.input,
            pairs=pairs,
            database_url=args.database_url,
            in_memory=args.in_memory,
            reset_db=args.reset_db,
            expectations=expectations,
            recursive=args.recursive,
        )
    except Exception as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 1

    print(_format_summary_table(report))

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
