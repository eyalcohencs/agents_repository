"""Synthetic graph generators, AC-78/AC-30/AC-62 fixtures, and pgRouting benchmarks."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.integrations.routing.graph_router import (
    NEAREST_NODE_SQL,
    build_shortest_path_sql,
    find_shortest_path,
)
from app.integrations.routing.models import (
    Coordinate,
    RoutingAlgorithm,
    RoutingCostMode,
)

# Representative Israeli off-road origin (Mount Carmel / Jezreel Valley)
DEFAULT_BASE_LAT = 32.6000
DEFAULT_BASE_LNG = 35.0000


@dataclass(frozen=True, slots=True)
class GraphScaleSpec:
    """Dimensions for a deterministic synthetic grid graph."""

    label: str
    rows: int
    cols: int
    step_deg: float = 0.0018  # ~200 m lat spacing, ~170 m lng spacing in Israel


DEFAULT_SCALE_SPECS: tuple[GraphScaleSpec, ...] = (
    GraphScaleSpec(label="1x", rows=20, cols=20),
    GraphScaleSpec(label="5x", rows=45, cols=45),
    GraphScaleSpec(label="10x", rows=64, cols=64),
)


@dataclass(frozen=True, slots=True)
class SeededGraph:
    """Metadata and sample endpoints for a seeded synthetic graph."""

    label: str
    node_count: int
    edge_count: int
    node_ids: tuple[int, ...]
    sample_pairs: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class OutsideBBoxFixture:
    """Deterministic AC-78 fixture where the globally optimal detour exits the Start/Finish bbox."""

    start_node_id: int
    end_node_id: int
    detour_apex_node_id: int
    start_coord: Coordinate
    end_coord: Coordinate
    detour_apex_coord: Coordinate
    detour_edge_ids: tuple[int, ...]
    inside_bbox_edge_ids: tuple[int, ...]
    tight_bbox: tuple[float, float, float, float]  # (min_lat, min_lng, max_lat, max_lng)


@dataclass(frozen=True, slots=True)
class DifficultyNeutralityFixture:
    """Deterministic AC-30 fixture with two competing paths between the same endpoints."""

    start_node_id: int
    end_node_id: int
    start_coord: Coordinate
    end_coord: Coordinate
    shorter_path_edge_ids: tuple[int, ...]
    longer_path_edge_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Summary latency statistics in milliseconds."""

    count: int
    min_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    mean_ms: float


@dataclass(frozen=True, slots=True)
class ScaleBenchmarkResult:
    """Measured pgRouting performance and EXPLAIN ANALYZE output for one scale/mode/algorithm."""

    scale_label: str
    algorithm: RoutingAlgorithm
    cost_mode: RoutingCostMode
    node_count: int
    edge_count: int
    latency: LatencyStats
    pair_costs: tuple[float, ...]
    explain_plan: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConcurrentBenchmarkResult:
    """Measured reader snapshot consistency and latency under active concurrent writes (AC-62)."""

    reads_completed: int
    writes_committed: int
    reader_errors: tuple[str, ...]
    partial_snapshot_violations: int
    observed_revisions: tuple[int, ...]
    latency: LatencyStats


def compute_percentiles(samples_ms: Sequence[float]) -> LatencyStats:
    """Compute exact min/p50/p95/max/mean statistics over positive millisecond samples."""
    if not samples_ms:
        raise ValueError("samples_ms must be non-empty.")
    ordered = sorted(float(x) for x in samples_ms)
    count = len(ordered)

    def _percentile(pct: float) -> float:
        if count == 1:
            return ordered[0]
        rank = (pct / 100.0) * (count - 1)
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            return ordered[lower]
        weight = rank - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return LatencyStats(
        count=count,
        min_ms=ordered[0],
        p50_ms=_percentile(50.0),
        p95_ms=_percentile(95.0),
        max_ms=ordered[-1],
        mean_ms=sum(ordered) / count,
    )


