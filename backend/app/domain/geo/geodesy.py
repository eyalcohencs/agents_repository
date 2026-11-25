"""WGS84 geodesic calculations and endpoint-preserving point reduction."""

from __future__ import annotations

import math
from collections.abc import Sequence

from pyproj import Geod

from app.domain.geo.models import ParsedTrack, ReducedTrack, TrackPoint, TrackSegment

_WGS84_GEOD = Geod(ellps="WGS84")
_WGS84_A = 6_378_137.0
_WGS84_E2 = 0.006694379990141316


def geodesic_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute exact WGS84 ellipsoidal distance in meters between two points."""
    if lat1 == lat2 and lon1 == lon2:
        return 0.0
    _, _, dist = _WGS84_GEOD.inv(lon1, lat1, lon2, lat2)
    distance = float(dist)
    if not math.isfinite(distance) or distance < 0.0:
        return 0.0
    return distance


def segment_length_m(points: Sequence[TrackPoint]) -> float:
    """Compute total geodesic length in meters along an ordered sequence of points."""
    if len(points) < 2:
        return 0.0
    total = 0.0
    for idx in range(len(points) - 1):
        p1 = points[idx]
        p2 = points[idx + 1]
        total += geodesic_distance_m(p1.lat, p1.lon, p2.lat, p2.lon)
    return total


def point_to_segment_distance_m(pt: TrackPoint, a: TrackPoint, b: TrackPoint) -> float:
    """Compute geodesic distance in meters from `pt` to segment `a`-`b` on WGS84."""
    if a.lat == b.lat and a.lon == b.lon:
        return geodesic_distance_m(pt.lat, pt.lon, a.lat, a.lon)

    mid_lat = 0.5 * (a.lat + b.lat)
    mid_lon = 0.5 * (a.lon + b.lon)
    phi = math.radians(mid_lat)
    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    denom = 1.0 - _WGS84_E2 * sin_phi * sin_phi
    n_rad = _WGS84_A / math.sqrt(denom)
    m_rad = _WGS84_A * (1.0 - _WGS84_E2) / (denom * math.sqrt(denom))

    deg_to_rad = math.pi / 180.0
    scale_x = deg_to_rad * n_rad * cos_phi
    scale_y = deg_to_rad * m_rad

    ax = (a.lon - mid_lon) * scale_x
    ay = (a.lat - mid_lat) * scale_y
    bx = (b.lon - mid_lon) * scale_x
    by = (b.lat - mid_lat) * scale_y
    px = (pt.lon - mid_lon) * scale_x
    py = (pt.lat - mid_lat) * scale_y

    abx = bx - ax
    aby = by - ay
    ab_len_sq = abx * abx + aby * aby
    if ab_len_sq <= 0.0:
        return geodesic_distance_m(pt.lat, pt.lon, a.lat, a.lon)

    t = ((px - ax) * abx + (py - ay) * aby) / ab_len_sq
    if t <= 0.0:
        return geodesic_distance_m(pt.lat, pt.lon, a.lat, a.lon)
    if t >= 1.0:
        return geodesic_distance_m(pt.lat, pt.lon, b.lat, b.lon)

    closest_lat = a.lat + t * (b.lat - a.lat)
    closest_lon = a.lon + t * (b.lon - a.lon)
    return geodesic_distance_m(pt.lat, pt.lon, closest_lat, closest_lon)


def reduce_segment(segment: TrackSegment, min_distance_m: float) -> TrackSegment:
    """Reduce a single segment by geodesic distance while retaining first and last points.

    Never drops either endpoint (AC-44 / AC-65). Intermediate points are retained
    when their WGS84 geodesic distance from the previous retained point is at least
    `min_distance_m`.
    """
    if min_distance_m < 0:
        raise ValueError("min_distance_m must be non-negative.")

    pts = segment.points
    if len(pts) <= 2 or min_distance_m == 0:
        return TrackSegment(index=segment.index, points=tuple(pts))

    retained: list[TrackPoint] = [pts[0]]
    last_kept = pts[0]

    for pt in pts[1:-1]:
        if geodesic_distance_m(last_kept.lat, last_kept.lon, pt.lat, pt.lon) >= min_distance_m:
            retained.append(pt)
            last_kept = pt

    last_pt = pts[-1]
    if len(retained) == 1 or retained[-1] != last_pt:
        retained.append(last_pt)

    return TrackSegment(index=segment.index, points=tuple(retained))


def reduce_track(track: ParsedTrack, min_distance_m: float) -> ReducedTrack:
    """Reduce every segment of a parsed track independently, preserving gaps and endpoints."""
    if min_distance_m < 0:
        raise ValueError("min_distance_m must be non-negative.")

    reduced_segments = tuple(
        reduce_segment(segment, min_distance_m) for segment in track.segments
    )

    gaps: list[float] = []
    for idx in range(len(track.segments) - 1):
        end_prev = track.segments[idx].points[-1]
        start_next = track.segments[idx + 1].points[0]
        gaps.append(
            geodesic_distance_m(end_prev.lat, end_prev.lon, start_next.lat, start_next.lon)
        )

    reduced_count = sum(len(seg.points) for seg in reduced_segments)
    return ReducedTrack(
        source_format=track.source_format,
        segments=reduced_segments,
        file_sha256=track.file_sha256,
        normalized_fingerprint=track.normalized_fingerprint,
        raw_point_count=track.raw_point_count,
        reduced_point_count=reduced_count,
        min_distance_m=min_distance_m,
        input_segment_gaps_m=tuple(gaps),
    )
