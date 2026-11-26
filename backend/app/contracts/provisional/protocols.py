"""Backwards-compatible provisional protocol re-exports (frozen in T-009)."""

from __future__ import annotations

from app.contracts.protocols import (
    AllowanceReservationResult,
    AllowanceSettlementResult,
    ConfirmedUploadResult,
    EdgeRatingSummary,
    EligibilityDecision,
    EligibilityService,
    FileStorageAdapter,
    GeocodingMatch,
    GeocodingProvider,
    GraceApplicationResult,
    IngestionService,
    RatingService,
    RoutingProvider,
    StoredFileInfo,
    StoredFileRef,
    TrackSource,
    TrackSourceEntry,
    ValidatedUploadDraft,
)

__all__ = [
    "AllowanceReservationResult",
    "AllowanceSettlementResult",
    "ConfirmedUploadResult",
    "EdgeRatingSummary",
    "EligibilityDecision",
    "EligibilityService",
    "FileStorageAdapter",
    "GeocodingMatch",
    "GeocodingProvider",
    "GraceApplicationResult",
    "IngestionService",
    "RatingService",
    "RoutingProvider",
    "StoredFileInfo",
    "StoredFileRef",
    "TrackSource",
    "TrackSourceEntry",
    "ValidatedUploadDraft",
]