def generate_grid_topology(
    rows: int,
    cols: int,
    *,
    base_lat: float = DEFAULT_BASE_LAT,
    base_lng: float = DEFAULT_BASE_LNG,
    step_deg: float = 0.0018,
) -> tuple[list[Coordinate], list[tuple[int, int, int]]]:
    """Generate deterministic Israel-region grid coordinates and undirected edge tuples.

    Returns `(coords, edges)` where each edge is `(src_idx, tgt_idx, initial_difficulty)`.
    """
    if rows < 2 or cols < 2:
        raise ValueError("rows and cols must be >= 2.")

    coords: list[Coordinate] = []
    for r in range(rows):
        for c in range(cols):
            # Subtle deterministic perturbation so paths have varied edge lengths
            lat = base_lat + r * step_deg + ((r * 7 + c * 3) % 5) * 0.00003
            lng = base_lng + c * step_deg + ((r * 5 + c * 11) % 5) * 0.00003
            ele = 150.0 + float((r * 13 + c * 17) % 250)
            coords.append(Coordinate(lat=lat, lng=lng, ele=ele))

    edges: list[tuple[int, int, int]] = []
    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            if c + 1 < cols:
                diff = ((r + c) % 3) + 1
                edges.append((idx, idx + 1, diff))
            if r + 1 < rows:
                diff = ((r * 2 + c) % 3) + 1
                edges.append((idx, idx + cols, diff))

    return coords, edges


def _bulk_insert_nodes_and_edges(
    conn: Connection,
    coords: Sequence[Coordinate],
    edges: Sequence[tuple[int, int, int]],
    *,
    truncate: bool = False,
) -> tuple[list[int], list[int]]:
    """Bulk-insert nodes and PostGIS straight-line edges, updating graph_metadata."""
    if truncate:
        conn.execute(text("TRUNCATE TABLE edges, nodes RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                "INSERT INTO graph_metadata (id, revision) VALUES (1, 0) "
                "ON CONFLICT (id) DO UPDATE SET revision = 0"
            )
        )

    lngs = [pt.lng for pt in coords]
    lats = [pt.lat for pt in coords]
    eles = [pt.ele for pt in coords]

    node_rows = conn.execute(
        text(
            """
            INSERT INTO nodes (geom, ele)
            SELECT
                ST_SetSRID(ST_MakePoint(t.lng, t.lat), 4326),
                t.ele
            FROM unnest(
                CAST(:lngs AS double precision[]),
                CAST(:lats AS double precision[]),
                CAST(:eles AS real[])
            ) WITH ORDINALITY AS t(lng, lat, ele, ord)
            ORDER BY t.ord
            RETURNING id
            """
        ),
        {"lngs": lngs, "lats": lats, "eles": eles},
    ).fetchall()
    node_ids = [int(row[0]) for row in node_rows]

    if not edges:
        conn.execute(
            text("UPDATE graph_metadata SET revision = revision + 1 WHERE id = 1")
        )
        return node_ids, []

    src_ids = [node_ids[src] for src, _, _ in edges]
    tgt_ids = [node_ids[tgt] for _, tgt, _ in edges]
    diffs = [diff for _, _, diff in edges]

    edge_rows = conn.execute(
        text(
            """
            INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
            SELECT
                t.src,
                t.tgt,
                ST_MakeLine(ns.geom, nt.geom),
                ST_Length(ST_MakeLine(ns.geom, nt.geom)::geography)::real,
                t.diff
            FROM unnest(
                CAST(:srcs AS bigint[]),
                CAST(:tgts AS bigint[]),
                CAST(:diffs AS smallint[])
            ) WITH ORDINALITY AS t(src, tgt, diff, ord)
            JOIN nodes AS ns ON ns.id = t.src
            JOIN nodes AS nt ON nt.id = t.tgt
            ORDER BY t.ord
            RETURNING id
            """
        ),
        {"srcs": src_ids, "tgts": tgt_ids, "diffs": diffs},
    ).fetchall()
    edge_ids = [int(row[0]) for row in edge_rows]

    conn.execute(text("UPDATE graph_metadata SET revision = revision + 1 WHERE id = 1"))
    return node_ids, edge_ids


