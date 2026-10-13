"""Shared HDC trial utilities; application adapters own only UI-specific actions."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apps.preconditions import PreconditionFailed
from collector.automation.hdc_backend import HdcBackend, HdcError
from collector.smaps_collector import SmapsEmptyError, collect_snapshot_from_smaps
from collector.dataset.manifest import DatasetLayout
from collector.runtime.timers import TimingRecorder
from collector.runtime.trial_state import TrialErrorCode, TrialState


def now_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:10]}"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_hdc_argument(value: str | list[str] | tuple[str, ...] | None) -> str | None:
    """Reassemble a Windows HDC path split by ``Start-Process``."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    result = " ".join(str(item) for item in value).strip()
    return result or None


def arm_reference_window(
    backend: HdcBackend,
    app_id: str,
    expected_pid: str | None,
    *,
    timing: TimingRecorder | None = None,
) -> str:
    """Clear referenced bits before a window, never immediately before capture."""
    pid = backend.ensure_process_alive(app_id, previous_pid=expected_pid)
    if expected_pid is not None and pid != expected_pid:
        raise HdcError("PROCESS_RESTARTED", f"process PID changed during trial: {expected_pid}->{pid}")
    if timing is None:
        backend.clear_refs(pid)
    else:
        with timing.measure("T_clear_refs"):
            backend.clear_refs(pid)
    return pid


def snapshot(
    backend: HdcBackend,
    layout: DatasetLayout,
    app_id: str,
    trial_id: str,
    phase: str,
    expected_pid: str | None,
    *,
    clear_before_capture: bool = True,
    timing: TimingRecorder | None = None,
) -> tuple[Any, Path, str]:
    """Capture one real smaps snapshot and persist its raw JSON payload."""
    pid = backend.ensure_process_alive(app_id, previous_pid=expected_pid)
    if expected_pid is not None and pid != expected_pid:
        raise HdcError("PROCESS_RESTARTED", f"process PID changed during trial: {expected_pid}->{pid}")
    if clear_before_capture:
        if timing is None:
            backend.clear_refs(pid)
        else:
            with timing.measure("T_clear_refs"):
                backend.clear_refs(pid)
    if timing is None:
        smaps = backend.capture_smaps(pid)
        item = collect_snapshot_from_smaps(
            smaps,
            snapshot_id=f"{trial_id}_{phase.lower()}_{datetime.now(timezone.utc).strftime('%f')}",
            app_id=app_id,
            phase=phase,
            source="hdc",
            is_mock=False,
            pid=pid,
        )
    else:
        with timing.measure("T_device_sampling"):
            smaps = backend.capture_smaps(pid)
        with timing.measure("T_parse"):
            item = collect_snapshot_from_smaps(
                smaps,
                snapshot_id=f"{trial_id}_{phase.lower()}_{datetime.now(timezone.utc).strftime('%f')}",
                app_id=app_id,
                phase=phase,
                source="hdc",
                is_mock=False,
                pid=pid,
            )
    payload = {
        **asdict(item),
        "regions": [asdict(region) for region in item.regions],
        "metadata": {**item.metadata, "target": backend.target, "pid": pid, "source": "hdc", "is_mock": False},
    }
    path = layout.write_raw_json("smaps", item.snapshot_id, payload)
    return item, path, pid


def feature_payload(feature: Any) -> dict[str, Any]:
    return {
        "sample_id": feature.sample_id,
        "app_id": feature.app_id,
        "operation_id": feature.operation_id,
        "schema_version": feature.schema_version,
        "raw_vector": list(feature.raw_vector),
        "l2_vector": list(feature.l2_vector),
        "file_features": feature.file_features,
        "anon_features": feature.anon_features,
        "channel_features": feature.channel_features,
        "signal_strength": feature.signal_strength,
        "metadata": feature.metadata,
        "is_mock": False,
    }


def feature_identifier(trial_id: str, schema_version: str) -> str:
    return f"{trial_id}__{schema_version.replace('.', '_')}"


def failure_code(error: BaseException, state: TrialState) -> TrialErrorCode:
    if isinstance(error, PreconditionFailed):
        return TrialErrorCode.PRECONDITION_FAILED
    if isinstance(error, SmapsEmptyError):
        return TrialErrorCode.SMAPS_EMPTY
    if isinstance(error, HdcError):
        try:
            return TrialErrorCode(error.code)
        except ValueError:
            return TrialErrorCode.CAPTURE_FAILED
    if state == TrialState.ACTION_CONFIRM:
        return TrialErrorCode.ACTION_NOT_CONFIRMED
    if state == TrialState.ACTION:
        return TrialErrorCode.ACTION_FAILED
    if state == TrialState.QUALITY_CHECK:
        return TrialErrorCode.FEATURE_FAILED
    return TrialErrorCode.CAPTURE_FAILED
