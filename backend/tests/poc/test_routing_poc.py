"""Unit and Testcontainers integration tests for T-004 (POC 2: Exact Full-Graph Routing)."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.integrations.routing.benchmark import (
    GraphScaleSpec,
    benchmark_concurrent_reads_during_writes,
    benchmark_graph_scale,
    compute_percentiles,
    explain_nearest_node,
    generate_grid_topology,
    seed_difficulty_neutrality_fixture,
    seed_outside_bbox_fixture,
    seed_synthetic_grid_graph,
)
from app.integrations.routing.cli import load_coordinate_pairs
from app.integrations.routing.cli import main as cli_main
from app.integrations.routing.graph_router import (
    NEAREST_NODE_SQL,
    build_shortest_path_sql,
    calculate_leg,
    find_nearest_node,
    find_shortest_path,
    orient_edge_points,
    verify_route_continuity,
)
from app.integrations.routing.models import (
    Coordinate,
    ExternalRouteResult,
    RouteContinuityError,
    RouteSegment,
    RoutingAlgorithm,
    RoutingConfig,
    RoutingCostMode,
    RoutingProviderError,
    geodesic_distance_m,
)
from app.integrations.routing.protocol import RoutingProvider
from app.integrations.routing.providers import (
    FakeRoutingProvider,
    GraphHopperProvider,
    OpenRouteServiceProvider,
    map_graphhopper_profile,
)
from tests.conftest import build_alembic_config

# ---------------------------------------------------------------------------
# Helpers for pure unit tests (fake DB engine + transaction lifecycle tracker)
# ---------------------------------------------------------------------------


class _FakeMappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeScalarResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappingResult:
        return _FakeMappingResult(self._rows)

    def first(self) -> tuple[Any, ...] | None:
        if not self._rows:
            return None
        return tuple(self._rows[0].values())


class _FakeTransaction:
    def __init__(self, tracker: _FakeDBEngine) -> None:
        self._tracker = tracker

    def __enter__(self) -> _FakeTransaction:
        self._tracker.tx_open_count += 1
        self._tracker.active_tx = True
        self._tracker.events.append("tx_begin")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._tracker.active_tx = False
        self._tracker.events.append("tx_end")


class _FakeConnection:
    def __init__(self, tracker: _FakeDBEngine) -> None:
        self._tracker = tracker

    def __enter__(self) -> _FakeConnection:
        self._tracker.active_conn = True
        self._tracker.events.append("conn_open")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._tracker.active_conn = False
        self._tracker.events.append("conn_close")

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self._tracker)

    def execute(
        self,
        statement: Any,
        params: dict[str, Any] | None = None,
    ) -> _FakeScalarResult:
        sql = str(statement)
        self._tracker.executed_sql.append((sql, params))
        if "SET TRANSACTION" in sql:
            self._tracker.events.append(f"sql:{sql.strip()}")
            return _FakeScalarResult([])
        if "FROM graph_metadata" in sql:
            return _FakeScalarResult([{"revision": self._tracker.revision}])
        if "ST_DWithin" in sql:
            assert params is not None
            return _FakeScalarResult(self._tracker.resolve_nearest(params))
        if "pgr_dijkstra" in sql or "pgr_bdDijkstra" in sql:
            assert params is not None
            return _FakeScalarResult(self._tracker.resolve_path(params))
        raise AssertionError(f"Unexpected SQL in unit test fake DB: {sql}")


class _FakeDBEngine:
    """Minimal SQLAlchemy Engine test double that records transaction/connection lifecycle."""

    def __init__(
        self,
        *,
        nearest_responses: list[dict[str, Any] | None],
        path_rows: list[dict[str, Any]] | None = None,
        revision: int = 7,
    ) -> None:
        self._nearest_queue = list(nearest_responses)
        self._path_rows = list(path_rows) if path_rows is not None else []
        self.revision = revision
        self.active_conn = False
        self.active_tx = False
        self.tx_open_count = 0
        self.events: list[str] = []
        self.executed_sql: list[tuple[str, dict[str, Any] | None]] = []

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self)

    def resolve_nearest(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._nearest_queue:
            return []
        item = self._nearest_queue.pop(0)
        return [item] if item is not None else []

    def resolve_path(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self._path_rows)


class _TxAssertingProvider(FakeRoutingProvider):
    """FakeRoutingProvider that asserts no DB transaction or connection is open during HTTP."""

    def __init__(self, fake_db: _FakeDBEngine, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fake_db = fake_db

    def route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        *,
        profile: str,
    ) -> ExternalRouteResult:
        assert not self._fake_db.active_tx, "DB transaction must be closed before HTTP calls!"
        assert not self._fake_db.active_conn, "DB connection must be released before HTTP calls!"
        self._fake_db.events.append(f"provider:{profile}")
        return super().route(origin, destination, profile=profile)


# ---------------------------------------------------------------------------
# Pure Unit Tests (zero Docker / network)
# ---------------------------------------------------------------------------


def test_models_and_config_validation() -> None:
    pt = Coordinate(lat=32.6, lng=35.0, ele=215.5)
    assert pt.to_dict() == {"lat": 32.6, "lng": 35.0, "ele": 215.5}
    assert Coordinate(lat=32.6, lng=35.0).to_dict() == {"lat": 32.6, "lng": 35.0}

    with pytest.raises(ValueError):
        Coordinate(lat=95.0, lng=35.0)
    with pytest.raises(ValueError):
        Coordinate(lat=32.6, lng=float("nan"))
    with pytest.raises(ValueError):
        Coordinate(lat=32.6, lng=35.0, ele=float("inf"))

    with pytest.raises(ValueError):
        RoutingConfig(close_radius_m=3000.0, far_radius_m=2000.0)
    with pytest.raises(ValueError):
        RoutingConfig(close_radius_m=0.0)
    with pytest.raises(ValueError):
        RoutingConfig(continuity_tolerance_m=-1.0)

    p1 = Coordinate(lat=32.6000, lng=35.0000)
    p2 = Coordinate(lat=32.6000, lng=35.0100)
    dist_m = geodesic_distance_m(p1, p2)
    assert 930.0 < dist_m < 950.0
    assert geodesic_distance_m(p1, p1) == 0.0

    # Segment invariants
    net_seg = RouteSegment(
        source="network",
        points=(p1, p2),
        edge_id=11,
        avg_difficulty=2.0,
        display_level=2,
    )
    assert net_seg.edge_id == 11
    with pytest.raises(ValueError):
        RouteSegment(source="network", points=(p1, p2))
    with pytest.raises(ValueError):
        RouteSegment(source="external", points=(p1, p2), edge_id=1, provider_profile="driving-car")
    with pytest.raises(ValueError):
        RouteSegment(
            source="external",
            points=(p1, p2),
            provider_profile="driving-car",
            display_level=1,
        )


def test_ors_provider_parses_geojson_retries_5xx_and_redacts_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "ors-super-secret-token-98765"
    attempts = 0
    sleep_calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.method == "POST"
        assert request.url.path == "/v2/directions/driving-car/geojson"
        assert request.headers["Authorization"] == secret_key
        body = json.loads(request.content.decode("utf-8"))
        assert body["coordinates"] == [[35.0, 32.6], [35.02, 32.61]]
        if attempts == 1:
            return httpx.Response(503, json={"error": "temporary overload"})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                [35.0, 32.6, 180.0],
                                [35.01, 32.605, 195.0],
                                [35.02, 32.61, 210.0],
                            ],
                        },
                        "properties": {"summary": {"distance": 2150.5, "duration": 240.0}},
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenRouteServiceProvider(
        api_key=secret_key,
        client=client,
        max_retries=2,
        retry_backoff_s=0.1,
        sleep_fn=sleep_calls.append,
    )
    assert isinstance(provider, RoutingProvider)
    assert secret_key not in repr(provider)

    with caplog.at_level(logging.WARNING):
        res = provider.route(
            Coordinate(lat=32.6, lng=35.0),
            Coordinate(lat=32.61, lng=35.02),
            profile="driving-car",
        )

    assert attempts == 2
    assert sleep_calls == [0.1]
    assert res.provider == "openrouteservice"
    assert res.profile == "driving-car"
    assert len(res.points) == 3
    assert res.points[0] == Coordinate(lat=32.6, lng=35.0, ele=180.0)
    assert res.distance_m == pytest.approx(2150.5)
    assert res.duration_s == pytest.approx(240.0)
    assert secret_key not in caplog.text


def test_ors_provider_429_does_not_retry_and_empty_geometry_fails() -> None:
    calls = 0

    def handler_429(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "quota exceeded"})

    provider_429 = OpenRouteServiceProvider(
        api_key="ors-key-429",
        client=httpx.Client(transport=httpx.MockTransport(handler_429)),
        max_retries=2,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(RoutingProviderError) as exc_info:
        provider_429.route(
            Coordinate(lat=32.6, lng=35.0),
            Coordinate(lat=32.61, lng=35.02),
            profile="driving-car",
        )
    assert calls == 1
    assert exc_info.value.code == "RATE_LIMITED"
    assert exc_info.value.status_code == 429
    assert exc_info.value.retryable is False

    def handler_empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    provider_empty = OpenRouteServiceProvider(
        api_key="ors-key-empty",
        client=httpx.Client(transport=httpx.MockTransport(handler_empty)),
        sleep_fn=lambda _: None,
    )
    with pytest.raises(RoutingProviderError) as empty_exc:
        provider_empty.route(
            Coordinate(lat=32.6, lng=35.0),
            Coordinate(lat=32.61, lng=35.02),
            profile="cycling-mountain",
        )
    assert empty_exc.value.code == "EMPTY_GEOMETRY"
    assert empty_exc.value.retryable is False


def test_graphhopper_provider_profile_mapping_timeout_retry_and_secret_redaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert map_graphhopper_profile("driving-car") == "car"
    assert map_graphhopper_profile("cycling-mountain") == "mtb"

    secret_key = "gh-secret-key-xyz-999"
    attempts = 0
    seen_profiles: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.params["key"] == secret_key
        payload = json.loads(request.content.decode("utf-8"))
        seen_profiles.append(payload["profile"])
        if attempts == 1:
            raise httpx.ReadTimeout(f"timeout hitting {request.url}")
        return httpx.Response(
            200,
            json={
                "paths": [
                    {
                        "distance": 1820.0,
                        "time": 180000,
                        "points": {
                            "type": "LineString",
                            "coordinates": [
                                [35.0, 32.6, 150.0],
                                [35.02, 32.61, 175.0],
                            ],
                        },
                    }
                ]
            },
        )

    provider = GraphHopperProvider(
        api_key=secret_key,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=2,
        retry_backoff_s=0.05,
        sleep_fn=lambda _: None,
    )
    assert secret_key not in repr(provider)

    with caplog.at_level(logging.WARNING):
        res = provider.route(
            Coordinate(lat=32.6, lng=35.0),
            Coordinate(lat=32.61, lng=35.02),
            profile="cycling-mountain",
        )

    assert attempts == 2
    assert seen_profiles == ["mtb", "mtb"]
    assert res.provider == "graphhopper"
    assert res.profile == "mtb"
    assert res.duration_s == pytest.approx(180.0)
    assert secret_key not in caplog.text

    # Exhausted timeout error must also never contain the query-param secret
    def always_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out at {request.url}")

    failing_gh = GraphHopperProvider(
        api_key=secret_key,
        client=httpx.Client(transport=httpx.MockTransport(always_timeout)),
        max_retries=1,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(RoutingProviderError) as timeout_exc:
        failing_gh.route(
            Coordinate(lat=32.6, lng=35.0),
            Coordinate(lat=32.61, lng=35.02),
            profile="driving-car",
        )
    assert timeout_exc.value.code == "PROVIDER_TIMEOUT"
    assert timeout_exc.value.retryable is True
    assert secret_key not in timeout_exc.value.message


def test_build_shortest_path_sql_exact_full_graph_and_difficulty_neutral() -> None:
    """Verify AC-30 and AC-78 structural SQL guarantees before hitting PostgreSQL."""
    for algorithm in ("pgr_dijkstra", "pgr_bdDijkstra"):
        for cost_mode in ("distance", "hops"):
            sql = build_shortest_path_sql(cost_mode=cost_mode, algorithm=algorithm)
            assert f"FROM {algorithm}(" in sql
            assert "directed := false" in sql
            assert "id::bigint AS id" in sql
            assert "source::bigint AS source" in sql
            assert "target::bigint AS target" in sql
            if cost_mode == "distance":
                assert "length_m::double precision AS cost" in sql
                assert "length_m::double precision AS reverse_cost" in sql
            else:
                assert "1.0::double precision AS cost" in sql
                assert "1.0::double precision AS reverse_cost" in sql

            # AC-78: zero bounding-box pruning predicates anywhere
            for forbidden_bbox in ("ST_MakeEnvelope", "ST_Intersects", "ST_Expand", "&&"):
                assert forbidden_bbox not in sql

            # AC-30: inner pgRouting edge query must NEVER reference difficulty or WHERE
            inner_start = sql.index(f"FROM {algorithm}(")
            inner_end = sql.index("directed := false")
            inner_section = sql[inner_start:inner_end]
            assert "difficulty" not in inner_section.lower()
            assert "where" not in inner_section.lower()

    assert "geom::geography" in NEAREST_NODE_SQL
    assert "ST_DWithin" in NEAREST_NODE_SQL
    assert "n.id ASC" in NEAREST_NODE_SQL


def test_orient_edge_points_reverses_and_assigns_endpoint_elevation() -> None:
    forward = orient_edge_points(
        [[35.0, 32.6], [35.01, 32.61]],
        reversed_traversal=False,
        source_ele=100.0,
        target_ele=250.0,
    )
    assert forward == (
        Coordinate(lat=32.6, lng=35.0, ele=100.0),
        Coordinate(lat=32.61, lng=35.01, ele=250.0),
    )

    reversed_pts = orient_edge_points(
        [[35.0, 32.6], [35.01, 32.61]],
        reversed_traversal=True,
        source_ele=100.0,
        target_ele=250.0,
    )
    assert reversed_pts == (
        Coordinate(lat=32.61, lng=35.01, ele=250.0),
        Coordinate(lat=32.6, lng=35.0, ele=100.0),
    )


def test_calculate_leg_closes_repeatable_read_tx_before_http_and_covers_all_5_branches() -> None:
    origin = Coordinate(lat=32.6000, lng=35.0000)
    dest = Coordinate(lat=32.6200, lng=35.0300)
    entry_row = {"id": 10, "lat": 32.6010, "lng": 35.0020, "ele": 150.0, "distance_m": 210.0}
    exit_row = {"id": 20, "lat": 32.6190, "lng": 35.0280, "ele": 220.0, "distance_m": 220.0}

    # 1. Connected branch (AC-24)
    connected_path_rows: list[dict[str, Any]] = [
        {
            "seq": 1,
            "node": 10,
            "edge": 101,
            "cost": 1200.0,
            "agg_cost": 0.0,
            "edge_source": 10,
            "edge_target": 20,
            "initial_difficulty": 3,
            "source_ele": 150.0,
            "target_ele": 220.0,
            "oriented_geojson": json.dumps(
                {"type": "LineString", "coordinates": [[35.0020, 32.6010], [35.0280, 32.6190]]}
            ),
        },
        {
            "seq": 2,
            "node": 20,
            "edge": -1,
            "cost": 0.0,
            "agg_cost": 1200.0,
            "edge_source": None,
            "edge_target": None,
            "initial_difficulty": None,
            "source_ele": None,
            "target_ele": None,
            "oriented_geojson": None,
        },
    ]
    fake_db = _FakeDBEngine(nearest_responses=[entry_row, exit_row], path_rows=connected_path_rows)
    provider = _TxAssertingProvider(fake_db)
    res = calculate_leg(fake_db, provider, origin, dest)  # type: ignore[arg-type]

    assert res.branch == "connected"
    assert res.graph_revision == 7
    assert fake_db.tx_open_count == 1
    assert "sql:SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in fake_db.events
    assert fake_db.events.index("conn_close") < fake_db.events.index("provider:driving-car")
    assert [s.source for s in res.segments] == ["external", "network", "external"]
    assert res.segments[1].edge_id == 101
    assert res.segments[1].avg_difficulty == 3.0
    assert res.segments[1].display_level == 3
    assert all(g == 0.0 for g in res.continuity_gaps_m)

    # 2. No-entry fallback (AC-26)
    fake_db_no_entry = _FakeDBEngine(nearest_responses=[None, None])
    prov_no_entry = _TxAssertingProvider(fake_db_no_entry)
    res_no_entry = calculate_leg(fake_db_no_entry, prov_no_entry, origin, dest)  # type: ignore[arg-type]
    assert res_no_entry.branch == "no_entry_fallback"
    assert len(res_no_entry.segments) == 1
    assert res_no_entry.segments[0].provider_profile == "cycling-mountain"

    # 3. Entry found, no exit -> continuous access + fallback (AC-27)
    fake_db_no_exit = _FakeDBEngine(nearest_responses=[entry_row, None, None])
    prov_no_exit = _TxAssertingProvider(fake_db_no_exit)
    res_no_exit = calculate_leg(fake_db_no_exit, prov_no_exit, origin, dest)  # type: ignore[arg-type]
    assert res_no_exit.branch == "entry_no_exit_fallback"
    assert [c.profile for c in prov_no_exit.calls] == ["driving-car", "cycling-mountain"]
    assert prov_no_exit.calls[0].destination == prov_no_exit.calls[1].origin
    assert res_no_exit.continuity_gaps_m == (0.0,)

    # 4. Disconnected components -> access + driving-car connector + exit (AC-28)
    fake_db_disc = _FakeDBEngine(nearest_responses=[entry_row, exit_row], path_rows=[])
    prov_disc = _TxAssertingProvider(fake_db_disc)
    res_disc = calculate_leg(fake_db_disc, prov_disc, origin, dest)  # type: ignore[arg-type]
    assert res_disc.branch == "disconnected"
    assert [c.profile for c in prov_disc.calls] == ["driving-car", "driving-car", "driving-car"]
    assert len(res_disc.segments) == 3

    # 5. Entry == Exit
    fake_db_same = _FakeDBEngine(nearest_responses=[entry_row, entry_row])
    prov_same = _TxAssertingProvider(fake_db_same)
    res_same = calculate_leg(fake_db_same, prov_same, origin, dest)  # type: ignore[arg-type]
    assert res_same.branch == "entry_equals_exit"
    assert [c.profile for c in prov_same.calls] == ["driving-car", "driving-car"]
    assert len(res_same.segments) == 2


@pytest.mark.parametrize("fault", ["429", "503", "timeout", "empty_geometry"])
def test_calculate_leg_zero_partial_route_on_provider_faults(fault: Any) -> None:
    """AC-29: Mid-calculation provider failure raises structured error with zero partial route."""
    origin = Coordinate(lat=32.6000, lng=35.0000)
    dest = Coordinate(lat=32.6200, lng=35.0300)
    entry_row = {"id": 10, "lat": 32.6010, "lng": 35.0020, "ele": None, "distance_m": 150.0}
    exit_row = {"id": 20, "lat": 32.6190, "lng": 35.0280, "ele": None, "distance_m": 150.0}

    fake_db = _FakeDBEngine(nearest_responses=[entry_row, exit_row], path_rows=[])
    # Fail on 2nd call (connector) after 1st call (access) succeeded
    provider = FakeRoutingProvider(fault=fault, fail_on_call=2)
    with pytest.raises(RoutingProviderError):
        calculate_leg(fake_db, provider, origin, dest)  # type: ignore[arg-type]
    assert len(provider.calls) == 2


def test_continuity_verification_rejects_gap_exceeding_tolerance() -> None:
    origin = Coordinate(lat=32.6000, lng=35.0000)
    dest = Coordinate(lat=32.6200, lng=35.0300)
    entry_row = {"id": 10, "lat": 32.6010, "lng": 35.0020, "ele": None, "distance_m": 150.0}
    fake_db = _FakeDBEngine(nearest_responses=[entry_row, None, None])
    provider = FakeRoutingProvider(fault="continuity_gap", fail_on_call=1)

    with pytest.raises(RouteContinuityError) as exc_info:
        calculate_leg(
            fake_db,  # type: ignore[arg-type]
            provider,
            origin,
            dest,
            config=RoutingConfig(continuity_tolerance_m=150.0),
        )
    assert exc_info.value.gap_m > 150.0
    assert exc_info.value.between_sections == "access->fallback"

    # Direct verify_route_continuity sanity check
    seg_a = RouteSegment(
        source="external",
        points=(Coordinate(32.6, 35.0), Coordinate(32.601, 35.001)),
        provider_profile="driving-car",
    )
    seg_b = RouteSegment(
        source="external",
        points=(Coordinate(32.601, 35.001), Coordinate(32.605, 35.005)),
        provider_profile="cycling-mountain",
    )
    assert verify_route_continuity([("a", seg_a), ("b", seg_b)], tolerance_m=10.0) == (0.0,)


def test_benchmark_helpers_and_cli_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stats = compute_percentiles([10.0, 20.0, 30.0, 40.0, 50.0])
    assert stats.count == 5
    assert stats.min_ms == 10.0
    assert stats.p50_ms == 30.0
    assert stats.p95_ms == pytest.approx(48.0)
    assert stats.max_ms == 50.0

    coords, edges = generate_grid_topology(4, 5)
    assert len(coords) == 20
    assert len(edges) == 4 * 4 + 3 * 5
    assert all(src != tgt and 1 <= diff <= 3 for src, tgt, diff in edges)

    # CLI end-to-end with fake provider and snap-gap recording
    pairs_file = tmp_path / "pairs.json"
    out_file = tmp_path / "report.json"
    pairs_file.write_text(
        json.dumps(
            [
                {
                    "name": "carmel-leg",
                    "origin": {"lat": 32.70, "lng": 35.02},
                    "destination": {"lat": 32.58, "lng": 35.18},
                }
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_coordinate_pairs(pairs_file)
    assert len(loaded) == 1 and loaded[0].name == "carmel-leg"

    secret_val = "do-not-leak-in-cli-output"
    monkeypatch.setenv("ORS_API_KEY", secret_val)
    monkeypatch.setenv("GRAPHHOPPER_API_KEY", secret_val)

    exit_code = cli_main(
        [
            "--input",
            str(pairs_file),
            "--providers",
            "fake",
            "--profiles",
            "driving-car,cycling-mountain",
            "--output-json",
            str(out_file),
        ]
    )
    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert secret_val not in stdout
    report = json.loads(out_file.read_text(encoding="utf-8"))
    assert secret_val not in json.dumps(report)
    assert report["summary"]["fake"]["total_requests"] == 2
    assert report["summary"]["fake"]["succeeded"] == 2
    assert report["records"][0]["origin_snap_gap_m"] == 0.0
    assert report["records"][0]["destination_snap_gap_m"] == 0.0


def test_qa_ors_and_graphhopper_error_classification_retry_exhaustion_and_redaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """QA audit: 4xx/429 non-retryable, 5xx/ConnectError retry backoff & exhaustion, no leaks."""
    origin = Coordinate(lat=32.6000, lng=35.0000)
    dest = Coordinate(lat=32.6100, lng=35.0200)

    # 1. Non-retryable HTTP statuses (400, 401, 403, 429) on both ORS and GraphHopper
    for status_code, expected_code in (
        (400, "PROVIDER_CLIENT_ERROR"),
        (401, "AUTH_ERROR"),
        (403, "AUTH_ERROR"),
        (429, "RATE_LIMITED"),
    ):
        ors_calls = 0
        gh_calls = 0
        secret_ors = f"ors-secret-{status_code}"
        secret_gh = f"gh-secret-{status_code}"

        def make_handler(code: int, counter_box: list[int]) -> Any:
            def _handler(request: httpx.Request) -> httpx.Response:
                counter_box[0] += 1
                return httpx.Response(code, json={"error": f"err-{code}"})

            return _handler

        ors_box = [ors_calls]
        gh_box = [gh_calls]
        ors = OpenRouteServiceProvider(
            api_key=secret_ors,
            client=httpx.Client(transport=httpx.MockTransport(make_handler(status_code, ors_box))),
            max_retries=2,
            sleep_fn=lambda _: None,
        )
        gh = GraphHopperProvider(
            api_key=secret_gh,
            client=httpx.Client(transport=httpx.MockTransport(make_handler(status_code, gh_box))),
            max_retries=2,
            sleep_fn=lambda _: None,
        )

        with caplog.at_level(logging.WARNING):
            with pytest.raises(RoutingProviderError) as ors_exc:
                ors.route(origin, dest, profile="driving-car")
            with pytest.raises(RoutingProviderError) as gh_exc:
                gh.route(origin, dest, profile="driving-car")

        assert ors_box[0] == 1
        assert gh_box[0] == 1
        assert ors_exc.value.code == expected_code
        assert ors_exc.value.status_code == status_code
        assert ors_exc.value.retryable is False
        assert gh_exc.value.code == expected_code
        assert gh_exc.value.status_code == status_code
        assert gh_exc.value.retryable is False
        assert secret_ors not in str(ors_exc.value)
        assert secret_gh not in str(gh_exc.value)
        assert secret_ors not in caplog.text
        assert secret_gh not in caplog.text

    # 2. 502 retry exhaustion with exponential backoff [0.1, 0.2] (total 3 attempts)
    sleeps: list[float] = []
    attempts_502 = 0

    def always_502(request: httpx.Request) -> httpx.Response:
        nonlocal attempts_502
        attempts_502 += 1
        return httpx.Response(502, text="Bad Gateway")

    ors_502 = OpenRouteServiceProvider(
        api_key="ors-secret-502",
        client=httpx.Client(transport=httpx.MockTransport(always_502)),
        max_retries=2,
        retry_backoff_s=0.1,
        sleep_fn=sleeps.append,
    )
    with pytest.raises(RoutingProviderError) as exc_502:
        ors_502.route(origin, dest, profile="driving-car")
    assert attempts_502 == 3
    assert sleeps == pytest.approx([0.1, 0.2])
    assert exc_502.value.code == "PROVIDER_UNAVAILABLE"
    assert exc_502.value.status_code == 502
    assert exc_502.value.retryable is True

    # 3. Transport/ConnectError retry exhaustion with secret redaction on GraphHopper
    gh_secret_net = "gh-secret-connect-err-123"
    net_attempts = 0

    def always_connect_err(request: httpx.Request) -> httpx.Response:
        nonlocal net_attempts
        net_attempts += 1
        raise httpx.ConnectError(f"failed connecting to {request.url}")

    gh_net = GraphHopperProvider(
        api_key=gh_secret_net,
        client=httpx.Client(transport=httpx.MockTransport(always_connect_err)),
        max_retries=2,
        retry_backoff_s=0.0,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(RoutingProviderError) as net_exc:
        gh_net.route(origin, dest, profile="cycling-mountain")
    assert net_attempts == 3
    assert net_exc.value.code == "PROVIDER_NETWORK_ERROR"
    assert net_exc.value.retryable is True
    assert net_exc.value.__context__ is None
    assert gh_secret_net not in str(net_exc.value)

    # 4. GraphHopper malformed payloads: empty paths, invalid JSON, out-of-range coords
    for payload, expected_err in (
        (httpx.Response(200, json={"paths": []}), "EMPTY_GEOMETRY"),
        (httpx.Response(200, text="not-json"), "INVALID_RESPONSE"),
        (
            httpx.Response(
                200,
                json={"paths": [{"points": {"coordinates": [[35.0, 99.0], [35.1, 32.6]]}}]},
            ),
            "INVALID_RESPONSE",
        ),
    ):
        gh_bad = GraphHopperProvider(
            api_key="gh-key-bad",
            client=httpx.Client(transport=httpx.MockTransport(lambda _r, resp=payload: resp)),
            sleep_fn=lambda _: None,
        )
        with pytest.raises(RoutingProviderError) as bad_exc:
            gh_bad.route(origin, dest, profile="driving-car")
        assert bad_exc.value.code == expected_err
        assert bad_exc.value.retryable is False


def test_qa_calculate_leg_continuity_and_mid_faults_across_branches() -> None:
    """QA audit: continuity rejection and zero partial route across connected & disconnected."""
    origin = Coordinate(lat=32.6000, lng=35.0000)
    dest = Coordinate(lat=32.6200, lng=35.0300)
    entry_row = {"id": 10, "lat": 32.6010, "lng": 35.0020, "ele": 150.0, "distance_m": 210.0}
    exit_row = {"id": 20, "lat": 32.6190, "lng": 35.0280, "ele": 220.0, "distance_m": 220.0}
    connected_path_rows: list[dict[str, Any]] = [
        {
            "seq": 1,
            "node": 10,
            "edge": 101,
            "cost": 1200.0,
            "agg_cost": 0.0,
            "edge_source": 10,
            "edge_target": 20,
            "initial_difficulty": 2,
            "source_ele": 150.0,
            "target_ele": 220.0,
            "oriented_geojson": json.dumps(
                {"type": "LineString", "coordinates": [[35.0020, 32.6010], [35.0280, 32.6190]]}
            ),
        },
        {
            "seq": 2,
            "node": 20,
            "edge": -1,
            "cost": 0.0,
            "agg_cost": 1200.0,
            "edge_source": None,
            "edge_target": None,
            "initial_difficulty": None,
            "source_ele": None,
            "target_ele": None,
            "oriented_geojson": None,
        },
    ]

    # Connected branch: access->network[0] gap exceeds tolerance
    db_gap_conn = _FakeDBEngine(
        nearest_responses=[entry_row, exit_row],
        path_rows=connected_path_rows,
    )
    with pytest.raises(RouteContinuityError) as conn_gap_exc:
        calculate_leg(
            db_gap_conn,  # type: ignore[arg-type]
            FakeRoutingProvider(fault="continuity_gap", fail_on_call=1),
            origin,
            dest,
            config=RoutingConfig(continuity_tolerance_m=150.0),
        )
    assert conn_gap_exc.value.between_sections == "access->network[0]"

    # Connected branch: exit leg fails with 429 after access succeeded -> zero partial route
    db_fault_conn = _FakeDBEngine(
        nearest_responses=[entry_row, exit_row],
        path_rows=connected_path_rows,
    )
    prov_fault_conn = FakeRoutingProvider(fault="429", fail_on_call=2)
    with pytest.raises(RoutingProviderError) as exit_fault_exc:
        calculate_leg(db_fault_conn, prov_fault_conn, origin, dest)  # type: ignore[arg-type]
    assert exit_fault_exc.value.status_code == 429
    assert len(prov_fault_conn.calls) == 2

    # Disconnected branch: access->connector continuity gap rejected
    db_gap_disc = _FakeDBEngine(nearest_responses=[entry_row, exit_row], path_rows=[])
    with pytest.raises(RouteContinuityError) as disc_gap_exc:
        calculate_leg(
            db_gap_disc,  # type: ignore[arg-type]
            FakeRoutingProvider(fault="continuity_gap", fail_on_call=1),
            origin,
            dest,
        )
    assert disc_gap_exc.value.between_sections == "access->connector"

    # Entry-no-exit branch: fallback fails after access succeeded -> zero partial route
    db_fault_ne = _FakeDBEngine(nearest_responses=[entry_row, None, None])
    prov_fault_ne = FakeRoutingProvider(fault="503", fail_on_call=2)
    with pytest.raises(RoutingProviderError) as ne_exc:
        calculate_leg(db_fault_ne, prov_fault_ne, origin, dest)  # type: ignore[arg-type]
    assert ne_exc.value.status_code == 503
    assert len(prov_fault_ne.calls) == 2


# ---------------------------------------------------------------------------
# Integration Tests (Real PostGIS + pgRouting via Testcontainers)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def routing_db_engine(postgres_db_url: str) -> Generator[Engine, None, None]:
    """Run Alembic migrations once per module against the PostGIS/pgRouting container."""
    cfg = build_alembic_config(postgres_db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(postgres_db_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


def _reset_graph(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE edges, nodes RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                "INSERT INTO graph_metadata (id, revision) VALUES (1, 0) "
                "ON CONFLICT (id) DO UPDATE SET revision = 0"
            )
        )


def _insert_node(conn: Any, coord: Coordinate) -> int:
    node_id = conn.execute(
        text(
            "INSERT INTO nodes (geom, ele) "
            "VALUES (ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), :ele) "
            "RETURNING id"
        ),
        {"lng": coord.lng, "lat": coord.lat, "ele": coord.ele},
    ).scalar_one()
    return int(node_id)


def _insert_edge(conn: Any, src: int, tgt: int, difficulty: int = 1) -> int:
    edge_id = conn.execute(
        text(
            """
            INSERT INTO edges (source, target, geom, length_m, initial_difficulty)
            SELECT
                :src,
                :tgt,
                ST_MakeLine(ns.geom, nt.geom),
                ST_Length(ST_MakeLine(ns.geom, nt.geom)::geography)::real,
                :diff
            FROM nodes AS ns, nodes AS nt
            WHERE ns.id = :src AND nt.id = :tgt
            RETURNING id
            """
        ),
        {"src": src, "tgt": tgt, "diff": difficulty},
    ).scalar_one()
    return int(edge_id)


@pytest.mark.integration
def test_integration_ac24_ac25_connected_close_far_and_tie_break(
    routing_db_engine: Engine,
) -> None:
    """AC-24 & AC-25: Connected route, CLOSE->FAR expansion, reversed edge orientation, id tie."""
    _reset_graph(routing_db_engine)
    n1_coord = Coordinate(lat=32.6000, lng=35.0000, ele=100.0)
    n2_coord = Coordinate(lat=32.6000, lng=35.0100, ele=150.0)
    n3_coord = Coordinate(lat=32.6000, lng=35.0200, ele=200.0)
    # Two nodes equidistant from (32.6000, 35.0000) to verify deterministic id ASC tie-break
    tie_a_coord = Coordinate(lat=32.7000, lng=35.1000, ele=110.0)
    tie_b_coord = Coordinate(lat=32.7000, lng=35.1200, ele=120.0)

    with routing_db_engine.begin() as conn:
        n1 = _insert_node(conn, n1_coord)
        n2 = _insert_node(conn, n2_coord)
        n3 = _insert_node(conn, n3_coord)
        # Insert e1 in forward direction (n1->n2) and e2 in REVERSE stored direction (n3->n2)
        e1 = _insert_edge(conn, n1, n2, difficulty=1)
        e2 = _insert_edge(conn, n3, n2, difficulty=3)
        tie_a = _insert_node(conn, tie_a_coord)
        tie_b = _insert_node(conn, tie_b_coord)
        conn.execute(text("ANALYZE nodes"))
        conn.execute(text("ANALYZE edges"))

    # Tie-break test: midpoint between tie_a and tie_b must pick lower id (tie_a)
    with routing_db_engine.connect() as conn:
        midpoint = Coordinate(lat=32.7000, lng=35.1100)
        tie_match = find_nearest_node(conn, midpoint, close_radius_m=2000.0, far_radius_m=10000.0)
        assert tie_match is not None
        assert tie_match.node_id == min(tie_a, tie_b)

    # AC-24: Start & Finish within CLOSE radius (2km)
    provider = FakeRoutingProvider()
    res_close = calculate_leg(
        routing_db_engine,
        provider,
        origin=Coordinate(lat=32.5980, lng=34.9990),
        destination=Coordinate(lat=32.6020, lng=35.0210),
    )
    assert res_close.branch == "connected"
    assert res_close.entry_node is not None and res_close.entry_node.search_tier == "close"
    assert res_close.exit_node is not None and res_close.exit_node.search_tier == "close"
    assert [s.source for s in res_close.segments] == ["external", "network", "network", "external"]
    assert [s.edge_id for s in res_close.segments if s.source == "network"] == [e1, e2]
    # Reversed stored edge (n3->n2) must be oriented n2 -> n3
    assert res_close.segments[2].points[0] == n2_coord
    assert res_close.segments[2].points[-1] == n3_coord
    assert res_close.segments[2].avg_difficulty == 3.0
    assert res_close.segments[2].display_level == 3
    assert all(g <= 0.01 for g in res_close.continuity_gaps_m)

    # AC-25: Start outside CLOSE (2km) but inside FAR (10km) -> ~4.4km south of n1
    res_far = calculate_leg(
        routing_db_engine,
        FakeRoutingProvider(),
        origin=Coordinate(lat=32.5600, lng=35.0000),
        destination=Coordinate(lat=32.6010, lng=35.0205),
        config=RoutingConfig(close_radius_m=2000.0, far_radius_m=10000.0),
    )
    assert res_far.branch == "connected"
    assert res_far.entry_node is not None
    assert res_far.entry_node.node_id == n1
    assert res_far.entry_node.search_tier == "far"
    assert 2000.0 < res_far.entry_node.distance_m <= 10000.0


@pytest.mark.integration
def test_integration_ac26_ac27_ac28_ac29_all_fallback_disconnected_and_faults(
    routing_db_engine: Engine,
) -> None:
    """AC-26, AC-27, AC-28, AC-29, and entry==exit against real PostGIS/pgRouting."""
    _reset_graph(routing_db_engine)
    with routing_db_engine.begin() as conn:
        # Component A near (32.60, 35.00)
        a1 = _insert_node(conn, Coordinate(32.6000, 35.0000, 100.0))
        a2 = _insert_node(conn, Coordinate(32.6000, 35.0050, 110.0))
        _insert_edge(conn, a1, a2, difficulty=1)
        # Disconnected Component B near (32.63, 35.05)
        b1 = _insert_node(conn, Coordinate(32.6300, 35.0500, 200.0))
        b2 = _insert_node(conn, Coordinate(32.6300, 35.0550, 210.0))
        _insert_edge(conn, b1, b2, difficulty=2)

    # AC-26: No entry within FAR -> full fallback Start -> Finish
    prov_26 = FakeRoutingProvider()
    r26 = calculate_leg(
        routing_db_engine,
        prov_26,
        origin=Coordinate(31.5000, 34.5000),
        destination=Coordinate(31.6000, 34.6000),
    )
    assert r26.branch == "no_entry_fallback"
    assert len(r26.segments) == 1
    assert r26.segments[0].provider_profile == "cycling-mountain"

    # AC-27: Entry found near a1, no exit within FAR -> driving access + fallback continuously
    prov_27 = FakeRoutingProvider()
    r27 = calculate_leg(
        routing_db_engine,
        prov_27,
        origin=Coordinate(32.5990, 34.9990),
        destination=Coordinate(31.5000, 34.5000),
    )
    assert r27.branch == "entry_no_exit_fallback"
    assert [s.provider_profile for s in r27.segments] == ["driving-car", "cycling-mountain"]
    assert all(g <= 0.01 for g in r27.continuity_gaps_m)

    # AC-28: Disconnected components -> access + driving-car connector + exit
    prov_28 = FakeRoutingProvider()
    r28 = calculate_leg(
        routing_db_engine,
        prov_28,
        origin=Coordinate(32.5990, 34.9990),
        destination=Coordinate(32.6310, 35.0560),
    )
    assert r28.branch == "disconnected"
    assert [s.provider_profile for s in r28.segments] == [
        "driving-car",
        "driving-car",
        "driving-car",
    ]
    assert all(g <= 0.01 for g in r28.continuity_gaps_m)

    # Entry == Exit
    r_same = calculate_leg(
        routing_db_engine,
        FakeRoutingProvider(),
        origin=Coordinate(32.5995, 34.9995),
        destination=Coordinate(32.6005, 35.0005),
    )
    assert r_same.branch == "entry_equals_exit"
    assert len(r_same.segments) == 2

    # AC-29: 429/503/empty_geometry -> error, zero partial route
    for fault in ("429", "503", "empty_geometry"):
        with pytest.raises(RoutingProviderError):
            calculate_leg(
                routing_db_engine,
                FakeRoutingProvider(fault=fault),
                origin=Coordinate(32.5990, 34.9990),
                destination=Coordinate(32.6005, 35.0055),
            )


@pytest.mark.integration
def test_integration_ac30_difficulty_neutral_route_selection(
    routing_db_engine: Engine,
) -> None:
    """AC-30: Changing edge difficulty from 1 to 3 never alters the chosen geometric path."""
    with routing_db_engine.begin() as conn:
        fixture = seed_difficulty_neutrality_fixture(conn, truncate=True)

    before = calculate_leg(
        routing_db_engine,
        FakeRoutingProvider(),
        origin=Coordinate(fixture.start_coord.lat - 0.0005, fixture.start_coord.lng),
        destination=Coordinate(fixture.end_coord.lat - 0.0005, fixture.end_coord.lng),
    )
    before_net_edges = [s.edge_id for s in before.segments if s.source == "network"]
    assert tuple(before_net_edges) == fixture.shorter_path_edge_ids
    assert all(s.display_level == 1 for s in before.segments if s.source == "network")

    # Raise difficulty of optimal path from 1 to 3 without modifying geometry
    with routing_db_engine.begin() as conn:
        conn.execute(
            text("UPDATE edges SET initial_difficulty = 3 WHERE id = ANY(CAST(:ids AS bigint[]))"),
            {"ids": list(fixture.shorter_path_edge_ids)},
        )

    after = calculate_leg(
        routing_db_engine,
        FakeRoutingProvider(),
        origin=Coordinate(fixture.start_coord.lat - 0.0005, fixture.start_coord.lng),
        destination=Coordinate(fixture.end_coord.lat - 0.0005, fixture.end_coord.lng),
    )
    after_net_edges = [s.edge_id for s in after.segments if s.source == "network"]
    assert after_net_edges == before_net_edges
    assert all(s.display_level == 3 for s in after.segments if s.source == "network")


@pytest.mark.integration
def test_integration_ac78_optimal_detour_outside_bbox_distance_and_hops(
    routing_db_engine: Engine,
) -> None:
    """AC-78: Exact full-graph routing returns globally optimal detour outside Start/Finish bbox."""
    with routing_db_engine.begin() as conn:
        fixture = seed_outside_bbox_fixture(conn, truncate=True)

    min_lat, _, max_lat, _ = fixture.tight_bbox
    assert fixture.detour_apex_coord.lat > max_lat

    for cost_mode in ("distance", "hops"):
        costs_by_algo: dict[str, float] = {}
        for algo in ("pgr_dijkstra", "pgr_bdDijkstra"):
            with routing_db_engine.connect() as conn:
                path = find_shortest_path(
                    conn,
                    fixture.start_node_id,
                    fixture.end_node_id,
                    cost_mode=cost_mode,
                    algorithm=algo,
                )
            assert path is not None
            assert path.edge_ids == fixture.detour_edge_ids
            assert fixture.detour_apex_node_id in path.node_ids
            costs_by_algo[algo] = path.total_cost

        assert costs_by_algo["pgr_dijkstra"] == pytest.approx(costs_by_algo["pgr_bdDijkstra"])

        # Also verify through calculate_leg end-to-end
        leg = calculate_leg(
            routing_db_engine,
            FakeRoutingProvider(),
            origin=Coordinate(fixture.start_coord.lat, fixture.start_coord.lng - 0.0005),
            destination=Coordinate(fixture.end_coord.lat, fixture.end_coord.lng + 0.0005),
            config=RoutingConfig(cost_mode=cost_mode),
        )
        assert leg.branch == "connected"
        assert tuple(s.edge_id for s in leg.segments if s.source == "network") == (
            fixture.detour_edge_ids
        )
        assert any(pt.lat > max_lat for pt in leg.flattened_points())
        assert min_lat < max_lat


@pytest.mark.integration
def test_integration_ac62_concurrent_reads_during_writes_and_scale_benchmark(
    routing_db_engine: Engine,
) -> None:
    """AC-62 + POC 2 benchmark: 1x/5x/10x graphs, EXPLAIN ANALYZE, parity, concurrent reads."""
    specs = (
        GraphScaleSpec(label="1x", rows=10, cols=10),
        GraphScaleSpec(label="5x", rows=22, cols=22),
        GraphScaleSpec(label="10x", rows=32, cols=32),
    )

    last_seeded = None
    for spec in specs:
        with routing_db_engine.begin() as conn:
            seeded = seed_synthetic_grid_graph(conn, spec, truncate=True)
            last_seeded = seeded
            knn_plan = explain_nearest_node(conn, Coordinate(lat=32.605, lng=35.005))
            assert any("ix_nodes_geom_geog" in line for line in knn_plan)

        results = benchmark_graph_scale(
            routing_db_engine,
            seeded=seeded,
            algorithms=("pgr_dijkstra", "pgr_bdDijkstra"),
            cost_modes=("distance", "hops"),
        )
        assert len(results) == 4
        # Verify exact cost equivalence between pgr_dijkstra and pgr_bdDijkstra
        for mode in ("distance", "hops"):
            dijk = next(r for r in results if r.algorithm == "pgr_dijkstra" and r.cost_mode == mode)
            bd = next(r for r in results if r.algorithm == "pgr_bdDijkstra" and r.cost_mode == mode)
            assert dijk.pair_costs == pytest.approx(bd.pair_costs)
            assert len(dijk.explain_plan) > 0
            assert len(bd.explain_plan) > 0

    assert last_seeded is not None
    conc = benchmark_concurrent_reads_during_writes(
        routing_db_engine,
        pairs=last_seeded.sample_pairs[:3],
        writer_batches=3,
        nodes_per_batch=8,
        writer_hold_s=0.02,
    )
    assert conc.writes_committed == 3
    assert conc.reads_completed >= 3
    assert conc.reader_errors == ()
    assert conc.partial_snapshot_violations == 0


@pytest.mark.integration
def test_integration_qa_ac25_exit_far_hops_vs_distance_and_ac30_all_modes(
    routing_db_engine: Engine,
) -> None:
    """QA integration: asymmetric CLOSE/FAR exit, hops vs distance divergence, AC-30 all modes."""
    _reset_graph(routing_db_engine)
    # Build two competing paths between n_start and n_end:
    # - Path A: 2 hops via far north apex (~4.3 km)
    # - Path B: 3 hops nearly straight (~1.9 km)
    n_start_c = Coordinate(lat=32.6000, lng=35.0000, ele=100.0)
    n_end_c = Coordinate(lat=32.6000, lng=35.0200, ele=120.0)
    # Path A: 2 hops, long detour (~4.3 km)
    apex_far_c = Coordinate(lat=32.6180, lng=35.0100, ele=250.0)
    # Path B: 3 hops, nearly straight (~1.9 km)
    mid1_c = Coordinate(lat=32.6001, lng=35.0067, ele=105.0)
    mid2_c = Coordinate(lat=32.6001, lng=35.0133, ele=110.0)

    with routing_db_engine.begin() as conn:
        n_start = _insert_node(conn, n_start_c)
        n_end = _insert_node(conn, n_end_c)
        n_apex = _insert_node(conn, apex_far_c)
        n_m1 = _insert_node(conn, mid1_c)
        n_m2 = _insert_node(conn, mid2_c)
        e_a1 = _insert_edge(conn, n_start, n_apex, difficulty=1)
        e_a2 = _insert_edge(conn, n_apex, n_end, difficulty=1)
        e_b1 = _insert_edge(conn, n_start, n_m1, difficulty=1)
        e_b2 = _insert_edge(conn, n_m1, n_m2, difficulty=1)
        e_b3 = _insert_edge(conn, n_m2, n_end, difficulty=1)
        conn.execute(text("ANALYZE nodes"))
        conn.execute(text("ANALYZE edges"))

    # Asymmetric CLOSE entry + FAR exit (destination ~4.4 km east of n_end)
    leg_far_exit = calculate_leg(
        routing_db_engine,
        FakeRoutingProvider(),
        origin=Coordinate(lat=32.5995, lng=34.9995),
        destination=Coordinate(lat=32.6000, lng=35.0670),
        config=RoutingConfig(close_radius_m=2000.0, far_radius_m=10000.0, cost_mode="distance"),
    )
    assert leg_far_exit.branch == "connected"
    assert leg_far_exit.entry_node is not None and leg_far_exit.entry_node.search_tier == "close"
    assert leg_far_exit.exit_node is not None and leg_far_exit.exit_node.search_tier == "far"
    assert leg_far_exit.exit_node.node_id == n_end
    assert 2000.0 < leg_far_exit.exit_node.distance_m <= 10000.0

    # Verify distance vs hops diverge across BOTH pgr_dijkstra and pgr_bdDijkstra
    for algo in ("pgr_dijkstra", "pgr_bdDijkstra"):
        with routing_db_engine.connect() as conn:
            dist_path = find_shortest_path(
                conn, n_start, n_end, cost_mode="distance", algorithm=algo
            )
            hops_path = find_shortest_path(
                conn, n_start, n_end, cost_mode="hops", algorithm=algo
            )
        assert dist_path is not None and dist_path.edge_ids == (e_b1, e_b2, e_b3)
        assert hops_path is not None and hops_path.edge_ids == (e_a1, e_a2)
        assert hops_path.total_cost == pytest.approx(2.0)

    # AC-30: raise difficulty on BOTH winning paths to 3; geometry must stay identical in all modes
    with routing_db_engine.begin() as conn:
        conn.execute(
            text("UPDATE edges SET initial_difficulty = 3 WHERE id = ANY(CAST(:ids AS bigint[]))"),
            {"ids": [e_a1, e_a2, e_b1, e_b2, e_b3]},
        )

    algos: tuple[RoutingAlgorithm, ...] = ("pgr_dijkstra", "pgr_bdDijkstra")
    mode_cases: tuple[tuple[RoutingCostMode, tuple[int, ...]], ...] = (
        ("distance", (e_b1, e_b2, e_b3)),
        ("hops", (e_a1, e_a2)),
    )
    for algo in algos:
        for mode, expected_edges in mode_cases:
            leg = calculate_leg(
                routing_db_engine,
                FakeRoutingProvider(),
                origin=Coordinate(lat=32.5995, lng=34.9995),
                destination=Coordinate(lat=32.6005, lng=35.0205),
                config=RoutingConfig(cost_mode=mode, algorithm=algo),
            )
            net_segs = [s for s in leg.segments if s.source == "network"]
            assert tuple(s.edge_id for s in net_segs) == expected_edges
            assert all(s.display_level == 3 for s in net_segs)


@pytest.mark.integration
def test_integration_qa_cross_poc_snapping_to_routing_end_to_end(
    routing_db_engine: Engine,
) -> None:
    """Cross-POC QA test: ingest real GPX/KML via T-003 and route over it via T-004."""
    from app.domain.geo.models import SnappingConfig
    from app.domain.geo.parser import parse_track_file
    from app.domain.geo.snapping import ingest_track_transaction

    _reset_graph(routing_db_engine)
    fixtures_dir = Path(__file__).resolve().parents[1] / "fixtures" / "geo"
    fwd_track = parse_track_file(
        "01_israel_base_forward.gpx",
        (fixtures_dir / "01_israel_base_forward.gpx").read_bytes(),
    )
    rev_track = parse_track_file(
        "02_israel_base_reverse.gpx",
        (fixtures_dir / "02_israel_base_reverse.gpx").read_bytes(),
    )

    cfg = SnappingConfig(20.0, 20.0)
    with routing_db_engine.begin() as conn:
        res_fwd = ingest_track_transaction(conn, fwd_track, cfg, difficulty=2)
        res_rev = ingest_track_transaction(conn, rev_track, cfg, difficulty=3)
        conn.execute(text("ANALYZE nodes"))
        conn.execute(text("ANALYZE edges"))

    assert res_fwd.edges_created > 0
    assert res_rev.edges_created == 0
    assert res_rev.edges_reused == res_fwd.edges_created

    # Route in reverse (from track end back to track start) -> exercises ST_Reverse on snapped edges
    start_pt = fwd_track.segments[0].points[-1]
    end_pt = fwd_track.segments[0].points[0]
    leg = calculate_leg(
        routing_db_engine,
        FakeRoutingProvider(),
        origin=Coordinate(lat=start_pt.lat - 0.0002, lng=start_pt.lon),
        destination=Coordinate(lat=end_pt.lat + 0.0002, lng=end_pt.lon),
        config=RoutingConfig(cost_mode="distance", algorithm="pgr_bdDijkstra"),
    )
    assert leg.branch == "connected"
    assert leg.graph_revision == 2
    net_segs = [s for s in leg.segments if s.source == "network"]
    assert len(net_segs) == res_fwd.edges_created
    assert all(s.display_level == 2 for s in net_segs)  # preserved original difficulty=2
    assert all(g <= 0.01 for g in leg.continuity_gaps_m)
