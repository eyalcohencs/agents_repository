"""Safe GPX/KML parsers and canonical track fingerprinting."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree as StdET

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as DefusedET

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
    TrackPoint,
    TrackSegment,
)

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".gpx", ".kml"})


def compute_file_sha256(raw_bytes: bytes) -> str:
    """Compute SHA-256 hex digest of raw file bytes for integrity and storage identity."""
    return hashlib.sha256(raw_bytes).hexdigest()


def _format_coord_6dp(value: float) -> str:
    rounded = round(value, 6)
    if rounded == 0.0:
        rounded = 0.0
    return f"{rounded:.6f}"


def compute_normalized_fingerprint(segments: Sequence[TrackSegment]) -> str:
    """Compute canonical SHA-256 fingerprint over ordered segments at 6-decimal precision.

    Preserves point order and explicit segment boundaries while normalizing `-0.0` to `0.0`
    and ignoring XML metadata (`<time>`, `<name>`, `<desc>`, `<ele>`, namespaces, whitespace).
    """
    canonical_lines: list[str] = []
    for seg_idx, segment in enumerate(segments):
        coords = ";".join(
            f"{_format_coord_6dp(pt.lat)},{_format_coord_6dp(pt.lon)}"
            for pt in segment.points
        )
        canonical_lines.append(f"seg:{seg_idx}:{coords}")
    canonical_payload = "\n".join(canonical_lines).encode("utf-8")
    return hashlib.sha256(canonical_payload).hexdigest()


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _validate_size_and_non_empty(raw_bytes: bytes, max_upload_mb: int) -> None:
    if not raw_bytes or not raw_bytes.strip():
        raise GeoParseError(ERROR_CODE_EMPTY_FILE, "Track file is empty.")
    max_bytes = max_upload_mb * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise GeoParseError(
            ERROR_CODE_FILE_TOO_LARGE,
            f"Track file exceeds maximum allowed size of {max_upload_mb} MB.",
        )


def _safe_parse_xml(raw_bytes: bytes) -> StdET.Element:
    try:
        root: StdET.Element = DefusedET.fromstring(
            raw_bytes,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except DefusedXmlException as exc:
        raise GeoParseError(
            ERROR_CODE_UNSAFE_XML,
            "XML file contains forbidden DTD or entity declarations.",
        ) from exc
    except (StdET.ParseError, UnicodeDecodeError, ValueError) as exc:
        raise GeoParseError(
            ERROR_CODE_MALFORMED_XML,
            "Track file contains malformed XML.",
        ) from exc
    return root


def _parse_float(raw: str | None, label: str) -> float:
    if raw is None:
        raise GeoParseError(
            ERROR_CODE_INVALID_COORDINATE,
            f"Missing required {label} coordinate value.",
        )
    try:
        val = float(raw.strip())
    except ValueError as exc:
        raise GeoParseError(
            ERROR_CODE_INVALID_COORDINATE,
            f"Invalid numeric {label} coordinate value.",
        ) from exc
    if not math.isfinite(val):
        raise GeoParseError(
            ERROR_CODE_INVALID_COORDINATE,
            f"Non-finite {label} coordinate value.",
        )
    return val


def _parse_optional_elevation(raw: str | None) -> float | None:
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        val = float(stripped)
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def _build_track_point(lat: float, lon: float, ele: float | None = None) -> TrackPoint:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise GeoParseError(
            ERROR_CODE_INVALID_COORDINATE,
            f"Coordinate out of valid WGS84 range (lat={lat}, lon={lon}).",
        )
    return TrackPoint(lat=lat, lon=lon, ele=ele)


def _segment_has_distinct_points(points: Sequence[TrackPoint]) -> bool:
    if len(points) < 2:
        return False
    first = points[0]
    return any(pt.lat != first.lat or pt.lon != first.lon for pt in points[1:])


def _finalize_segments(raw_segments: Sequence[list[TrackPoint]]) -> tuple[TrackSegment, ...]:
    usable: list[TrackSegment] = []
    for pts in raw_segments:
        if len(pts) >= 2 and _segment_has_distinct_points(pts):
            usable.append(TrackSegment(index=len(usable), points=tuple(pts)))

    if not usable:
        raise GeoParseError(
            ERROR_CODE_DEGENERATE_GEOMETRY,
            "Track contains no usable segment with at least 2 distinct points.",
        )
    return tuple(usable)


def parse_gpx(
    raw_bytes: bytes,
    *,
    max_upload_mb: int = 10,
    max_upload_points: int = 50_000,
) -> ParsedTrack:
    """Safely parse a GPX file, preserving all `<trk>`/`<trkseg>` elements separately."""
    _validate_size_and_non_empty(raw_bytes, max_upload_mb)
    root = _safe_parse_xml(raw_bytes)

    if _local_name(root.tag).lower() != "gpx":
        raise GeoParseError(
            ERROR_CODE_MALFORMED_XML,
            "Root XML element is not a valid GPX document.",
        )

    raw_segments: list[list[TrackPoint]] = []
    raw_point_count = 0

    for elem in root.iter():
        if _local_name(elem.tag) != "trk":
            continue
        for child in elem:
            if _local_name(child.tag) != "trkseg":
                continue
            seg_points: list[TrackPoint] = []
            for pt_elem in child:
                if _local_name(pt_elem.tag) != "trkpt":
                    continue
                raw_point_count += 1
                if raw_point_count > max_upload_points:
                    raise GeoParseError(
                        ERROR_CODE_TOO_MANY_POINTS,
                        f"Track exceeds maximum allowed point count ({max_upload_points}).",
                    )
                lat = _parse_float(pt_elem.attrib.get("lat"), "latitude")
                lon = _parse_float(pt_elem.attrib.get("lon"), "longitude")
                ele: float | None = None
                for pt_child in pt_elem:
                    if _local_name(pt_child.tag) == "ele":
                        ele = _parse_optional_elevation(pt_child.text)
                        break
                seg_points.append(_build_track_point(lat=lat, lon=lon, ele=ele))
            if seg_points:
                raw_segments.append(seg_points)

    segments = _finalize_segments(raw_segments)
    return ParsedTrack(
        source_format="gpx",
        segments=segments,
        file_sha256=compute_file_sha256(raw_bytes),
        normalized_fingerprint=compute_normalized_fingerprint(segments),
        raw_point_count=raw_point_count,
    )


def parse_kml(
    raw_bytes: bytes,
    *,
    max_upload_mb: int = 10,
    max_upload_points: int = 50_000,
) -> ParsedTrack:
    """Safely parse a KML file, normalizing KML `lon,lat[,ele]` order into WGS84 TrackPoints."""
    _validate_size_and_non_empty(raw_bytes, max_upload_mb)
    root = _safe_parse_xml(raw_bytes)

    if _local_name(root.tag).lower() != "kml":
        raise GeoParseError(
            ERROR_CODE_MALFORMED_XML,
            "Root XML element is not a valid KML document.",
        )

    raw_segments: list[list[TrackPoint]] = []
    raw_point_count = 0

    for elem in root.iter():
        local = _local_name(elem.tag)
        if local == "LineString":
            seg_points: list[TrackPoint] = []
            for child in elem:
                if _local_name(child.tag) != "coordinates":
                    continue
                coord_text = (child.text or "").strip()
                if not coord_text:
                    continue
                for token in coord_text.split():
                    parts = token.split(",")
                    if len(parts) < 2:
                        raise GeoParseError(
                            ERROR_CODE_INVALID_COORDINATE,
                            "Malformed KML coordinate tuple; expected lon,lat[,ele].",
                        )
                    raw_point_count += 1
                    if raw_point_count > max_upload_points:
                        raise GeoParseError(
                            ERROR_CODE_TOO_MANY_POINTS,
                            f"Track exceeds maximum allowed point count ({max_upload_points}).",
                        )
                    lon = _parse_float(parts[0], "longitude")
                    lat = _parse_float(parts[1], "latitude")
                    ele = _parse_optional_elevation(parts[2]) if len(parts) >= 3 else None
                    seg_points.append(_build_track_point(lat=lat, lon=lon, ele=ele))
            if seg_points:
                raw_segments.append(seg_points)
        elif local == "Track":
            # KML gx:Track uses <gx:coord>lon lat [ele]</gx:coord>
            gx_points: list[TrackPoint] = []
            for child in elem:
                if _local_name(child.tag) != "coord":
                    continue
                coord_text = (child.text or "").strip()
                if not coord_text:
                    continue
                parts = coord_text.split()
                if len(parts) < 2:
                    raise GeoParseError(
                        ERROR_CODE_INVALID_COORDINATE,
                        "Malformed KML gx:coord entry; expected lon lat [ele].",
                    )
                raw_point_count += 1
                if raw_point_count > max_upload_points:
                    raise GeoParseError(
                        ERROR_CODE_TOO_MANY_POINTS,
                        f"Track exceeds maximum allowed point count ({max_upload_points}).",
                    )
                lon = _parse_float(parts[0], "longitude")
                lat = _parse_float(parts[1], "latitude")
                ele = _parse_optional_elevation(parts[2]) if len(parts) >= 3 else None
                gx_points.append(_build_track_point(lat=lat, lon=lon, ele=ele))
            if gx_points:
                raw_segments.append(gx_points)

    segments = _finalize_segments(raw_segments)
    return ParsedTrack(
        source_format="kml",
        segments=segments,
        file_sha256=compute_file_sha256(raw_bytes),
        normalized_fingerprint=compute_normalized_fingerprint(segments),
        raw_point_count=raw_point_count,
    )


def parse_track_file(
    filename: str,
    raw_bytes: bytes,
    *,
    max_upload_mb: int = 10,
    max_upload_points: int = 50_000,
    allowed_formats: frozenset[str] = frozenset({"gpx", "kml"}),
) -> ParsedTrack:
    """Dispatch parsing by file extension, rejecting `.twl` and unsupported formats cleanly."""
    ext = Path(filename).suffix.lower()
    if ext == ".gpx" and "gpx" in allowed_formats:
        return parse_gpx(
            raw_bytes,
            max_upload_mb=max_upload_mb,
            max_upload_points=max_upload_points,
        )
    if ext == ".kml" and "kml" in allowed_formats:
        return parse_kml(
            raw_bytes,
            max_upload_mb=max_upload_mb,
            max_upload_points=max_upload_points,
        )
    raise GeoParseError(
        ERROR_CODE_UNSUPPORTED_FORMAT,
        f"Unsupported track file format '{ext or 'none'}'. Supported formats: GPX, KML.",
    )


def parse_track_directory(
    directory: Path,
    *,
    max_upload_mb: int = 10,
    max_upload_points: int = 50_000,
    recursive: bool = False,
) -> tuple[list[tuple[Path, ParsedTrack]], list[tuple[Path, GeoParseError]]]:
    """Parse all files in a directory, skipping unsupported/corrupt files without aborting."""
    iterator = sorted(directory.rglob("*") if recursive else directory.iterdir())
    parsed: list[tuple[Path, ParsedTrack]] = []
    errors: list[tuple[Path, GeoParseError]] = []

    for path in iterator:
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            continue
        try:
            track = parse_track_file(
                path.name,
                path.read_bytes(),
                max_upload_mb=max_upload_mb,
                max_upload_points=max_upload_points,
            )
            parsed.append((path, track))
        except GeoParseError as exc:
            errors.append((path, exc))

    return parsed, errors
