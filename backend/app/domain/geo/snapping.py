"""Transaction-locked PostGIS node snapping and unordered edge upsert."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.domain.geo.geodesy import geodesic_distance_m, reduce_track
from app.domain.geo.models import (
    ERROR_CODE_DEGENERATE_GEOMETRY,
    GeoParseError,
    IngestionResult,
    ParsedTrack,
    SnappedEdgeRef,
    SnappingConfig,
    TrackPoint,
    TrackSegment,
)

# 64-bit signed advisory lock key ("ONTRACK" in ASCII hex)
GRAPH_WRITER_ADVISORY_LOCK_KEY: int = 0x4F4E545241434B

_SETUP_TEMP_TABLES_SQL = """
CREATE TEMP TABLE IF NOT EXISTS _snap_cfg (
    snap_radius_m DOUBLE PRECISION NOT NULL,
    difficulty SMALLINT NOT NULL
) ON COMMIT DROP;

CREATE TEMP TABLE IF NOT EXISTS _snap_pts (
    ord INTEGER PRIMARY KEY,
    seg_idx INTEGER NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    ele REAL NULL
) ON COMMIT DROP;

CREATE TEMP TABLE IF NOT EXISTS _snap_node_out (
    ord INTEGER PRIMARY KEY,
    seg_idx INTEGER NOT NULL,
    node_id BIGINT NOT NULL,
    created BOOLEAN NOT NULL
) ON COMMIT DROP;

CREATE TEMP TABLE IF NOT EXISTS _snap_edge_out (
    ord INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    edge_id BIGINT NULL,
    source_node_id BIGINT NULL,
    target_node_id BIGINT NULL,
    length_m REAL NULL,
    created BOOLEAN NOT NULL
) ON COMMIT DROP;

DELETE FROM _snap_cfg;
DELETE FROM _snap_pts;
DELETE FROM _snap_node_out;
DELETE FROM _snap_edge_out;
"""

_SNAP_AND_UPSERT_PLPGSQL = """
DO $$
DECLARE
    v_radius DOUBLE PRECISION;
    v_difficulty SMALLINT;
    rec RECORD;
    v_pt_geom geometry(Point, 4326);
    v_pt_geog geography;
    v_node_id BIGINT;
    v_created BOOLEAN;
    v_prev_seg INTEGER := NULL;
    v_prev_node BIGINT := NULL;
    v_edge_ord INTEGER := 0;
    v_line geometry(LineString, 4326);
    v_len_m DOUBLE PRECISION;
    v_edge_id BIGINT;
    v_e_src BIGINT;
    v_e_tgt BIGINT;
    v_e_len REAL;
    v_edge_created BOOLEAN;
