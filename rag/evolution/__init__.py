"""Observation-phase building blocks for assisted self-evolution.

The package is deliberately isolated from the online path: the recorder only
accepts ACL-filtered diagnostic details, and nothing here can change a response,
a candidate, or a release gate.
"""

from rag.evolution.models import (
    EVOLUTION_TRANSITIONS,
    DiagnosticDetail,
    EvolutionStatus,
    FailureObservation,
    FailureStage,
    NonEvolvableReason,
    ObservationPool,
)
from rag.evolution.recorder import FailureRecorder
from rag.evolution.store import FailureTraceStore

__all__ = [
    "EVOLUTION_TRANSITIONS",
    "DiagnosticDetail",
    "EvolutionStatus",
    "FailureObservation",
    "FailureRecorder",
    "FailureStage",
    "FailureTraceStore",
    "NonEvolvableReason",
    "ObservationPool",
]
