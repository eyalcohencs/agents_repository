"""Exact full-graph pgRouting queries and two-point hybrid leg orchestration."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.integrations.routing.models import (
    Coordinate,
    ExternalRouteResult,
    HybridRouteResult,
    NearestNodeMatch,
    NetworkPathResult,
    RouteBranch,
    RouteContinuityError,
    RouteSegment,
    RoutingAlgorithm,
    RoutingConfig,
    RoutingCostMode,
    RoutingProviderError,
    SearchTier,
    geodesic_distance_m,
)
from app.integrations.routing.protocol import RoutingProvider

_VALID_ALGORITHMS: dict[RoutingAlgorithm, str] = {
    "pgr_dijkstra": "pgr_dijkstra",
    "pgr_bdDijkstra": "pgr_bdDijkstra",
}

_INNER_EDGE_SQL: dict[RoutingCostMode, str] = {
    "distance": (
        "SELECT id::bigint AS id, source::bigint AS source, target::bigint AS target, "
        "length_m::double precision AS cost, length_m::double precision AS reverse_cost "
        "FROM edges"
    ),
    "hops": (
        "SELECT id::bigint AS id, source::bigint AS source, target::bigint AS target, "
        "1.0::double precision AS cost, 1.0::double precision AS reverse_cost "
        "FROM edges"
    ),
}

NEAREST_NODE_SQL = """
SELECT
    n.id,
    ST_Y(n.geom) AS lat,
    ST_X(n.geom) AS lng,
    n.ele,
    ST_Distance(
        n.geom::geography,
        ST_SetSRID(
            ST_MakePoint(
                CAST(:lng AS double precision),
                CAST(:lat AS double precision)
            ),
            4326
        )::geography
    ) AS distance_m
FROM nodes AS n
WHERE ST_DWithin(
    n.geom::geography,
    ST_SetSRID(
        ST_MakePoint(
            CAST(:lng AS double precision),
            CAST(:lat AS double precision)
        ),
        4326
    )::geography,
    CAST(:radius_m AS double precision)
)
ORDER BY
    n.geom::geography <-> ST_SetSRID(
        ST_MakePoint(
            CAST(:lng AS double precision),
            CAST(:lat AS double precision)
        ),
        4326
    )::geography ASC,
    n.id ASC