BEGIN
    SELECT snap_radius_m, difficulty
    INTO v_radius, v_difficulty
    FROM _snap_cfg
    LIMIT 1;

    -- Phase 1: Lock/write `nodes` in deterministic input order (seeing same-tx inserts)
    FOR rec IN
        SELECT ord, seg_idx, lat, lon, ele
        FROM _snap_pts
        ORDER BY ord ASC
    LOOP
        v_pt_geom := ST_SetSRID(ST_MakePoint(rec.lon, rec.lat), 4326);
        v_pt_geog := v_pt_geom::geography;
        v_node_id := NULL;

        SELECT n.id
        INTO v_node_id
        FROM nodes n
        WHERE ST_DWithin(n.geom::geography, v_pt_geog, v_radius)
        ORDER BY ST_Distance(n.geom::geography, v_pt_geog) ASC, n.id ASC
        LIMIT 1;

        IF v_node_id IS NULL THEN
            INSERT INTO nodes (geom, ele)
            VALUES (v_pt_geom, rec.ele)
            RETURNING id INTO v_node_id;
            v_created := TRUE;
        ELSE
            v_created := FALSE;
        END IF;

        INSERT INTO _snap_node_out (ord, seg_idx, node_id, created)
        VALUES (rec.ord, rec.seg_idx, v_node_id, v_created);
    END LOOP;

    -- Phase 2: Lock/write `edges` per segment without bridging across segments
    FOR rec IN
        SELECT ord, seg_idx, node_id
        FROM _snap_node_out
        ORDER BY ord ASC
    LOOP
        IF v_prev_seg IS NOT NULL AND rec.seg_idx = v_prev_seg THEN
            v_edge_ord := v_edge_ord + 1;

            IF v_prev_node = rec.node_id THEN
                INSERT INTO _snap_edge_out (
                    ord, status, edge_id, source_node_id, target_node_id, length_m, created
                )
                VALUES (v_edge_ord, 'self_loop', NULL, v_prev_node, rec.node_id, NULL, FALSE);
            ELSE
                SELECT ST_MakeLine(s.geom, t.geom),
                       ST_Length(ST_MakeLine(s.geom, t.geom)::geography)
                INTO v_line, v_len_m
                FROM nodes s, nodes t
                WHERE s.id = v_prev_node AND t.id = rec.node_id;

                IF v_len_m IS NULL OR (v_len_m::real) <= 0 THEN
                    INSERT INTO _snap_edge_out (
                        ord, status, edge_id, source_node_id, target_node_id, length_m, created
                    )
                    VALUES (v_edge_ord, 'zero_length', NULL, v_prev_node, rec.node_id, 0.0, FALSE);
                ELSE
                    v_edge_id := NULL;
                    INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
                    VALUES (v_prev_node, rec.node_id, v_line, v_len_m::real, v_difficulty)
                    ON CONFLICT (LEAST(source, target), GREATEST(source, target)) DO NOTHING
                    RETURNING id, source, target, length_m
                    INTO v_edge_id, v_e_src, v_e_tgt, v_e_len;

                    IF v_edge_id IS NOT NULL THEN
                        v_edge_created := TRUE;
                    ELSE
                        SELECT e.id, e.source, e.target, e.length_m
                        INTO v_edge_id, v_e_src, v_e_tgt, v_e_len
                        FROM edges e
                        WHERE LEAST(e.source, e.target) = LEAST(v_prev_node, rec.node_id)
                          AND GREATEST(e.source, e.target) = GREATEST(v_prev_node, rec.node_id);
                        v_edge_created := FALSE;
                    END IF;

                    INSERT INTO _snap_edge_out (
                        ord, status, edge_id, source_node_id, target_node_id, length_m, created
                    )
                    VALUES (
                        v_edge_ord, 'ok', v_edge_id, v_e_src, v_e_tgt, v_e_len, v_edge_created
                    );
                END IF;
            END IF;
        END IF;

        v_prev_seg := rec.seg_idx;
        v_prev_node := rec.node_id;
    END LOOP;
