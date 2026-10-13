"""Reproducible, balanced operation order for device collection sessions."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ScheduledOperation:
    round_index: int
    position: int
    operation_id: str


def balanced_trial_schedule(operation_ids: Iterable[str], *, rounds: int, seed: int) -> tuple[ScheduledOperation, ...]:
    """Shuffle once then rotate each round, distributing operations over positions."""
    operations = tuple(operation_ids)
    if not operations:
        raise ValueError("at least one operation is required")
    if len(set(operations)) != len(operations):
        raise ValueError("operation ids must be unique in a balanced schedule")
    if rounds < 1:
        raise ValueError("rounds must be positive")
    order = list(operations)
    random.Random(seed).shuffle(order)
    scheduled: list[ScheduledOperation] = []
    for round_index in range(1, rounds + 1):
        shift = (round_index - 1) % len(order)
        rotated = order[shift:] + order[:shift]
        scheduled.extend(
            ScheduledOperation(round_index=round_index, position=position, operation_id=operation_id)
            for position, operation_id in enumerate(rotated, start=1)
        )
    return tuple(scheduled)
