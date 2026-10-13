"""Serializable metadata models for the SQLite + immutable-file dataset."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    app_id: str
    started_at: str
    source: str
    is_mock: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrialRecord:
    trial_id: str
    session_id: str
    operation_id: str
    status: str
    baseline_ts: str | None = None
    action_start_ts: str | None = None
    action_end_ts: str | None = None
    post_ts: str | None = None
    source: str = "device"
    is_mock: bool = False
    state: Mapping[str, Any] = field(default_factory=dict)
    current_state: str = "SAVE"
    quality_status: str = "VALID"
    error_code: str | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.status != "success" and self.quality_status == "VALID":
            object.__setattr__(self, "quality_status", "INVALID")


@dataclass(frozen=True)
class QualityRecord:
    trial_id: str
    valid: bool
    signal_strength: float | None
    repeatability: float | None = None
    inter_class_margin: float | None = None
    valid_coverage: float | None = None
    reason: str | None = None
    source: str = "device"
    is_mock: bool = False
    quality_status: str = "VALID"
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.valid and self.quality_status == "VALID":
            object.__setattr__(self, "quality_status", "INVALID")