END $$;
"""


def build_normalized_multilinestring_wkt(segments: Sequence[TrackSegment]) -> str:
    """Build PostGIS SRID-4326 WKT `MULTILINESTRING` using `lon lat` coordinate order."""
    line_parts: list[str] = []
    for seg in segments:
        if len(seg.points) < 2:
            continue
        coords = ", ".join(f"{pt.lon:.7f} {pt.lat:.7f}" for pt in seg.points)
        line_parts.append(f"({coords})")
    if not line_parts:
        raise GeoParseError(
            ERROR_CODE_DEGENERATE_GEOMETRY,
            "Cannot build normalized MultiLineString from empty segments.",
        )
    return f"MULTILINESTRING({', '.join(line_parts)})"


def ingest_track_transaction(
    conn: Connection,
    track: ParsedTrack,
    config: SnappingConfig,
    *,
    difficulty: int = 1,
) -> IngestionResult:
    """Ingest a parsed track into PostGIS under the graph-writer advisory lock.

    Lock & write order strictly follows ARCH §4.8:
    1. `pg_advisory_xact_lock(GRAPH_WRITER_ADVISORY_LOCK_KEY)`
    2. `nodes` (ST_DWithin on `geom::geography`, `ORDER BY dist_m ASC, id ASC`, same-tx visible)
    3. `edges` (`ST_MakeLine`, `ST_Length(geom::geography)`, unordered reuse without overwrite)
    4. `tracks` (`ON CONFLICT (id) DO NOTHING`)
    5. `graph_metadata(revision)` incremented LAST.

    Raises `GeoParseError("DEGENERATE_GEOMETRY")` and rolls back the nested savepoint if
    zero usable edges remain after reduction and snapping.
    """
    if difficulty not in (1, 2, 3):
        raise ValueError("difficulty must be 1, 2, or 3.")

    start_time = time.perf_counter()
    reduced = reduce_track(track, config.point_simplification_distance_m)
    wkt = build_normalized_multilinestring_wkt(track.segments)

    ords: list[int] = []
    seg_idxs: list[int] = []
    lats: list[float] = []
    lons: list[float] = []
    eles: list[float | None] = []

    ord_counter = 0
    for seg in reduced.segments:
        for pt in seg.points:
            ord_counter += 1
            ords.append(ord_counter)
            seg_idxs.append(seg.index)
            lats.append(pt.lat)
            lons.append(pt.lon)
            eles.append(pt.ele if (pt.ele is not None and math.isfinite(pt.ele)) else None)

    db_queries = 0

    # 1. Acquire transaction-scoped graph-writer advisory lock first.
    conn.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": GRAPH_WRITER_ADVISORY_LOCK_KEY},
    )
    db_queries += 1

    # Wrap graph mutations in a savepoint so DEGENERATE_GEOMETRY never leaves partial nodes.
    with conn.begin_nested():
        conn.exec_driver_sql(_SETUP_TEMP_TABLES_SQL)
        db_queries += 1

        conn.execute(
            text("INSERT INTO _snap_cfg (snap_radius_m, difficulty) VALUES (:radius, :difficulty)"),
            {"radius": float(config.node_snap_radius_m), "difficulty": int(difficulty)},
        )
        db_queries += 1

        conn.execute(
            text(
                """
                INSERT INTO _snap_pts (ord, seg_idx, lat, lon, ele)
                SELECT * FROM unnest(
                    CAST(:ords AS integer[]),
                    CAST(:seg_idxs AS integer[]),
                    CAST(:lats AS double precision[]),
                    CAST(:lons AS double precision[]),
                    CAST(:eles AS real[])
                )
                """
            ),
            {
                "ords": ords,
                "seg_idxs": seg_idxs,
                "lats": lats,
                "lons": lons,
                "eles": eles,
            },
        )
        db_queries += 1

        # 2 & 3. Execute batched server-side node snapping then edge upsert.
        conn.exec_driver_sql(_SNAP_AND_UPSERT_PLPGSQL)
        db_queries += 1

        node_rows = conn.execute(
            text("SELECT seg_idx, node_id, created FROM _snap_node_out ORDER BY ord ASC")
        ).fetchall()
        db_queries += 1

        edge_rows = conn.execute(
            text(
                """
                SELECT status, edge_id, source_node_id, target_node_id, length_m, created
                FROM _snap_edge_out
                ORDER BY ord ASC
                """
            )
        ).fetchall()
        db_queries += 1

        nodes_created = 0
        nodes_reused = 0
        seg_node_map: dict[int, list[int]] = {}
        for row in node_rows:
            seg_idx = int(row[0])
            node_id = int(row[1])
            created = bool(row[2])
            if created:
                nodes_created += 1
            else:
                nodes_reused += 1
            seg_node_map.setdefault(seg_idx, []).append(node_id)

        edges_created = 0
        edges_reused = 0
        self_loops_skipped = 0
        zero_length_edges_skipped = 0
        edge_refs: list[SnappedEdgeRef] = []

        for row in edge_rows:
            status = str(row[0])
            if status == "self_loop":
                self_loops_skipped += 1
                continue
            if status == "zero_length":
                zero_length_edges_skipped += 1
                continue
            edge_id = int(row[1])
            src_id = int(row[2])
            tgt_id = int(row[3])
            length_m = float(row[4])
            created = bool(row[5])
            if created:
                edges_created += 1
            else:
                edges_reused += 1
            edge_refs.append(
                SnappedEdgeRef(
                    edge_id=edge_id,
                    source_node_id=src_id,
                    target_node_id=tgt_id,
                    length_m=length_m,
                    created=created,
                )
            )

        if not edge_refs:
            raise GeoParseError(
                ERROR_CODE_DEGENERATE_GEOMETRY,
                "No usable edges remain after point reduction and node snapping.",
            )

        # 4. Upsert canonical track record (ON CONFLICT DO NOTHING)
        conn.execute(
            text(
                """
                INSERT INTO tracks (id, normalized_geom)
                VALUES (:track_id, ST_GeomFromText(:wkt, 4326))
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"track_id": track.normalized_fingerprint, "wkt": wkt},
        )
        db_queries += 1

        # 5. Update graph_metadata(revision) strictly LAST
        rev_row = conn.execute(
            text(
                """
                UPDATE graph_metadata
                SET revision = revision + 1
                WHERE id = 1
                RETURNING revision
                """
            )
        ).fetchone()
        db_queries += 1
        if rev_row is None:
            raise RuntimeError("graph_metadata row (id=1) is missing.")
        graph_revision = int(rev_row[0])

    snapped_segments = tuple(
        tuple(seg_node_map.get(seg.index, ())) for seg in reduced.segments
    )
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    return IngestionResult(
        track_id=track.normalized_fingerprint,
        graph_revision=graph_revision,
        raw_point_count=track.raw_point_count,
        reduced_point_count=reduced.reduced_point_count,
        nodes_created=nodes_created,
        nodes_reused=nodes_reused,
        edges_created=edges_created,
        edges_reused=edges_reused,
        self_loops_skipped=self_loops_skipped,
        zero_length_edges_skipped=zero_length_edges_skipped,
        snapped_segments=snapped_segments,
        edges=tuple(edge_refs),
        elapsed_ms=elapsed_ms,
        db_queries_count=db_queries,
    )


