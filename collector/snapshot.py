"""Application-neutral VMA snapshot data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class VmaRegion:
    start: int
    end: int
    permissions: str
    pathname: str
    segment: str
    referenced_pages: float = 0.0
    rss_pages: float = 0.0
    pss_pages: float = 0.0
    private_clean_pages: float = 0.0
    private_dirty_pages: float = 0.0
    anonymous_pages: float = 0.0

    @property
    def namespace(self) -> str:
        return "FILE" if self.pathname.startswith("/") or self.segment.lower().startswith("file") else "ANON"

    @property
    def semantic_key(self) -> str:
        pathname = self.pathname.replace("\\", "/") or "(anonymous)"
        if self.namespace == "ANON" and pathname in {"", "(anonymous)"}:
            pathname = "(anonymous)"
        return f"{pathname}|segment={self.segment}|perms={self.permissions}"


@dataclass(frozen=True)
class VmaSnapshot:
    snapshot_id: str
    app_id: str
    phase: str
    timestamp: str
    regions: tuple[VmaRegion, ...] = ()
    source: str = "device"
    is_mock: bool = False
    pid: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(cls, snapshot_id: str, app_id: str, phase: str, regions: Iterable[VmaRegion], **kwargs: Any) -> "VmaSnapshot":
        return cls(
            snapshot_id=snapshot_id,
            app_id=app_id,
            phase=phase,
            timestamp=datetime.now(timezone.utc).isoformat(),
            regions=tuple(regions),
            **kwargs,
        )


def aggregate_snapshot_metric(snapshot: VmaSnapshot, metric: str) -> dict[tuple[str, str], float]:
    """Aggregate a region metric by the app-neutral VMA semantic key."""
    attributes = {
        "referenced": "referenced_pages",
        "rss": "rss_pages",
        "pss": "pss_pages",
        "private_clean": "private_clean_pages",
        "private_dirty": "private_dirty_pages",
        "anonymous": "anonymous_pages",
    }
    try:
        attribute = attributes[metric]
    except KeyError as error:
        raise ValueError(f"unsupported VMA metric: {metric}") from error
    values: dict[tuple[str, str], float] = {}
    for region in snapshot.regions:
        key = (region.namespace, region.semantic_key)
        values[key] = values.get(key, 0.0) + max(float(getattr(region, attribute)), 0.0)
    return values


def aggregate_snapshot_features(snapshot: VmaSnapshot) -> dict[tuple[str, str], float]:
    """Compatibility alias for the frozen Referenced-only v1/v2 transform."""
    return aggregate_snapshot_metric(snapshot, "referenced")