LIMIT 1
""".strip()


def build_shortest_path_sql(
    *,
    cost_mode: RoutingCostMode = "distance",
    algorithm: RoutingAlgorithm = "pgr_dijkstra",
) -> str:
    """Build exact full-graph pgRouting SQL (no bbox pruning, no difficulty in cost/WHERE)."""
    if cost_mode not in _INNER_EDGE_SQL:
        raise ValueError(f"Unsupported cost_mode: {cost_mode!r}")
    if algorithm not in _VALID_ALGORITHMS:
        raise ValueError(f"Unsupported algorithm: {algorithm!r}")

    inner_sql = _INNER_EDGE_SQL[cost_mode]
    pgr_fn = _VALID_ALGORITHMS[algorithm]

    return (
        "SELECT\n"
        "    p.seq,\n"
        "    p.node,\n"
        "    p.edge,\n"
        "    p.cost,\n"
        "    p.agg_cost,\n"
        "    e.source AS edge_source,\n"
        "    e.target AS edge_target,\n"
        "    e.initial_difficulty,\n"
        "    ns.ele AS source_ele,\n"
        "    nt.ele AS target_ele,\n"
        "    CASE\n"
        "        WHEN p.edge = -1 THEN NULL\n"
        "        WHEN p.node = e.target THEN ST_AsGeoJSON(ST_Reverse(e.geom))\n"
        "        ELSE ST_AsGeoJSON(e.geom)\n"
        "    END AS oriented_geojson\n"
        f"FROM {pgr_fn}(\n"
        f"    '{inner_sql}',\n"
        "    CAST(:start_node_id AS BIGINT),\n"
        "    CAST(:end_node_id AS BIGINT),\n"
        "    directed := false\n"
        ") AS p\n"
        "LEFT JOIN edges AS e ON e.id = p.edge\n"
        "LEFT JOIN nodes AS ns ON ns.id = e.source\n"
        "LEFT JOIN nodes AS nt ON nt.id = e.target\n"
        "ORDER BY p.seq ASC"
    )


def orient_edge_points(
    raw_coordinates: Sequence[Sequence[float]],
    *,
    reversed_traversal: bool,
    source_ele: float | None = None,
    target_ele: float | None = None,
    already_reversed: bool = False,
) -> tuple[Coordinate, ...]:
    """Convert `[lng, lat(, ele)]` coordinates into oriented `Coordinate` points with node `ele`."""
    coords_seq = list(raw_coordinates)
    if reversed_traversal and not already_reversed:
        coords_seq.reverse()

    start_ele = target_ele if reversed_traversal else source_ele
    end_ele = source_ele if reversed_traversal else target_ele
    last_idx = len(coords_seq) - 1

    points: list[Coordinate] = []
    for idx, raw in enumerate(coords_seq):
        lng = float(raw[0])
        lat = float(raw[1])
        ele: float | None = float(raw[2]) if len(raw) >= 3 and raw[2] is not None else None
        if ele is None:
            if idx == 0 and start_ele is not None:
                ele = float(start_ele)
            elif idx == last_idx and end_ele is not None:
                ele = float(end_ele)
        points.append(Coordinate(lat=lat, lng=lng, ele=ele))

    return tuple(points)


def _find_nearest_node_within_radius(
    conn: Connection,
    point: Coordinate,
    *,
    radius_m: float,
    tier: SearchTier,
) -> NearestNodeMatch | None:
    row = (
        conn.execute(
            text(NEAREST_NODE_SQL),
            {"lng": point.lng, "lat": point.lat, "radius_m": radius_m},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    ele_raw = row["ele"]
    coord = Coordinate(
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        ele=float(ele_raw) if ele_raw is not None else None,
    )
    return NearestNodeMatch(
        node_id=int(row["id"]),
        coord=coord,
        distance_m=float(row["distance_m"]),
        search_tier=tier,
    )


def find_nearest_node(
    conn: Connection,
    point: Coordinate,
    *,
    close_radius_m: float = 2000.0,
    far_radius_m: float = 10000.0,
) -> NearestNodeMatch | None:
    """Find nearest network node using `CLOSE` then `FAR` indexed geography search + `id ASC`."""
    if close_radius_m <= 0:
        raise ValueError("close_radius_m must be > 0.")
    if far_radius_m < close_radius_m:
        raise ValueError("far_radius_m must be >= close_radius_m.")

    close_match = _find_nearest_node_within_radius(
        conn,
        point,
        radius_m=close_radius_m,
        tier="close",
    )
    if close_match is not None:
        return close_match

    if far_radius_m == close_radius_m:
        return None

    return _find_nearest_node_within_radius(
        conn,
        point,
        radius_m=far_radius_m,
        tier="far",
    )


def find_shortest_path(
    conn: Connection,
    start_node_id: int,
    end_node_id: int,
    *,
    cost_mode: RoutingCostMode = "distance",
    algorithm: RoutingAlgorithm = "pgr_dijkstra",
) -> NetworkPathResult | None:
    """Run exact full-graph pgRouting shortest path (`pgr_dijkstra` or `pgr_bdDijkstra`)."""
    sql_text = build_shortest_path_sql(cost_mode=cost_mode, algorithm=algorithm)

    if start_node_id == end_node_id:
        return NetworkPathResult(
            segments=(),
            total_cost=0.0,
            algorithm=algorithm,
            cost_mode=cost_mode,
            node_ids=(start_node_id,),
            edge_ids=(),
        )

    rows = (
        conn.execute(
            text(sql_text),
            {"start_node_id": start_node_id, "end_node_id": end_node_id},
        )
        .mappings()
        .all()
    )
    if not rows:
        return None

    node_ids: list[int] = []
    edge_ids: list[int] = []
    segments: list[RouteSegment] = []

    for row in rows:
        node_id = int(row["node"])
        edge_id = int(row["edge"])
        node_ids.append(node_id)
        if edge_id == -1:
            continue

        raw_geojson = row["oriented_geojson"]
        if not isinstance(raw_geojson, str):
            raise ValueError(f"Missing geometry for edge_id={edge_id}.")
        geom_obj = json.loads(raw_geojson)
        raw_coords = geom_obj.get("coordinates") if isinstance(geom_obj, dict) else None
        if not isinstance(raw_coords, list) or len(raw_coords) < 2:
            raise ValueError(f"Degenerate LineString geometry for edge_id={edge_id}.")

        edge_target = int(row["edge_target"])
        reversed_traversal = node_id == edge_target
        source_ele = float(row["source_ele"]) if row["source_ele"] is not None else None
        target_ele = float(row["target_ele"]) if row["target_ele"] is not None else None

        points = orient_edge_points(
            raw_coords,
            reversed_traversal=reversed_traversal,
            source_ele=source_ele,
            target_ele=target_ele,
            already_reversed=True,
        )

        initial_diff = int(row["initial_difficulty"])
        segments.append(
            RouteSegment(
                source="network",
                points=points,
                edge_id=edge_id,
                provider_profile=None,
                avg_difficulty=float(initial_diff),
                display_level=initial_diff,
            )
        )
        edge_ids.append(edge_id)

    total_cost = float(rows[-1]["agg_cost"])
    return NetworkPathResult(
        segments=tuple(segments),
        total_cost=total_cost,
        algorithm=algorithm,
        cost_mode=cost_mode,
        node_ids=tuple(node_ids),
        edge_ids=tuple(edge_ids),
    )


def _external_result_to_segment(
    result: ExternalRouteResult,
    *,
    section_name: str,
) -> RouteSegment:
    if len(result.points) < 2:
        raise RoutingProviderError(
            code="EMPTY_GEOMETRY",
            status_code=None,
            retryable=False,
            message=(
                f"External routing provider {result.provider!r} returned fewer than 2 points "
                f"for {section_name} section."
            ),
            provider=result.provider,
        )
    return RouteSegment(
        source="external",
        points=result.points,
        edge_id=None,
        provider_profile=result.profile,
        avg_difficulty=None,
        display_level=None,
    )


def verify_route_continuity(
    labeled_segments: Sequence[tuple[str, RouteSegment]],
    *,
    tolerance_m: float,
) -> tuple[float, ...]:
    """Verify every adjacent segment pair is continuous within `tolerance_m`."""
    if not labeled_segments:
        raise ValueError("Route must contain at least one segment.")

    gaps: list[float] = []
    for idx in range(len(labeled_segments) - 1):
        prev_label, prev_seg = labeled_segments[idx]
        next_label, next_seg = labeled_segments[idx + 1]
        gap_m = geodesic_distance_m(prev_seg.points[-1], next_seg.points[0])
        if gap_m > tolerance_m:
            raise RouteContinuityError(
                gap_m=gap_m,
                between_sections=f"{prev_label}->{next_label}",
            )
        gaps.append(gap_m)
    return tuple(gaps)


def _read_graph_snapshot(
    engine: Engine,
    origin: Coordinate,
    destination: Coordinate,
    config: RoutingConfig,
) -> tuple[int | None, NearestNodeMatch | None, NearestNodeMatch | None, NetworkPathResult | None]:
    """Run all DB graph reads inside one short `REPEATABLE READ READ ONLY` transaction."""
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))

        rev_row = conn.execute(
            text("SELECT revision FROM graph_metadata WHERE id = 1")
        ).first()
        graph_revision = int(rev_row[0]) if rev_row is not None else None

        entry_node = find_nearest_node(
            conn,
            origin,
            close_radius_m=config.close_radius_m,
            far_radius_m=config.far_radius_m,
        )
        if entry_node is None:
            return graph_revision, None, None, None

        exit_node = find_nearest_node(
            conn,
            destination,
            close_radius_m=config.close_radius_m,
            far_radius_m=config.far_radius_m,
        )
        if exit_node is None:
            return graph_revision, entry_node, None, None

        network_path = find_shortest_path(
            conn,
            entry_node.node_id,
            exit_node.node_id,
            cost_mode=config.cost_mode,
            algorithm=config.algorithm,
        )
        return graph_revision, entry_node, exit_node, network_path


_DEFAULT_ROUTING_CONFIG = RoutingConfig()


def calculate_leg(
    engine: Engine,
    provider: RoutingProvider,
    origin: Coordinate,
    destination: Coordinate,
    *,
    config: RoutingConfig = _DEFAULT_ROUTING_CONFIG,
) -> HybridRouteResult:
    """Compute a complete, continuity-verified two-point hybrid route leg.

    Guarantees:
    - All graph reads execute in one short `REPEATABLE READ READ ONLY` transaction.
    - The DB transaction and connection are closed BEFORE any external HTTP call.
    - Handles all 5 hybrid branches (connected, entry==exit, disconnected, no-entry,
      entry-without-exit continuous fallback).
    - Raises `RoutingProviderError` or `RouteContinuityError` with zero partial route on failure.
    """
    internal_started = time.perf_counter()
    graph_revision, entry_node, exit_node, network_path = _read_graph_snapshot(
        engine,
        origin,
        destination,
        config,
    )
    internal_query_ms = (time.perf_counter() - internal_started) * 1000.0

    # No DB transaction or connection is held beyond this point.
    labeled_segments: list[tuple[str, RouteSegment]] = []
    external_latency_ms = 0.0
    branch: RouteBranch

    if entry_node is None:
        branch = "no_entry_fallback"
        fallback_ext = provider.route(
            origin,
            destination,
            profile=config.fallback_profile,
        )
        external_latency_ms += fallback_ext.latency_ms
        labeled_segments.append(
            ("fallback", _external_result_to_segment(fallback_ext, section_name="fallback"))
        )
    elif exit_node is None:
        branch = "entry_no_exit_fallback"
        access_ext = provider.route(
            origin,
            entry_node.coord,
            profile=config.access_profile,
        )
        external_latency_ms += access_ext.latency_ms
        fallback_ext = provider.route(
            entry_node.coord,
            destination,
            profile=config.fallback_profile,
        )
        external_latency_ms += fallback_ext.latency_ms
        labeled_segments.append(
            ("access", _external_result_to_segment(access_ext, section_name="access"))
        )
        labeled_segments.append(
            ("fallback", _external_result_to_segment(fallback_ext, section_name="fallback"))
        )
    elif entry_node.node_id == exit_node.node_id:
        branch = "entry_equals_exit"
        access_ext = provider.route(
            origin,
            entry_node.coord,
            profile=config.access_profile,
        )
        external_latency_ms += access_ext.latency_ms
        exit_ext = provider.route(
            exit_node.coord,
            destination,
            profile=config.access_profile,
        )
        external_latency_ms += exit_ext.latency_ms
        labeled_segments.append(
            ("access", _external_result_to_segment(access_ext, section_name="access"))
        )
        labeled_segments.append(
            ("exit", _external_result_to_segment(exit_ext, section_name="exit"))
        )
    elif network_path is None:
        branch = "disconnected"
        access_ext = provider.route(
            origin,
            entry_node.coord,
            profile=config.access_profile,
        )
        external_latency_ms += access_ext.latency_ms
        connector_ext = provider.route(
            entry_node.coord,
            exit_node.coord,
            profile=config.access_profile,
        )
        external_latency_ms += connector_ext.latency_ms
        exit_ext = provider.route(
            exit_node.coord,
            destination,
            profile=config.access_profile,
        )
        external_latency_ms += exit_ext.latency_ms
        labeled_segments.append(
            ("access", _external_result_to_segment(access_ext, section_name="access"))
        )
        labeled_segments.append(
            (
                "connector",
                _external_result_to_segment(connector_ext, section_name="connector"),
            )
        )
        labeled_segments.append(
            ("exit", _external_result_to_segment(exit_ext, section_name="exit"))
        )
    else:
        branch = "connected"
        access_ext = provider.route(
            origin,
            entry_node.coord,
            profile=config.access_profile,
        )
        external_latency_ms += access_ext.latency_ms
        exit_ext = provider.route(
            exit_node.coord,
            destination,
            profile=config.access_profile,
        )
        external_latency_ms += exit_ext.latency_ms

        labeled_segments.append(
            ("access", _external_result_to_segment(access_ext, section_name="access"))
        )
        for idx, net_seg in enumerate(network_path.segments):
            labeled_segments.append((f"network[{idx}]", net_seg))
        labeled_segments.append(
            ("exit", _external_result_to_segment(exit_ext, section_name="exit"))
        )

    continuity_gaps_m = verify_route_continuity(
        labeled_segments,
        tolerance_m=config.continuity_tolerance_m,
    )
    segments = tuple(seg for _, seg in labeled_segments)
    endpoint_snap_gaps_m = (
        geodesic_distance_m(origin, segments[0].points[0]),
        geodesic_distance_m(destination, segments[-1].points[-1]),
    )

    return HybridRouteResult(
        origin=origin,
        destination=destination,
        branch=branch,
        segments=segments,
        entry_node=entry_node,
        exit_node=exit_node,
        cost_mode=config.cost_mode,
        algorithm=config.algorithm,
        graph_revision=graph_revision,
        internal_query_ms=internal_query_ms,
        external_latency_ms=external_latency_ms,
        continuity_gaps_m=continuity_gaps_m,
        endpoint_snap_gaps_m=endpoint_snap_gaps_m,
    )
