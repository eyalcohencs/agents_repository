"""Backwards-compatible provisional policy wrapper (frozen in T-009).

Retains ``CONTRACT_STAGE = "provisional"`` and ``is_provisional_contract() == True``
within ``app.contracts.provisional.policy`` so existing provisional imports continue
working while new callers import ``CONTRACT_STAGE = "frozen"`` from ``app.contracts``.
"""

from __future__ import annotations

from typing import Literal

from app.contracts.policy import (
    CONTRACT_FREEZE_TASK,
    GATE_1_POLICY_ANNOTATIONS,
    GATE_2_OPEN_FIELDS,
    PolicyAnnotation,
    PolicyStatus,
)

CONTRACT_STAGE: Literal["provisional"] = "provisional"


def is_provisional_contract() -> bool:
    """Return True for the backwards-compatible provisional policy namespace."""
    return CONTRACT_STAGE == "provisional"


__all__ = [
    "CONTRACT_FREEZE_TASK",
    "CONTRACT_STAGE",
    "GATE_1_POLICY_ANNOTATIONS",
    "GATE_2_OPEN_FIELDS",
    "PolicyAnnotation",
    "PolicyStatus",
    "is_provisional_contract",
]
