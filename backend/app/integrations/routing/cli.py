"""Live OpenRouteService vs. GraphHopper routing comparison CLI (`T-004` / `T-028`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.integrations.routing.benchmark import compute_percentiles
from app.integrations.routing.models import (
    Coordinate,
    RoutingProviderError,
    geodesic_distance_m,
)
from app.integrations.routing.protocol import RoutingProvider
from app.integrations.routing.providers import (
    FakeRoutingProvider,
    GraphHopperProvider,
    OpenRouteServiceProvider,
)


@dataclass(frozen=True, slots=True)
class CoordinatePairSpec:
    """Single origin/destination test case loaded from JSON input."""

    name: str
    origin: Coordinate
    destination: Coordinate
    profiles: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class PairComparisonRecord:
    """Outcome of a single provider call, recording external snap gaps separately."""

    pair_name: str
    provider: str
    requested_profile: str
    resolved_profile: str | None
    success: bool
    latency_ms: float | None
    point_count: int | None
    distance_m: float | None
    duration_s: float | None
    origin_snap_gap_m: float | None
    destination_snap_gap_m: float | None
    error_code: str | None = None
    status_code: int | None = None
    error_message: str | None = None


def load_coordinate_pairs(path: Path) -> list[CoordinatePairSpec]:
    """Load coordinate pairs from a JSON file (`[{name, origin, destination, profile?}]`)."""
    raw_data: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw_data, dict) and isinstance(raw_data.get("pairs"), list):
        items = raw_data["pairs"]
    elif isinstance(raw_data, list):
        items = raw_data
    else:
        raise ValueError("Input JSON must be a list of pairs or an object with a 'pairs' list.")

    pairs: list[CoordinatePairSpec] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Pair entry at index {idx} must be a JSON object.")
        name = str(item.get("name") or f"pair-{idx + 1}")
        raw_origin = item.get("origin")
        raw_dest = item.get("destination")
        if not isinstance(raw_origin, dict) or not isinstance(raw_dest, dict):
            raise ValueError(f"Pair {name!r} must include 'origin' and 'destination' objects.")
        origin = Coordinate(
            lat=float(raw_origin["lat"]),
            lng=float(raw_origin["lng"]),
            ele=float(raw_origin["ele"]) if raw_origin.get("ele") is not None else None,
        )
        destination = Coordinate(
            lat=float(raw_dest["lat"]),
            lng=float(raw_dest["lng"]),
            ele=float(raw_dest["ele"]) if raw_dest.get("ele") is not None else None,
        )

        profiles: tuple[str, ...] | None = None
        if isinstance(item.get("profiles"), list):
            profiles = tuple(str(p).strip() for p in item["profiles"] if str(p).strip())
        elif isinstance(item.get("profile"), str) and item["profile"].strip():
            profiles = (item["profile"].strip(),)

        pairs.append(
            CoordinatePairSpec(
                name=name,
                origin=origin,
                destination=destination,
                profiles=profiles,
            )
        )
    return pairs


def _sanitize_error_message(message: str, secrets: Sequence[str]) -> str:
    """Scrub any configured API key values from error strings as defense-in-depth."""
    cleaned = message
    for secret in secrets:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned


def run_provider_comparison(
    pairs: Sequence[CoordinatePairSpec],
    providers: Sequence[RoutingProvider],
    *,
    default_profiles: Sequence[str] = ("driving-car", "cycling-mountain"),
    secrets_to_redact: Sequence[str] = (),
) -> dict[str, Any]:
    """Compare one or more `RoutingProvider` implementations across coordinate pairs."""
    records: list[PairComparisonRecord] = []

    for pair in pairs:
        profiles = pair.profiles if pair.profiles else tuple(default_profiles)
        for profile in profiles:
            for provider in providers:
                try:
                    result = provider.route(pair.origin, pair.destination, profile=profile)
                except RoutingProviderError as exc:
                    records.append(
                        PairComparisonRecord(
                            pair_name=pair.name,
                            provider=provider.name,
                            requested_profile=profile,
                            resolved_profile=None,
                            success=False,
                            latency_ms=None,
                            point_count=None,
                            distance_m=None,
                            duration_s=None,
                            origin_snap_gap_m=None,
                            destination_snap_gap_m=None,
                            error_code=exc.code,
                            status_code=exc.status_code,
                            error_message=_sanitize_error_message(exc.message, secrets_to_redact),
                        )
                    )
                else:
                    origin_snap_gap_m = geodesic_distance_m(pair.origin, result.points[0])
                    destination_snap_gap_m = geodesic_distance_m(
                        pair.destination,
                        result.points[-1],
                    )
                    records.append(
                        PairComparisonRecord(
                            pair_name=pair.name,
                            provider=provider.name,
                            requested_profile=profile,
                            resolved_profile=result.profile,
                            success=True,
                            latency_ms=round(result.latency_ms, 2),
                            point_count=len(result.points),
                            distance_m=(
                                round(result.distance_m, 2)
                                if result.distance_m is not None
                                else None
                            ),
                            duration_s=(
                                round(result.duration_s, 2)
                                if result.duration_s is not None
                                else None
                            ),
                            origin_snap_gap_m=round(origin_snap_gap_m, 2),
                            destination_snap_gap_m=round(destination_snap_gap_m, 2),
                        )
                    )

    summary: dict[str, dict[str, Any]] = {}
    for provider in providers:
        prov_records = [r for r in records if r.provider == provider.name]
        succeeded = [r for r in prov_records if r.success]
        failed = [r for r in prov_records if not r.success]
        latencies = [r.latency_ms for r in succeeded if r.latency_ms is not None]
        origin_gaps = [r.origin_snap_gap_m for r in succeeded if r.origin_snap_gap_m is not None]
        dest_gaps = [
            r.destination_snap_gap_m for r in succeeded if r.destination_snap_gap_m is not None
        ]
        latency_stats = compute_percentiles(latencies) if latencies else None

        summary[provider.name] = {
            "total_requests": len(prov_records),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "failure_rate": (
                round(len(failed) / len(prov_records), 4) if prov_records else 0.0
            ),
            "rate_limited_count": sum(1 for r in failed if r.status_code == 429),
            "p50_latency_ms": round(latency_stats.p50_ms, 2) if latency_stats else None,
            "p95_latency_ms": round(latency_stats.p95_ms, 2) if latency_stats else None,
            "max_origin_snap_gap_m": round(max(origin_gaps), 2) if origin_gaps else None,
            "max_destination_snap_gap_m": round(max(dest_gaps), 2) if dest_gaps else None,
        }

    return {
        "pair_count": len(pairs),
        "default_profiles": list(default_profiles),
        "summary": summary,
        "records": [asdict(r) for r in records],
    }


def _build_providers_from_env(
    provider_names: Sequence[str],
    *,
    timeout_s: float,
    max_retries: int,
) -> tuple[list[RoutingProvider], list[str]]:
    providers: list[RoutingProvider] = []
    secrets: list[str] = []

    for raw_name in provider_names:
        name = raw_name.strip().lower()
        if not name:
            continue
        if name in ("ors", "openrouteservice"):
            api_key = os.environ.get("ORS_API_KEY", "").strip()
            if not api_key:
                raise SystemExit("ORS_API_KEY environment variable is required for 'ors'.")
            secrets.append(api_key)
            providers.append(
                OpenRouteServiceProvider(
                    api_key=api_key,
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                )
            )
        elif name in ("gh", "graphhopper"):
            api_key = os.environ.get("GRAPHHOPPER_API_KEY", "").strip()
            if not api_key:
                raise SystemExit(
                    "GRAPHHOPPER_API_KEY environment variable is required for 'graphhopper'."
                )
            secrets.append(api_key)
            providers.append(
                GraphHopperProvider(
                    api_key=api_key,
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                )
            )
        elif name == "fake":
            providers.append(FakeRoutingProvider())
        else:
            raise SystemExit(
                f"Unsupported provider {raw_name!r}. Choose from: ors, graphhopper, fake."
            )

    if not providers:
        raise SystemExit("At least one provider must be specified via --providers.")
    return providers, secrets


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.integrations.routing.cli",
        description=(
            "Compare external routing providers (OpenRouteService vs. GraphHopper) "
            "over JSON coordinate pairs. Reads ORS_API_KEY and GRAPHHOPPER_API_KEY from env."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to JSON file containing origin/destination coordinate pairs.",
    )
    parser.add_argument(
        "--providers",
        default="ors,graphhopper",
        help="Comma-separated providers to test (ors, graphhopper, fake).",
    )
    parser.add_argument(
        "--profiles",
        default="driving-car,cycling-mountain",
        help="Comma-separated default profiles when not specified per pair.",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=10.0,
        help="Per-request HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retries on transient 5xx/network errors.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path to write full structured JSON comparison report.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for live provider comparison."""
    args = _parse_args(argv)
    pairs = load_coordinate_pairs(args.input)
    provider_names = [p.strip() for p in args.providers.split(",") if p.strip()]
    default_profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]

    providers, secrets = _build_providers_from_env(
        provider_names,
        timeout_s=args.timeout_s,
        max_retries=args.max_retries,
    )
    try:
        report = run_provider_comparison(
            pairs,
            providers,
            default_profiles=default_profiles,
            secrets_to_redact=secrets,
        )
    finally:
        for provider in providers:
            close_fn = getattr(provider, "close", None)
            if callable(close_fn):
                close_fn()

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