def analyze_graph_tables(conn: Connection) -> None:
    """Refresh PostgreSQL planner statistics on `nodes` and `edges` before benchmarking."""
    conn.execute(text("ANALYZE nodes"))
    conn.execute(text("ANALYZE edges"))


def seed_synthetic_grid_graph(
    conn: Connection,
    spec: GraphScaleSpec,
    *,
    base_lat: float = DEFAULT_BASE_LAT,
    base_lng: float = DEFAULT_BASE_LNG,
    truncate: bool = True,
) -> SeededGraph:
    """Seed a deterministic synthetic grid graph and run `ANALYZE nodes; ANALYZE edges;`."""
    coords, edges = generate_grid_topology(
        spec.rows,
        spec.cols,
        base_lat=base_lat,
        base_lng=base_lng,
        step_deg=spec.step_deg,
    )
    node_ids, edge_ids = _bulk_insert_nodes_and_edges(
        conn,
        coords,
        edges,
        truncate=truncate,
    )
    analyze_graph_tables(conn)

    rows, cols = spec.rows, spec.cols
    sample_pairs = (
        (node_ids[0], node_ids[rows * cols - 1]),
        (node_ids[cols - 1], node_ids[(rows - 1) * cols]),
        (node_ids[rows // 4 * cols + cols // 4], node_ids[(3 * rows // 4) * cols + 3 * cols // 4]),
        (node_ids[0], node_ids[(rows // 2) * cols + cols - 1]),
        (node_ids[cols // 2], node_ids[(rows - 1) * cols + cols // 2]),
    )
    return SeededGraph(
        label=spec.label,
        node_count=len(node_ids),
        edge_count=len(edge_ids),
        node_ids=tuple(node_ids),
        sample_pairs=sample_pairs,
    )


def seed_outside_bbox_fixture(
    conn: Connection,
    *,
    truncate: bool = True,
) -> OutsideBBoxFixture:
    """Seed an AC-78 fixture where the globally optimal detour exits the Start/Finish bbox.

    - Start `(32.6000, 35.0000)` to End `(32.6000, 35.0200)` (~1.88 km straight east).
    - Direct inside-bbox path: 16-hop zigzag strictly within `lat in [32.598, 32.602]`
      (~7.3 km total distance, 16 hops).
    - Outside-bbox optimal detour: 2 edges looping north to `(32.6120, 35.0100)`
      (~3.26 km total distance, 2 hops), >1.3 km north of the tight Start/Finish bbox.
    The outside-bbox detour is strictly optimal under BOTH `distance` and `hops`.
    """
    start_coord = Coordinate(lat=32.6000, lng=35.0000, ele=200.0)
    end_coord = Coordinate(lat=32.6000, lng=35.0200, ele=210.0)
    detour_apex = Coordinate(lat=32.6120, lng=35.0100, ele=260.0)

    zigzag_steps = 16
    coords: list[Coordinate] = [start_coord, end_coord, detour_apex]
    # Indices: 0=start, 1=end, 2=detour_apex, 3..17=15 internal zigzag vertices
    for step in range(1, zigzag_steps):
        frac = step / zigzag_steps
        lng = start_coord.lng + frac * (end_coord.lng - start_coord.lng)
        lat = 32.6020 if step % 2 == 1 else 32.5980
        coords.append(Coordinate(lat=lat, lng=lng, ele=205.0))

    edges: list[tuple[int, int, int]] = [
        # Optimal outside-bbox detour (2 edges, indices 0 and 1 in returned edge_ids)
        (0, 2, 2),
        (2, 1, 2),
    ]
    # Suboptimal inside-bbox zigzag path (16 edges)
    prev_idx = 0
    for z_idx in range(3, 3 + zigzag_steps - 1):
        edges.append((prev_idx, z_idx, 1))
        prev_idx = z_idx
    edges.append((prev_idx, 1, 1))

    node_ids, edge_ids = _bulk_insert_nodes_and_edges(
        conn,
        coords,
        edges,
        truncate=truncate,
    )
    analyze_graph_tables(conn)

    return OutsideBBoxFixture(
        start_node_id=node_ids[0],
        end_node_id=node_ids[1],
        detour_apex_node_id=node_ids[2],
        start_coord=start_coord,
        end_coord=end_coord,
        detour_apex_coord=detour_apex,
        detour_edge_ids=(edge_ids[0], edge_ids[1]),
        inside_bbox_edge_ids=tuple(edge_ids[2:]),
        tight_bbox=(32.5980, 35.0000, 32.6020, 35.0200),
    )


def seed_difficulty_neutrality_fixture(
    conn: Connection,
    *,
    truncate: bool = True,
) -> DifficultyNeutralityFixture:
    """Seed an AC-30 fixture with two competing paths (shorter vs longer)."""
    start_coord = Coordinate(lat=32.6200, lng=35.0200, ele=180.0)
    end_coord = Coordinate(lat=32.6200, lng=35.0400, ele=190.0)
    short_mid = Coordinate(lat=32.6205, lng=35.0300, ele=185.0)
    long_mid_a = Coordinate(lat=32.6300, lng=35.0260, ele=220.0)
    long_mid_b = Coordinate(lat=32.6300, lng=35.0340, ele=225.0)

    coords = [start_coord, end_coord, short_mid, long_mid_a, long_mid_b]
    edges = [
        # Shorter path (initial_difficulty = 1)
        (0, 2, 1),
        (2, 1, 1),
        # Longer path (initial_difficulty = 1)
        (0, 3, 1),
        (3, 4, 1),
        (4, 1, 1),
    ]
    node_ids, edge_ids = _bulk_insert_nodes_and_edges(
        conn,
        coords,
        edges,
        truncate=truncate,
    )
    analyze_graph_tables(conn)

    return DifficultyNeutralityFixture(
        start_node_id=node_ids[0],
        end_node_id=node_ids[1],
        start_coord=start_coord,
        end_coord=end_coord,
        shorter_path_edge_ids=(edge_ids[0], edge_ids[1]),
        longer_path_edge_ids=(edge_ids[2], edge_ids[3], edge_ids[4]),
    )


def explain_shortest_path(
    conn: Connection,
    start_node_id: int,
    end_node_id: int,
    *,
    cost_mode: RoutingCostMode = "distance",
    algorithm: RoutingAlgorithm = "pgr_dijkstra",
) -> tuple[str, ...]:
    """Capture `EXPLAIN (ANALYZE, BUFFERS)` plan for exact full-graph shortest path."""
    sql_body = build_shortest_path_sql(cost_mode=cost_mode, algorithm=algorithm)
    rows = conn.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS)\n{sql_body}"),
        {"start_node_id": start_node_id, "end_node_id": end_node_id},
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def explain_nearest_node(
    conn: Connection,
    point: Coordinate,
    *,
    radius_m: float = 2000.0,
) -> tuple[str, ...]:
    """Capture `EXPLAIN (ANALYZE, BUFFERS)` plan for indexed geography KNN node lookup."""
    rows = conn.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS)\n{NEAREST_NODE_SQL}"),
        {"lng": point.lng, "lat": point.lat, "radius_m": radius_m},
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def benchmark_graph_scale(
    engine: Engine,
    *,
    seeded: SeededGraph,
    algorithms: Sequence[RoutingAlgorithm] = ("pgr_dijkstra", "pgr_bdDijkstra"),
    cost_modes: Sequence[RoutingCostMode] = ("distance", "hops"),
    iterations_per_pair: int = 1,
) -> list[ScaleBenchmarkResult]:
    """Benchmark exact `pgr_dijkstra` vs `pgr_bdDijkstra` across cost modes on a seeded graph."""
    if iterations_per_pair < 1:
        raise ValueError("iterations_per_pair must be >= 1.")

    results: list[ScaleBenchmarkResult] = []
    with engine.connect() as conn:
        for cost_mode in cost_modes:
            for algorithm in algorithms:
                samples_ms: list[float] = []
                pair_costs: list[float] = []
                for start_id, end_id in seeded.sample_pairs:
                    last_cost: float | None = None
                    for _ in range(iterations_per_pair):
                        started = time.perf_counter()
                        path = find_shortest_path(
                            conn,
                            start_id,
                            end_id,
                            cost_mode=cost_mode,
                            algorithm=algorithm,
                        )
                        elapsed_ms = (time.perf_counter() - started) * 1000.0
                        if path is None:
                            raise RuntimeError(
                                f"Expected connected path for ({start_id}, {end_id}) in "
                                f"synthetic graph {seeded.label!r}."
                            )
                        samples_ms.append(elapsed_ms)
                        last_cost = path.total_cost
                    assert last_cost is not None
                    pair_costs.append(last_cost)

                first_start, first_end = seeded.sample_pairs[0]
                plan = explain_shortest_path(
                    conn,
                    first_start,
                    first_end,
                    cost_mode=cost_mode,
                    algorithm=algorithm,
                )
                results.append(
                    ScaleBenchmarkResult(
                        scale_label=seeded.label,
                        algorithm=algorithm,
                        cost_mode=cost_mode,
                        node_count=seeded.node_count,
                        edge_count=seeded.edge_count,
                        latency=compute_percentiles(samples_ms),
                        pair_costs=tuple(pair_costs),
                        explain_plan=plan,
                    )
                )

    return results


def benchmark_concurrent_reads_during_writes(
    engine: Engine,
    *,
    pairs: Sequence[tuple[int, int]],
    writer_batches: int = 5,
    nodes_per_batch: int = 10,
    writer_hold_s: float = 0.03,
    cost_mode: RoutingCostMode = "distance",
    algorithm: RoutingAlgorithm = "pgr_dijkstra",
) -> ConcurrentBenchmarkResult:
    """Verify AC-62 snapshot isolation: concurrent `REPEATABLE READ READ ONLY` reads during writes.

    Each writer batch inserts `nodes_per_batch` nodes and `(nodes_per_batch - 1)` connected edges
    in one transaction, sleeps `writer_hold_s` mid-transaction before committing (and also executes
    a rolled-back batch), while readers verify:
    - zero partial uncommitted batches are ever observed (`delta % edges_per_batch == 0`)
    - shortest-path calculation succeeds consistently with zero reader errors
    """
    if not pairs:
        raise ValueError("pairs must be non-empty.")
    if nodes_per_batch < 2:
        raise ValueError("nodes_per_batch must be >= 2.")

    edges_per_batch = nodes_per_batch - 1
    with engine.connect() as conn:
        initial_edge_count = int(conn.execute(text("SELECT count(*) FROM edges")).scalar_one())

    writer_done = threading.Event()
    writer_started = threading.Event()
    writer_errors: list[str] = []
    writes_committed = 0

    def _writer() -> None:
        nonlocal writes_committed
        writer_started.set()
        try:
            for batch_idx in range(writer_batches):
                base_lat = 32.8000 + batch_idx * 0.0100
                coords = [
                    Coordinate(lat=base_lat, lng=35.2000 + i * 0.0010, ele=300.0)
                    for i in range(nodes_per_batch)
                ]
                edge_tuples = [(i, i + 1, 2) for i in range(edges_per_batch)]

                # Uncommitted / rolled-back batch to verify readers never see dirty writes
                with engine.connect() as conn:
                    tx = conn.begin()
                    try:
                        _bulk_insert_nodes_and_edges(conn, coords, edge_tuples, truncate=False)
                        time.sleep(writer_hold_s / 2.0)
                    finally:
                        tx.rollback()

                # Committed multi-statement batch held open briefly before commit
                with engine.connect() as conn, conn.begin():
                    half = len(edge_tuples) // 2
                    inserted_nodes, _ = _bulk_insert_nodes_and_edges(
                        conn,
                        coords,
                        edge_tuples[:half],
                        truncate=False,
                    )
                    time.sleep(writer_hold_s)
                    rem = edge_tuples[half:]
                    if rem:
                        conn.execute(
                            text(
                                """
                                INSERT INTO edges (
                                    source, target, geom, length_m, initial_difficulty
                                )
                                SELECT
                                    t.src,
                                    t.tgt,
                                    ST_MakeLine(ns.geom, nt.geom),
                                    ST_Length(ST_MakeLine(ns.geom, nt.geom)::geography)::real,
                                    t.diff
                                FROM unnest(
                                    CAST(:srcs AS bigint[]),
                                    CAST(:tgts AS bigint[]),
                                    CAST(:diffs AS smallint[])
                                ) AS t(src, tgt, diff)
                                JOIN nodes AS ns ON ns.id = t.src
                                JOIN nodes AS nt ON nt.id = t.tgt
                                """
                            ),
                            {
                                "srcs": [inserted_nodes[s] for s, _, _ in rem],
                                "tgts": [inserted_nodes[t] for _, t, _ in rem],
                                "diffs": [d for _, _, d in rem],
                            },
                        )
                        conn.execute(
                            text(
                                "UPDATE graph_metadata SET revision = revision + 1 WHERE id = 1"
                            )
                        )
                    writes_committed += 1
        except Exception as exc:  # pragma: no cover - surfaced in assertions
            writer_errors.append(f"writer error: {exc!r}")
        finally:
            writer_done.set()

    reader_latencies: list[float] = []
    reader_errors: list[str] = []
    observed_revisions: set[int] = set()
    partial_snapshot_violations = 0

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()
    writer_started.wait(timeout=5.0)

    pair_idx = 0
    while not writer_done.is_set() or len(reader_latencies) < max(3, len(pairs)):
        start_id, end_id = pairs[pair_idx % len(pairs)]
        pair_idx += 1
        started = time.perf_counter()
        try:
            with engine.connect() as conn, conn.begin():
                conn.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                rev_before = int(
                    conn.execute(
                        text("SELECT revision FROM graph_metadata WHERE id = 1")
                    ).scalar_one()
                )
                edge_count_1 = int(
                    conn.execute(text("SELECT count(*) FROM edges")).scalar_one()
                )
                path = find_shortest_path(
                    conn,
                    start_id,
                    end_id,
                    cost_mode=cost_mode,
                    algorithm=algorithm,
                )
                edge_count_2 = int(
                    conn.execute(text("SELECT count(*) FROM edges")).scalar_one()
                )
                rev_after = int(
                    conn.execute(
                        text("SELECT revision FROM graph_metadata WHERE id = 1")
                    ).scalar_one()
                )

                observed_revisions.add(rev_before)
                delta = edge_count_1 - initial_edge_count
                if (
                    edge_count_1 != edge_count_2
                    or rev_before != rev_after
                    or delta < 0
                    or (delta % edges_per_batch) != 0
                ):
                    partial_snapshot_violations += 1
                if path is None:
                    reader_errors.append(
                        f"Unexpected disconnected path for ({start_id}, {end_id})."
                    )
        except Exception as exc:  # pragma: no cover - surfaced in assertions
            reader_errors.append(f"reader error: {exc!r}")
        else:
            reader_latencies.append((time.perf_counter() - started) * 1000.0)

    writer_thread.join(timeout=30.0)
    if writer_errors:
        reader_errors.extend(writer_errors)

    return ConcurrentBenchmarkResult(
        reads_completed=len(reader_latencies),
        writes_committed=writes_committed,
        reader_errors=tuple(reader_errors),
        partial_snapshot_violations=partial_snapshot_violations,
        observed_revisions=tuple(sorted(observed_revisions)),
        latency=compute_percentiles(reader_latencies or [0.0]),
    )
