"""Formal trial state machine and failure taxonomy."""

from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:  # Python 3.10 is part of the supported collection runtime.
    from enum import Enum

    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return self.value


class TrialState(StrEnum):
    PREPARE = "PREPARE"
    PRECONDITION_CHECK = "PRECONDITION_CHECK"
    BASELINE = "BASELINE"
    ACTION = "ACTION"
    ACTION_CONFIRM = "ACTION_CONFIRM"
    POST_ACTION = "POST_ACTION"
    QUALITY_CHECK = "QUALITY_CHECK"
    SAVE = "SAVE"
    FAILED = "FAILED"


class TrialErrorCode(StrEnum):
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    ACTION_FAILED = "ACTION_FAILED"
    ACTION_NOT_CONFIRMED = "ACTION_NOT_CONFIRMED"
    PROCESS_NOT_FOUND = "PROCESS_NOT_FOUND"
    PROCESS_AMBIGUOUS = "PROCESS_AMBIGUOUS"
    PROCESS_RESTARTED = "PROCESS_RESTARTED"
    CLEAR_REFS_FAILED = "CLEAR_REFS_FAILED"
    SMAPS_EMPTY = "SMAPS_EMPTY"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    HDC_TIMEOUT = "HDC_TIMEOUT"
    FEATURE_FAILED = "FEATURE_FAILED"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    HDC_DISCONNECTED = "HDC_DISCONNECTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TRANSFER_FAILED = "TRANSFER_FAILED"
    NEEDS_DEVICE_VALIDATION = "NEEDS_DEVICE_VALIDATION"


_TRANSITIONS = {
    TrialState.PREPARE: TrialState.PRECONDITION_CHECK,
    TrialState.PRECONDITION_CHECK: TrialState.BASELINE,
    TrialState.BASELINE: TrialState.ACTION,
    TrialState.ACTION: TrialState.ACTION_CONFIRM,
    TrialState.ACTION_CONFIRM: TrialState.POST_ACTION,
    TrialState.POST_ACTION: TrialState.QUALITY_CHECK,
    TrialState.QUALITY_CHECK: TrialState.SAVE,
}


class InvalidTrialTransition(RuntimeError):
    pass


class TrialStateMachine:
    def __init__(self) -> None:
        self.state = TrialState.PREPARE
        self.history = [self.state]

    def advance(self, next_state: TrialState) -> None:
        if _TRANSITIONS.get(self.state) != next_state:
            raise InvalidTrialTransition(f"{self.state} -> {next_state}")
        self.state = next_state
        self.history.append(next_state)

    def fail(self) -> None:
        if self.state == TrialState.FAILED:
            return
        self.state = TrialState.FAILED
        self.history.append(self.state)
