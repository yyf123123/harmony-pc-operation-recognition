"""Automation backend contract shared by all applications."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from apps.base import OperationSpec


class DeviceValidationRequired(RuntimeError):
    """Raised when a device-only identifier or UI contract is not validated."""


@dataclass(frozen=True)
class BackendActionResult:
    status: str
    state: Mapping[str, Any] = field(default_factory=dict)
    event: Mapping[str, Any] = field(default_factory=dict)
    timing: Mapping[str, float | None] = field(default_factory=dict)
    source: str = "device"
    is_mock: bool = False


class AutomationBackend(ABC):
    source = "device"
    is_mock = False

    @abstractmethod
    def prepare(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        raise NotImplementedError

    @abstractmethod
    def execute(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        raise NotImplementedError

    @abstractmethod
    def validate(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        raise NotImplementedError

    def confirm(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        """Confirm the observable post-action state; never infer success from exit 0."""
        return self.validate(app_id, operation, state)

    def close(self) -> None:
        return None