@dataclass
class _InMemoryEdge:
    edge_id: int
    source: int
    target: int
    length_m: float
    initial_difficulty: int


@dataclass
class InMemoryGraph:
    """Pure-Python WGS84 reference graph mirroring PostGIS snapping semantics.

    Used for fast sandboxed unit testing and dry-run synthetic benchmarking;
    production and integration tests use `ingest_track_transaction` against PostGIS.
    """

    nodes: dict[int, TrackPoint] = field(default_factory=dict)
    edges: dict[tuple[int, int], _InMemoryEdge] = field(default_factory=dict)
    tracks: dict[str, str] = field(default_factory=dict)
    revision: int = 0
    _next_node_id: int = 1
    _next_edge_id: int = 1
    _grid_deg: float = 0.001
    _spatial_buckets: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    def _bucket_key(self, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / self._grid_deg), math.floor(lon / self._grid_deg))

    def add_node(self, pt: TrackPoint) -> int:
        node_id = self._next_node_id
        self._next_node_id += 1
        self.nodes[node_id] = pt
        key = self._bucket_key(pt.lat, pt.lon)
        self._spatial_buckets.setdefault(key, []).append(node_id)
        return node_id

    def find_nearest_node(self, pt: TrackPoint, radius_m: float) -> int | None:
        lat_deg_span = (radius_m / 110_574.0) + 1e-6
        cos_lat = max(math.cos(math.radians(pt.lat)), 1e-4)
        lon_deg_span = (radius_m / (111_320.0 * cos_lat)) + 1e-6

        min_bx = math.floor((pt.lat - lat_deg_span) / self._grid_deg)
        max_bx = math.floor((pt.lat + lat_deg_span) / self._grid_deg)
        min_by = math.floor((pt.lon - lon_deg_span) / self._grid_deg)
        max_by = math.floor((pt.lon + lon_deg_span) / self._grid_deg)

        best_id: int | None = None
        best_dist: float = math.inf

        for bx in range(min_bx, max_bx + 1):
            for by in range(min_by, max_by + 1):
                for candidate_id in self._spatial_buckets.get((bx, by), ()):
                    cand = self.nodes[candidate_id]
                    dist = geodesic_distance_m(pt.lat, pt.lon, cand.lat, cand.lon)
                    if dist <= radius_m and (
                        dist < best_dist
                        or (
                            dist == best_dist
                            and (best_id is None or candidate_id < best_id)
                        )
                    ):
                        best_dist = dist
                        best_id = candidate_id
        return best_id


