"""Stage timing with independent HDC and algorithm fields."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


TIMING_FIELDS = (
    "T_clear_refs", "T_device_sampling", "T_hdc_transfer", "T_parse",
    "T_feature", "T_clustering", "T_total",
)


@dataclass
class TimingRecorder:
    source: str = "device"
    is_mock: bool = False
    values: dict[str, float | None] = field(default_factory=lambda: {key: None for key in TIMING_FIELDS})

    @contextmanager
    def measure(self, field: str) -> Iterator[None]:
        if field not in TIMING_FIELDS:
            raise ValueError(field)
        started = time.perf_counter()
        try:
            yield
        finally:
            prior = self.values.get(field)
            self.values[field] = (float(prior) if prior is not None else 0.0) + (time.perf_counter() - started)

    def start_total(self) -> float:
        return time.perf_counter()

    def finish_total(self, started: float) -> None:
        self.values["T_total"] = time.perf_counter() - started
