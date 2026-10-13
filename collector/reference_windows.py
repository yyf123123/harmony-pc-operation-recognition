"""Explicit reference-window capture contracts for real VMA trials."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Generic, Mapping, TypeVar


Captured = TypeVar("Captured")


def capture_profile_name(value: object) -> str:
    """Return the human-readable profile name from stored metadata."""
    if isinstance(value, CaptureProfile):
        return value.name
    if isinstance(value, Mapping):
        return str(value.get("name") or "unknown")
    return str(value or "unknown")


def capture_profile_identity(value: object) -> str:
    """Return the immutable identity used to isolate capture conditions.

    The display name alone is not enough to evaluate features safely: changing
    a reference or post window changes the population represented by the
    feature.  Older rows that only stored a name remain readable, whereas new
    complete profiles incorporate every timing value into their identity.
    """
    if isinstance(value, CaptureProfile):
        value = value.to_metadata()
    if not isinstance(value, Mapping):
        return capture_profile_name(value)
    explicit = value.get("identity")
    if explicit:
        return str(explicit)
    required = ("baseline_window_ms", "post_short_window_ms", "post_long_window_ms")
    if not all(key in value for key in required):
        return capture_profile_name(value)
    return (
        f"{capture_profile_name(value)}"
        f"|baseline_window_ms={int(value['baseline_window_ms'])}"
        f"|post_short_window_ms={int(value['post_short_window_ms'])}"
        f"|post_long_window_ms={int(value['post_long_window_ms'])}"
        f"|prepare_settle_ms={int(value.get('prepare_settle_ms', 0))}"
    )


@dataclass(frozen=True)
class CaptureProfile:
    """Frozen timing semantics recorded with every v3 collection session."""

    name: str = "cross_app.reference_windows.v1"
    baseline_window_ms: int = 750
    post_short_window_ms: int = 750
    post_long_window_ms: int = 3000
    prepare_settle_ms: int = 0

    def __post_init__(self) -> None:
        if min(
            self.baseline_window_ms,
            self.post_short_window_ms,
            self.post_long_window_ms,
            self.prepare_settle_ms,
        ) < 0:
            raise ValueError("capture window durations must not be negative")

    @property
    def identity(self) -> str:
        return capture_profile_identity(
            {
                "name": self.name,
                "baseline_window_ms": self.baseline_window_ms,
                "post_short_window_ms": self.post_short_window_ms,
                "post_long_window_ms": self.post_long_window_ms,
                "prepare_settle_ms": self.prepare_settle_ms,
            }
        )

    @property
    def baseline_phases(self) -> tuple[str, str]:
        return ("BASELINE_1", "BASELINE_2")

    @property
    def action_phase(self) -> str:
        return "ACTION_WINDOW"

    @property
    def post_windows(self) -> tuple[tuple[str, int], tuple[str, int]]:
        return (("POST_0750", self.post_short_window_ms), ("POST_3000", self.post_long_window_ms))

    def to_metadata(self) -> dict[str, object]:
        data = asdict(self)
        data.update(
            {
                "identity": self.identity,
                "baseline_phases": list(self.baseline_phases),
                "action_phase": self.action_phase,
                "post_windows": [{"phase": phase, "window_ms": window_ms} for phase, window_ms in self.post_windows],
            }
        )
        return data


class ReferenceWindowCapture(Generic[Captured]):
    """Separates reference clearing from capture so action evidence cannot be erased."""

    def __init__(self, *, clear_refs: Callable[[], None], sleep: Callable[[float], None] = time.sleep) -> None:
        self._clear_refs = clear_refs
        self._sleep = sleep
        self.events: list[tuple[str, str]] = []

    def arm(self, phase: str) -> None:
        self._clear_refs()
        self.events.append(("arm", phase))

    def wait_ms(self, phase: str, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("window duration must not be negative")
        self._sleep(milliseconds / 1000.0)
        self.events.append(("wait", phase))

    def capture(self, phase: str, capture_snapshot: Callable[[], Captured]) -> Captured:
        """Capture an already armed window; this method intentionally never clears refs."""
        result = capture_snapshot()
        self.events.append(("capture", phase))
        return result
