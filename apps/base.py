"""Application-neutral operation catalog and adapter contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    app_id: str
    module: str
    operation_family: str
    display_name: str
    precondition: str
    action_spec: Mapping[str, Any]
    postcondition: str
    content_id: str | None = None
    state_variables: tuple[str, ...] = ()
    primary_candidate: bool = False
    primary_eval: bool = False
    expected_signal: str = "unknown"
    requires_network: bool = False
    requires_fixed_content: bool = False
    automation_status: str = "UNIMPLEMENTED"
    device_validation_status: str = "NEEDS_DEVICE_VALIDATION"
    exclude_reason: str | None = None
    signal_strength: float | None = None
    repeatability: float | None = None
    inter_class_margin: float | None = None
    valid_coverage: float | None = None
    classification_result: float | None = None
    quality_version: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OperationSpec":
        required = (
            "operation_id", "app_id", "module", "operation_family",
            "display_name", "precondition", "action_spec", "postcondition",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"operation is missing required fields: {missing}")
        return cls(
            operation_id=str(value["operation_id"]),
            app_id=str(value["app_id"]),
            module=str(value["module"]),
            operation_family=str(value["operation_family"]),
            display_name=str(value["display_name"]),
            precondition=str(value["precondition"]),
            action_spec=dict(value["action_spec"]),
            postcondition=str(value["postcondition"]),
            content_id=value.get("content_id"),
            state_variables=tuple(value.get("state_variables", ())),
            primary_candidate=bool(value.get("primary_candidate", False)),
            primary_eval=bool(value.get("primary_eval", False)),
            expected_signal=str(value.get("expected_signal", "unknown")),
            requires_network=bool(value.get("requires_network", False)),
            requires_fixed_content=bool(value.get("requires_fixed_content", False)),
            automation_status=str(value.get("automation_status", "UNIMPLEMENTED")),
            device_validation_status=str(value.get("device_validation_status", "NEEDS_DEVICE_VALIDATION")),
            exclude_reason=value.get("exclude_reason"),
            signal_strength=_optional_float(value.get("signal_strength")),
            repeatability=_optional_float(value.get("repeatability")),
            inter_class_margin=_optional_float(value.get("inter_class_margin")),
            valid_coverage=_optional_float(value.get("valid_coverage")),
            classification_result=_optional_float(value.get("classification_result")),
            quality_version=value.get("quality_version"),
        )

    def to_mapping(self) -> dict[str, Any]:
        value = {
            "operation_id": self.operation_id,
            "app_id": self.app_id,
            "module": self.module,
            "operation_family": self.operation_family,
            "display_name": self.display_name,
            "precondition": self.precondition,
            "action_spec": dict(self.action_spec),
            "postcondition": self.postcondition,
            "content_id": self.content_id,
            "state_variables": list(self.state_variables),
            "primary_candidate": self.primary_candidate,
            "primary_eval": self.primary_eval,
            "expected_signal": self.expected_signal,
            "requires_network": self.requires_network,
            "requires_fixed_content": self.requires_fixed_content,
            "automation_status": self.automation_status,
            "device_validation_status": self.device_validation_status,
            "exclude_reason": self.exclude_reason,
            "signal_strength": self.signal_strength,
            "repeatability": self.repeatability,
            "inter_class_margin": self.inter_class_margin,
            "valid_coverage": self.valid_coverage,
            "classification_result": self.classification_result,
            "quality_version": self.quality_version,
        }
        return value


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


@dataclass(frozen=True)
class OperationCatalog:
    app_id: str
    display_name: str
    schema_version: str
    operations: tuple[OperationSpec, ...]
    evaluation_sets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = [operation.operation_id for operation in self.operations]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate operation_id in catalog {self.app_id}")
        if any(operation.app_id != self.app_id for operation in self.operations):
            raise ValueError("operation app_id does not match catalog app_id")

    def get(self, operation_id: str) -> OperationSpec:
        for operation in self.operations:
            if operation.operation_id == operation_id:
                return operation
        raise KeyError(operation_id)

    def for_eval_set(self, eval_set: str = "full") -> tuple[OperationSpec, ...]:
        if eval_set in self.evaluation_sets:
            return tuple(self.get(operation_id) for operation_id in self.evaluation_sets[eval_set])
        if eval_set == "full":
            return self.operations
        if eval_set == "stable":
            return tuple(operation for operation in self.operations if operation.primary_eval)
        if eval_set == "primary":
            return tuple(operation for operation in self.operations if operation.primary_candidate)
        raise ValueError(f"unknown evaluation set: {eval_set}")


class AppAdapter(ABC):
    """Application behavior boundary; collection and evaluation do not depend on UI details."""

    @property
    @abstractmethod
    def app_id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def catalog(self) -> OperationCatalog:
        raise NotImplementedError

    def prepare(self, operation: OperationSpec, state: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(state)

    def postcondition(self, operation: OperationSpec, state: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(state)