def ingest_track_in_memory(
    graph: InMemoryGraph,
    track: ParsedTrack,
    config: SnappingConfig,
    *,
    difficulty: int = 1,
) -> IngestionResult:
    """Run exact WGS84 node snapping and unordered edge upsert against an `InMemoryGraph`."""
    if difficulty not in (1, 2, 3):
        raise ValueError("difficulty must be 1, 2, or 3.")

    start_time = time.perf_counter()
    reduced = reduce_track(track, config.point_simplification_distance_m)
    wkt = build_normalized_multilinestring_wkt(track.segments)

    # Snapshot state for atomic rollback on DEGENERATE_GEOMETRY
    prev_next_node = graph._next_node_id
    prev_next_edge = graph._next_edge_id
    created_node_ids: list[int] = []
    created_edge_keys: list[tuple[int, int]] = []

    nodes_created = 0
    nodes_reused = 0
    snapped_segments_list: list[tuple[int, ...]] = []

    for seg in reduced.segments:
        seg_ids: list[int] = []
        for pt in seg.points:
            existing_id = graph.find_nearest_node(pt, config.node_snap_radius_m)
            if existing_id is None:
                new_id = graph.add_node(pt)
                created_node_ids.append(new_id)
                nodes_created += 1
                seg_ids.append(new_id)
            else:
                nodes_reused += 1
                seg_ids.append(existing_id)
        snapped_segments_list.append(tuple(seg_ids))

    edges_created = 0
    edges_reused = 0
    self_loops_skipped = 0
    zero_length_edges_skipped = 0
    edge_refs: list[SnappedEdgeRef] = []

    for snapped_seg_ids in snapped_segments_list:
        for idx in range(len(snapped_seg_ids) - 1):
            src_id = snapped_seg_ids[idx]
            tgt_id = snapped_seg_ids[idx + 1]
            if src_id == tgt_id:
                self_loops_skipped += 1
                continue

            src_pt = graph.nodes[src_id]
            tgt_pt = graph.nodes[tgt_id]
            length_m = geodesic_distance_m(src_pt.lat, src_pt.lon, tgt_pt.lat, tgt_pt.lon)
            if length_m <= 0.0:
                zero_length_edges_skipped += 1
                continue

            key = (min(src_id, tgt_id), max(src_id, tgt_id))
            existing_edge = graph.edges.get(key)
            if existing_edge is not None:
                edges_reused += 1
                edge_refs.append(
                    SnappedEdgeRef(
                        edge_id=existing_edge.edge_id,
                        source_node_id=existing_edge.source,
                        target_node_id=existing_edge.target,
                        length_m=existing_edge.length_m,
                        created=False,
                    )
                )
            else:
                edge_id = graph._next_edge_id
                graph._next_edge_id += 1
                edge = _InMemoryEdge(
                    edge_id=edge_id,
                    source=src_id,
                    target=tgt_id,
                    length_m=length_m,
                    initial_difficulty=difficulty,
                )
                graph.edges[key] = edge
                created_edge_keys.append(key)
                edges_created += 1
                edge_refs.append(
                    SnappedEdgeRef(
                        edge_id=edge_id,
                        source_node_id=src_id,
                        target_node_id=tgt_id,
                        length_m=length_m,
                        created=True,
                    )
                )

    if not edge_refs:
        # Roll back created nodes/edges atomically
        for key in created_edge_keys:
            graph.edges.pop(key, None)
        for nid in created_node_ids:
            pt = graph.nodes.pop(nid)
            bkey = graph._bucket_key(pt.lat, pt.lon)
            bucket = graph._spatial_buckets.get(bkey)
            if bucket is not None and nid in bucket:
                bucket.remove(nid)
        graph._next_node_id = prev_next_node
        graph._next_edge_id = prev_next_edge
        raise GeoParseError(
            ERROR_CODE_DEGENERATE_GEOMETRY,
            "No usable edges remain after point reduction and node snapping.",
        )

    graph.tracks.setdefault(track.normalized_fingerprint, wkt)
    graph.revision += 1

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    return IngestionResult(
        track_id=track.normalized_fingerprint,
        graph_revision=graph.revision,
        raw_point_count=track.raw_point_count,
        reduced_point_count=reduced.reduced_point_count,
        nodes_created=nodes_created,
        nodes_reused=nodes_reused,
        edges_created=edges_created,
        edges_reused=edges_reused,
        self_loops_skipped=self_loops_skipped,
        zero_length_edges_skipped=zero_length_edges_skipped,
        snapped_segments=tuple(snapped_segments_list),
        edges=tuple(edge_refs),
        elapsed_ms=elapsed_ms,
        db_queries_count=0,
    )
