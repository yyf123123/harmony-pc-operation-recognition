"""One labelled Linux WPS sample; existing feature math and catalogue are reused."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from apps.wps.adapter import WpsAdapter
from collector.runtime.trial_state import TrialState, TrialStateMachine
from features.wps import map_fixed_window_sequence, build_vector, l2_normalize
from .procfs import CaptureError, LinuxProcfs


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or plan.get("schema_version") != "linux.wps.action-plan.v1":
        raise ValueError("unsupported action plan schema")
    WpsAdapter().catalog.get(plan["operation_id"])
    for key in ("precondition", "action", "verify", "focus"):
        argv = plan.get(key)
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in argv):
            raise ValueError(f"{key} must be an explicit argv list; shell strings are unsupported")
    for key in ("baseline_s", "action_s", "post_s", "command_timeout_s"):
        value = float(plan.get(key, {"baseline_s": 5, "action_s": 15, "post_s": 5, "command_timeout_s": 15}[key]))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
    return plan


def run_command(argv: list[str], timeout: float, name: str, *, output: bool = False) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureError(f"{name} failed or timed out") from error
    if result.returncode:
        raise CaptureError(f"{name} failed (exit {result.returncode})")
    # External action/verification output may contain document text. Do not log it.
    return result.stdout if output else ""


def capture_sample(*, plan: dict, backend: LinuxProcfs, output: Path, session_id: str,
                   trial_id: str, app_version: str, device_id: str,
                   collection_stage: str = "smoke", capture_context: dict | None = None) -> dict:
    validate_plan(plan)
    if not all((session_id, trial_id, app_version, device_id)):
        raise ValueError("session, trial, application version and anonymous device ID are required")
    if collection_stage not in {"smoke", "preflight", "formal"}:
        raise ValueError("unsupported collection stage")
    operation = WpsAdapter().catalog.get(plan["operation_id"])
    catalog = WpsAdapter().catalog
    label = str(operation.action_spec["legacy_label"])
    label_id = list(catalog.operations).index(operation)
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    for folder in ("raw", "metadata", "derived", "validation"):
        (output / folder).mkdir()
    sample_id = hashlib.sha256(f"{session_id}\0{trial_id}\0{operation.operation_id}\0{time.monotonic_ns()}".encode()).hexdigest()[:24]
    timeout = float(plan.get("command_timeout_s", 15))
    durations = {key: float(plan.get(key, default)) for key, default in (("baseline_s", 5), ("action_s", 15), ("post_s", 5))}
    page_size = os.sysconf("SC_PAGE_SIZE")
    machine = TrialStateMachine()
    windows: list[dict] = []
    metadata = {
        "schema_version": "linux.wps.capture.v1", "sample_id": sample_id,
        "session_id": session_id, "trial_id": trial_id, "app_id": "wps",
        "app_version": app_version, "device_id": device_id, "created_at": utc_now(),
        "kernel": platform.release(), "architecture": platform.machine(), "page_size_bytes": page_size,
        "processes": [asdict(item) for item in backend.identities],
        "process_coverage": "explicit_pid_set_only", "focus_coverage": "window_boundary_checks_only",
        "app_instance_id": hashlib.sha256(json.dumps([asdict(item) for item in backend.identities], sort_keys=True).encode()).hexdigest(),
        "action_plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        "collector_sha256": hashlib.sha256(backend.collector.read_bytes()).hexdigest(),
        "capture_profile": {"name": "linux.wps.fixed_windows.v1", "baseline_count": 2, **durations},
        "feature_schema": "wps.vma.feature.v1", "source": getattr(backend, "source", "linux-procfs"),
        "is_mock": getattr(backend, "is_mock", False), "app_version_source": "user-provided",
        "operation_id": operation.operation_id, "operation_label": label, "label_id": label_id,
        "label_schema": catalog.schema_version, "collection_stage": collection_stage,
        "capture_context": dict(capture_context or {}),
    }
    root_pid = getattr(backend, "root_pid", None)
    if root_pid is not None:
        root = next(item for item in backend.identities if item.pid == root_pid)
        metadata.update(root_pid=root_pid, app_instance_id=f"wps:{root.pid}:{root.start_ticks}",
                        process_coverage="explicit_authorized_visible_process_tree_at_boundaries")
    save_json(output / "metadata/session.json", metadata)

    def focus() -> dict:
        try:
            evidence = json.loads(run_command(plan["focus"], timeout, "focus", output=True))
            if (type(evidence["pid"]) is not int or evidence["pid"] not in backend.pids
                    or not isinstance(evidence["source"], str) or not evidence["source"]
                    or not isinstance(evidence["window_id"], str) or not evidence["window_id"]):
                raise ValueError("focus is not a selected application process")
            return {key: evidence[key] for key in ("pid", "source", "window_id")}
        except (KeyError, TypeError, ValueError) as error:
            raise CaptureError("independent focus evidence does not identify a selected PID") from error

    def window(phase: str, duration: float, action: bool = False) -> dict:
        record = {"window_id": f"{sample_id}.{phase}", "segment_label": phase,
                  "window_kind": "BASELINE" if phase.startswith("BASELINE") else "OPERATION",
                  "status": "running", "window_started_at": utc_now()}
        windows.append(record)
        record["focus_start"] = focus()
        started = time.monotonic_ns()
        record["start_monotonic_ns"] = started
        backend.clear()
        record["T_clear_refs_s"] = (time.monotonic_ns() - started) / 1e9
        if action:
            record["action_started_at"] = utc_now()
            record["action_start_monotonic_ns"] = time.monotonic_ns()
            run_command(plan["action"], timeout, "action")
            record["action_end_monotonic_ns"] = time.monotonic_ns()
            record["action_ended_at"] = utc_now()
            machine.advance(TrialState.ACTION_CONFIRM)
            run_command(plan["verify"], timeout, "action verification")
            record["verification_end_monotonic_ns"] = time.monotonic_ns()
        elapsed = (time.monotonic_ns() - started) / 1e9
        if elapsed > duration:
            raise CaptureError(f"{phase} exceeded the configured observation interval")
        time.sleep(duration - elapsed)
        # Never clear_refs between the action and this capture.
        capture_start = time.monotonic_ns()
        record.update(backend.sample(output / "raw" / f"{phase}.tsv", page_size))
        record["T_device_sampling_s"] = (time.monotonic_ns() - capture_start) / 1e9
        record["end_monotonic_ns"] = time.monotonic_ns()
        record["focus_end"] = focus()
        # A valid create/close operation may change the active WPS window.
        # Both boundary PIDs must be selected; this does not prove continuous focus.
        record["focus_window_changed"] = record["focus_start"] != record["focus_end"]
        record["window_ended_at"] = utc_now()
        record["status"] = "success"
        record["report_count"] = len(record["report_paths"])
        record["collection_quality"] = "pass"
        save_json(output / "raw" / f"{phase}.json", record)
        return record

    try:
        machine.advance(TrialState.PRECONDITION_CHECK)
        backend.check()
        run_command(plan["precondition"], timeout, "precondition")
        focus()
        machine.advance(TrialState.BASELINE)
        baselines = [window(f"BASELINE_{number:02d}", durations["baseline_s"]) for number in (1, 2)]
        machine.advance(TrialState.ACTION)
        action = window("ACTION", durations["action_s"], True)
        machine.advance(TrialState.POST_ACTION)
        post = window("POST_ACTION", durations["post_s"])
        machine.advance(TrialState.QUALITY_CHECK)
        sequence = map_fixed_window_sequence(
            operation_execution_id=sample_id, operation_id=label, baseline_group_id=sample_id + ".baseline",
            baseline_windows=baselines, operation_windows=[action, post])
        maps = {namespace: {row["feature_key"]: row["aggregated_excess_pages"]
                            for row in sequence["aggregated_features"] if row["feature_namespace"] == namespace}
                for namespace in ("FILE", "ANON")}
        vector = l2_normalize(build_vector(maps["FILE"], maps["ANON"]))
        if not any(vector) or not all(math.isfinite(value) for value in vector):
            raise CaptureError("zero or nonfinite feature; no prediction or valid sample produced")
        sample = {**metadata, "schema_version": "wps.operation-sample.v1", "status": "success",
                  "execution_id": sample_id, "precondition": operation.precondition,
                  "baseline_window_count": 2, "action_window_s": durations["action_s"], "post_window_s": durations["post_s"],
                  "baseline_windows": baselines, "operation_windows": [action, post], "sequence": sequence,
                  "sample_started_at": baselines[0]["window_started_at"], "sample_ended_at": post["window_ended_at"],
                  "collector_version": "linux-native-wps-v1"}
        machine.advance(TrialState.SAVE)
        save_json(output / "derived/sample.json", sample)
        with (output / "operation_window_sequences.jsonl").open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
        save_json(output / "validation/status.json", {"status": "success", "states": [str(state) for state in machine.history]})
        return sample
    except Exception as error:
        machine.fail()
        save_json(output / "validation/status.json", {"status": "failed", "error": str(error),
                  "states": [str(state) for state in machine.history], "windows": windows})
        raise
