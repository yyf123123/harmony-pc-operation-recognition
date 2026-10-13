"""Load and validate JSON-compatible YAML operation catalogs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import OperationCatalog, OperationSpec


def load_catalog(path: str | Path) -> OperationCatalog:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("operations"), list):
        raise ValueError(f"invalid operation catalog: {path}")
    operations = tuple(OperationSpec.from_mapping(item) for item in raw["operations"])
    app_id = str(raw.get("app_id") or operations[0].app_id)
    evaluation_sets = {
        str(name): tuple(str(item) for item in items)
        for name, items in (raw.get("evaluation_sets") or {}).items()
    }
    catalog = OperationCatalog(
        app_id=app_id,
        display_name=str(raw.get("display_name", app_id)),
        schema_version=str(raw.get("schema_version", "cross-app.operation-catalog.v1")),
        operations=operations,
        evaluation_sets=evaluation_sets,
    )
    _validate_catalog(catalog)
    return catalog


def _validate_catalog(catalog: OperationCatalog) -> None:
    for operation in catalog.operations:
        if not operation.operation_id.startswith(f"{catalog.app_id}."):
            raise ValueError(f"operation_id is not namespaced: {operation.operation_id}")
        if not operation.precondition or not operation.postcondition:
            raise ValueError(f"operation has incomplete state contract: {operation.operation_id}")
    for name, operation_ids in catalog.evaluation_sets.items():
        unknown = set(operation_ids) - {operation.operation_id for operation in catalog.operations}
        if unknown:
            raise ValueError(f"evaluation set {name} contains unknown operations: {sorted(unknown)}")
