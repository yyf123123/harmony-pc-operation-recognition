#!/usr/bin/env python3
"""Collect labelled WPS operation windows and build the VMA dataset.

One trial is deliberately serial:

    start WPS -> NEW_DOCUMENT -> WRITE_TEXT -> preparation Save As
    -> dirty marker -> SAVE_DOCUMENT -> CLOSE_DOCUMENT -> force-stop

The default is 6 trials, i.e. 108 labelled operation samples across 18
common Writer operations. Use ``--trials 1`` for a device smoke run and
``--trials 25`` for a longer 450-sample collection.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from features.wps import build_dataset, map_fixed_window_sequence
from collector.wps.session import BUNDLE, HdcError, Session, now_iso


DEFAULT_BASELINE_WINDOW_COUNT = 2
DEFAULT_BASELINE_WINDOW_S = 5.0
DEFAULT_ACTION_WINDOW_S = 15.0
DEFAULT_POST_WINDOW_S = 5.0
DEFAULT_TRIALS = 6


def _safe_json(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _next_trial_number(root: Path) -> int:
    numbers = []
    for path in root.glob("trial_*"):
        try:
            numbers.append(int(path.name.split("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max(numbers, default=0) + 1


def _split_reports(value: str) -> list[str]:
    return [item for item in value.split(";") if item]


def _relative_reports(paths: list[str], trial_dir: Path) -> list[str]:
    result = []
    for path in paths:
        try:
            result.append(str(Path(path).resolve().relative_to(trial_dir.resolve())))
        except ValueError:
            result.append(path)
    return result


class DatasetSession(Session):
    """Small fixed-window adapter around the existing WPS v6 Session."""

    def __init__(self, args: argparse.Namespace):
        super().__init__(args)
        self.window_counter = 0

    def _new_window_id(self, execution_id: str, segment_index: int, segment_label: str) -> str:
        self.window_counter += 1
        return f"{execution_id}_{segment_index:02d}_{segment_label.lower()}_{self.window_counter:03d}"

    def _sample_window(
        self,
        *,
        window_id: str,
        operation_id: str,
        segment_index: int,
        segment_label: str,
        window_kind: str,
        action_started_at: str,
        action_ended_at: str,
        window_started_at: str,
        window_started: float,
        action_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        sample = self.sample(segment_index, f"{operation_id}_{segment_label}")
        report_paths = _split_reports(str(sample.get("report", "")))
        return {
            "window_id": window_id,
            "operation_id": operation_id,
            "segment_index": segment_index,
            "segment_label": segment_label,
            "window_kind": window_kind,
            "status": "success",
            "window_started_at": window_started_at,
            "window_ended_at": now_iso(),
            "window_elapsed_s": round(time.perf_counter() - window_started, 6),
            "action_started_at": action_started_at,
            "action_ended_at": action_ended_at,
            "action_metadata": action_metadata,
            "report_paths": report_paths,
            "report_count": int(sample.get("report_count", 0) or 0),
            "hash_mismatch_count": int(sample.get("hash_mismatch_count", 0) or 0),
            "collection_quality": "pass" if not sample.get("hash_mismatch_count", 0) else "hash_mismatch",
        }

    def collect_fixed_baselines(
        self,
        *,
        operation_execution_id: str,
        operation_id: str,
        baseline_state: str,
        baseline_window_count: int,
        baseline_window_s: float,
    ) -> tuple[str, list[dict[str, Any]]]:
        baseline_group_id = f"{operation_execution_id}_baseline"
        baselines: list[dict[str, Any]] = []
        for index in range(1, baseline_window_count + 1):
            window_id = self._new_window_id(operation_execution_id, 0, f"BASELINE_{index:02d}")
            started_at = now_iso()
            started = time.perf_counter()
            try:
                self.clear_refs()
                time.sleep(max(baseline_window_s, 0.0))
                window = self._sample_window(
                    window_id=window_id,
                    operation_id=operation_id,
                    segment_index=0,
                    segment_label=f"BASELINE_{index:02d}",
                    window_kind="BASELINE",
                    action_started_at="",
                    action_ended_at="",
                    window_started_at=started_at,
                    window_started=started,
                    action_metadata={"baseline_state": baseline_state, "baseline_index": index},
                )
            except (HdcError, OSError, RuntimeError, TimeoutError) as exc:
                window = {
                    "window_id": window_id,
                    "operation_id": operation_id,
                    "segment_index": 0,
                    "segment_label": f"BASELINE_{index:02d}",
                    "window_kind": "BASELINE",
                    "status": "failed",
                    "window_started_at": started_at,
                    "window_ended_at": now_iso(),
                    "window_elapsed_s": round(time.perf_counter() - started, 6),
                    "action_started_at": "",
                    "action_ended_at": "",
                    "action_metadata": {"baseline_state": baseline_state, "baseline_index": index},
                    "report_paths": [],
                    "report_count": 0,
                    "hash_mismatch_count": 0,
                    "error": str(exc),
                }
            baselines.append(window)
            if window["status"] != "success":
                raise HdcError(f"baseline window failed: {window['window_id']}: {window.get('error', '')}")
        return baseline_group_id, baselines

    def run_fixed_window(
        self,
        *,
        operation_execution_id: str,
        operation_id: str,
        segment_index: int,
        segment_label: str,
        window_kind: str,
        target_duration_s: float,
        action_callback: Callable[[], Any],
        baseline_group_id: str,
        action_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        window_id = self._new_window_id(operation_execution_id, segment_index, segment_label)
        window_started_at = now_iso()
        window_started = time.perf_counter()
        before = self.snapshot()
        if not before:
            raise HdcError(f"{operation_id}/{segment_label} 前未发现 WPS 进程")
        self.clear_refs()
        action_started_at = now_iso()
        action_started = time.perf_counter()
        action_result: Any = None
        try:
            action_result = action_callback()
        finally:
            action_ended_at = now_iso()
        elapsed = time.perf_counter() - window_started
        time.sleep(max(float(target_duration_s) - elapsed, 0.0))
        metadata = {
            **action_metadata,
            "baseline_group_id": baseline_group_id,
            "action_result": action_result or {},
        }
        window = self._sample_window(
            window_id=window_id,
            operation_id=operation_id,
            segment_index=segment_index,
            segment_label=segment_label,
            window_kind=window_kind,
            action_started_at=action_started_at,
            action_ended_at=action_ended_at,
            window_started_at=window_started_at,
            window_started=window_started,
            action_metadata=metadata,
        )
        window["baseline_group_id"] = baseline_group_id
        window["before_process_count"] = len(before)
        return window


def collect_labeled_operation(
    session: DatasetSession,
    *,
    trial_id: str,
    label_id: int,
    operation_label: str,
    precondition: str,
    catalog_entry: dict[str, Any] | None = None,
    action_callback: Callable[[], Any],
    action_window_s: float,
    post_window_s: float,
    baseline_window_count: int,
    baseline_window_s: float,
    device_target: str,
    system_version: str,
    wps_version: str,
) -> dict[str, Any]:
    execution_id = f"{session.session_id}_{trial_id}_{operation_label.lower()}"
    sample_id = f"wps_{trial_id}_{operation_label.lower()}"
    baseline_group_id, baselines = session.collect_fixed_baselines(
        operation_execution_id=execution_id,
        operation_id=operation_label,
        baseline_state=precondition,
        baseline_window_count=baseline_window_count,
        baseline_window_s=baseline_window_s,
    )
    action_window = session.run_fixed_window(
        operation_execution_id=execution_id,
        operation_id=operation_label,
        segment_index=1,
        segment_label="ACTION",
        window_kind="OPERATION",
        target_duration_s=action_window_s,
        action_callback=action_callback,
        baseline_group_id=baseline_group_id,
        action_metadata={
            "trial_id": trial_id,
            "sample_id": sample_id,
            "label_id": label_id,
            "operation_label": operation_label,
            "operation_category": (catalog_entry or {}).get("category", ""),
            "catalog_description": (catalog_entry or {}).get("description", ""),
            "input_method": (catalog_entry or {}).get("input_method", ""),
            "phase": "ACTION",
        },
    )
    post_window = session.run_fixed_window(
        operation_execution_id=execution_id,
        operation_id=operation_label,
        segment_index=2,
        segment_label="POST_ACTION",
        window_kind="OPERATION",
        target_duration_s=post_window_s,
        action_callback=lambda: None,
        baseline_group_id=baseline_group_id,
        action_metadata={
            "trial_id": trial_id,
            "sample_id": sample_id,
            "label_id": label_id,
            "operation_label": operation_label,
            "operation_category": (catalog_entry or {}).get("category", ""),
            "catalog_description": (catalog_entry or {}).get("description", ""),
            "input_method": (catalog_entry or {}).get("input_method", ""),
            "phase": "POST_ACTION",
        },
    )
    mapping = map_fixed_window_sequence(
        operation_execution_id=execution_id,
        operation_id=operation_label,
        baseline_group_id=baseline_group_id,
        baseline_windows=baselines,
        operation_windows=[action_window, post_window],
    )
    for window in [*baselines, action_window, post_window]:
        window["report_paths"] = _relative_reports(window.get("report_paths", []), session.local_out)
    for window in mapping.get("operation_windows", []):
        window["report_paths"] = _relative_reports(window.get("report_paths", []), session.local_out)
    windows = [*baselines, action_window, post_window]
    return {
        "schema_version": "wps.operation-sample.v1",
        "status": "success",
        "sample_id": sample_id,
        "trial_id": trial_id,
        "session_id": session.session_id,
        "label_id": label_id,
        "operation_label": operation_label,
        "precondition": precondition,
        "execution_id": execution_id,
        "baseline_group_id": baseline_group_id,
        "baseline_window_count": baseline_window_count,
        "action_window_s": action_window_s,
        "post_window_s": post_window_s,
        "baseline_windows": baselines,
        "operation_windows": [action_window, post_window],
        "sequence": mapping,
        "sample_started_at": min(item["window_started_at"] for item in windows),
        "sample_ended_at": max(item["window_ended_at"] for item in windows),
        "document_path": str((session.saved_document or {}).get("final_path") or (session.saved_document or {}).get("path") or ""),
        "device_target": device_target,
        "system_version": system_version,
        "wps_version": wps_version,
        "collector_version": "mem_analyze-v6-with-vma",
    }


def _session_namespace(args: argparse.Namespace, trial_dir: Path, session_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        target=args.target,
        out=str(trial_dir),
        device_dir=args.device_dir,
        device_out=args.device_out,
        session_id=session_id,
        no_build=args.no_build,
        collector_binary=args.collector_binary,
        launch_wait_s=args.launch_wait_s,
        editor_x=args.editor_x,
        editor_y=args.editor_y,
        heavy_repeats=0,
        test_serial=args.test_serial,
    )


def _device_metadata(session: DatasetSession) -> tuple[str, str]:
    system_version = session.device.shell("uname -a", check=False)
    wps_version = session.device.shell(f"bm dump -n {BUNDLE} 2>/dev/null | head -20", check=False)
    return system_version, wps_version


def _load_operation_catalog(script_dir: Path) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "apps/wps/catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    operations = catalog.get("operations")
    if not isinstance(operations, list) or len(operations) < 10:
        raise ValueError("WPS operation catalog must contain at least 10 operations")
    labels = [item.get("label") for item in operations]
    ids = [item.get("label_id") for item in operations]
    if len(set(labels)) != len(labels) or len(set(ids)) != len(ids):
        raise ValueError("WPS operation catalog contains duplicate labels or label_id values")
    return catalog


def _operation_plan(
    session: DatasetSession,
    trial_number: int,
) -> list[tuple[dict[str, Any], Callable[[], Any]]]:
    """Return the stable serial Writer action plan for one trial.

    SAVE_DOCUMENT and CLOSE_DOCUMENT are appended by ``_run_trial`` because
    Save As must first establish a per-trial existing path. The plan keeps
    setup actions outside the measured ACTION window so every labelled sample
    still contains exactly one target operation.
    """
    return [
        ({"label_id": 0, "label": "NEW_DOCUMENT", "precondition": "WPS_HOME_IDLE"}, session.new_word),
        (
            {"label_id": 1, "label": "WRITE_TEXT", "precondition": "BLANK_DOCUMENT_IDLE"},
            lambda: session.write_dataset_text(f"WPS_VMA_WRITE_{trial_number:03d}_001"),
        ),
        ({"label_id": 4, "label": "SELECT_ALL", "precondition": "TEXT_DOCUMENT_IDLE"}, session.select_all),
        ({"label_id": 5, "label": "COPY_SELECTION", "precondition": "TEXT_SELECTED_IDLE"}, session.copy_selection),
        ({"label_id": 6, "label": "PASTE_SELECTION", "precondition": "TEXT_SELECTED_IDLE"}, session.paste_selection),
        ({"label_id": 7, "label": "CUT_SELECTION", "precondition": "TEXT_SELECTED_IDLE"}, session.cut_selection),
        ({"label_id": 8, "label": "UNDO_EDIT", "precondition": "DIRTY_DOCUMENT_IDLE"}, session.undo_edit),
        ({"label_id": 9, "label": "REDO_EDIT", "precondition": "UNDO_AVAILABLE_IDLE"}, session.redo_edit),
        ({"label_id": 10, "label": "FIND_TEXT", "precondition": "TEXT_DOCUMENT_IDLE"}, session.find_text),
        ({"label_id": 11, "label": "REPLACE_TEXT", "precondition": "TEXT_DOCUMENT_IDLE"}, session.replace_text),
        ({"label_id": 12, "label": "INSERT_PAGE_BREAK", "precondition": "TEXT_DOCUMENT_IDLE"}, session.insert_page_break),
        ({"label_id": 13, "label": "INSERT_TABLE", "precondition": "TEXT_DOCUMENT_IDLE"}, session.insert_table),
        ({"label_id": 14, "label": "FORMAT_BOLD", "precondition": "TEXT_SELECTED_IDLE"}, session.format_bold),
        ({"label_id": 15, "label": "FORMAT_ITALIC", "precondition": "TEXT_SELECTED_IDLE"}, session.format_italic),
        ({"label_id": 16, "label": "FORMAT_UNDERLINE", "precondition": "TEXT_SELECTED_IDLE"}, session.format_underline),
        ({"label_id": 17, "label": "ALIGN_CENTER", "precondition": "TEXT_SELECTED_IDLE"}, session.align_center),
    ]


def _append_sequence(root: Path, sequence: dict[str, Any]) -> None:
    path = root / "operation_window_sequences.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_safe_json(sequence), ensure_ascii=False, sort_keys=True) + "\n")


def _run_trial(args: argparse.Namespace, root: Path, trial_number: int) -> dict[str, Any]:
    trial_id = f"trial_{trial_number:03d}"
    trial_dir = root / trial_id
    trial_dir.mkdir(parents=True, exist_ok=False)
    session_id = f"wps_v6_dataset_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{trial_number:03d}"
    session: DatasetSession | None = None
    trial_record: dict[str, Any] = {
        "trial_id": trial_id,
        "trial_dir": trial_id,
        "session_id": session_id,
        "status": "failed",
        "started_at": now_iso(),
        "operation_samples": [],
    }
    try:
        session = DatasetSession(_session_namespace(args, trial_dir, session_id))
        session.verify_access()
        session.build_and_push()
        session.record_document_baseline()
        session.force_stop_wps()
        session.start_wps()
        time.sleep(args.launch_wait_s)
        if not session.snapshot():
            raise HdcError("启动 WPS 后未发现进程")
        system_version, wps_version = _device_metadata(session)
        trial_record.update({
            "device_target": session.device.target,
            "system_version": system_version,
            "wps_version": wps_version,
            "startup_state": "WPS_HOME_IDLE",
        })

        catalog = _load_operation_catalog(session.script_dir)
        catalog_by_label = {item["label"]: item for item in catalog["operations"]}
        operation_plan = _operation_plan(session, trial_number)
        planned_labels = {str(spec["label"]) for spec, _ in operation_plan} | {
            "SAVE_DOCUMENT",
            "CLOSE_DOCUMENT",
        }
        if planned_labels != set(catalog_by_label):
            missing = sorted(set(catalog_by_label) - planned_labels)
            extra = sorted(planned_labels - set(catalog_by_label))
            raise ValueError(f"operation catalog/runner mismatch; missing={missing}, extra={extra}")
        trial_record.update({
            "catalog_schema_version": catalog.get("schema_version", ""),
            "formal_samples_expected": len(operation_plan) + 2,
        })
        for spec, action in operation_plan:
            label = str(spec["label"])
            label_id = int(spec["label_id"])
            precondition = str(spec["precondition"])
            catalog_entry = {**catalog_by_label.get(label, {}), **spec}
            # These operations require an explicit selected-text precondition.
            # Setup is outside the measured window, so the ACTION window still
            # contains exactly one labelled operation.
            if label in {"COPY_SELECTION", "PASTE_SELECTION", "CUT_SELECTION"}:
                session.select_all()
            if label == "UNDO_EDIT":
                session.write_dataset_text(f"WPS_VMA_UNDO_{trial_number:03d}_001")
            if label == "FIND_TEXT":
                session.write_dataset_text("WPS_FIND_TARGET")
            if label in {"FORMAT_BOLD", "FORMAT_ITALIC", "FORMAT_UNDERLINE", "ALIGN_CENTER"}:
                session.select_all()
            sample = collect_labeled_operation(
                session,
                trial_id=trial_id,
                label_id=label_id,
                operation_label=label,
                precondition=precondition,
                catalog_entry=catalog_entry,
                action_callback=action,
                action_window_s=args.action_window_s,
                post_window_s=args.post_window_s,
                baseline_window_count=args.baseline_window_count,
                baseline_window_s=args.baseline_window_s,
                device_target=session.device.target,
                system_version=system_version,
                wps_version=wps_version,
            )
            _append_sequence(root, sample)
            trial_record["operation_samples"].append(sample["sample_id"])

        # First Save As establishes a stable path and is deliberately not a
        # SAVE_DOCUMENT sample.  The formal sample measures ordinary save.
        session.save_document(verify_content=False)
        session.write_dataset_text(f"WPS_VMA_DIRTY_{trial_number:03d}_001")
        time.sleep(2.0)
        save_sample = collect_labeled_operation(
            session,
            trial_id=trial_id,
            label_id=2,
            operation_label="SAVE_DOCUMENT",
            precondition="DIRTY_SAVED_DOCUMENT_IDLE",
            catalog_entry=catalog_by_label["SAVE_DOCUMENT"],
            action_callback=session.save_existing_document,
            action_window_s=args.action_window_s,
            post_window_s=args.post_window_s,
            baseline_window_count=args.baseline_window_count,
            baseline_window_s=args.baseline_window_s,
            device_target=session.device.target,
            system_version=system_version,
            wps_version=wps_version,
        )
        _append_sequence(root, save_sample)
        trial_record["operation_samples"].append(save_sample["sample_id"])

        close_sample = collect_labeled_operation(
            session,
            trial_id=trial_id,
            label_id=3,
            operation_label="CLOSE_DOCUMENT",
            precondition="SAVED_DOCUMENT_IDLE",
            catalog_entry=catalog_by_label["CLOSE_DOCUMENT"],
            action_callback=session.close_document,
            action_window_s=args.action_window_s,
            post_window_s=args.post_window_s,
            baseline_window_count=args.baseline_window_count,
            baseline_window_s=args.baseline_window_s,
            device_target=session.device.target,
            system_version=system_version,
            wps_version=wps_version,
        )
        _append_sequence(root, close_sample)
        trial_record["operation_samples"].append(close_sample["sample_id"])
        trial_record["status"] = "success"
        return trial_record
    except (HdcError, OSError, RuntimeError, TimeoutError) as exc:
        trial_record["error"] = str(exc)
        (trial_dir / "trial_failure.json").write_text(json.dumps(_safe_json(trial_record), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return trial_record
    finally:
        trial_record["ended_at"] = now_iso()
        if session is not None:
            try:
                session.force_stop_wps()
            except Exception as exc:  # cleanup must not hide the trial result
                trial_record.setdefault("cleanup_error", str(exc))
            try:
                session.write_memory_summary()
            except Exception as exc:
                trial_record.setdefault("metadata_error", str(exc))
            session.close_files()
        (trial_dir / "trial_metadata.json").write_text(json.dumps(_safe_json(trial_record), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="新的外部数据集根目录")
    parser.add_argument("--target", default=os.environ.get("HDC_TARGET", ""), help="hdc target；单设备时可省略")
    parser.add_argument(
        "--trials",
        type=int,
        default=DEFAULT_TRIALS,
        help="完整 trial 数；默认 6，即 108 个正式操作样本",
    )
    parser.add_argument("--action-window-s", type=float, default=DEFAULT_ACTION_WINDOW_S)
    parser.add_argument("--post-window-s", type=float, default=DEFAULT_POST_WINDOW_S)
    parser.add_argument("--baseline-window-count", type=int, default=DEFAULT_BASELINE_WINDOW_COUNT)
    parser.add_argument("--baseline-window-s", type=float, default=DEFAULT_BASELINE_WINDOW_S)
    parser.add_argument("--launch-wait-s", type=float, default=10.0)
    parser.add_argument("--device-dir", default="/data/local/tmp/harmony_collector")
    parser.add_argument("--device-out", default="/data/local/tmp/harmony_collector/wps_reports")
    parser.add_argument("--editor-x", type=int, default=1100)
    parser.add_argument("--editor-y", type=int, default=1020)
    parser.add_argument("--test-serial", default="WPS-DATASET-0001")
    parser.add_argument("--no-build", action="store_true", help="Use a previously built collector")
    parser.add_argument("--collector-binary", type=Path, help="Previously built device collector path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.trials < 1:
        raise SystemExit("--trials 必须 >= 1")
    if args.baseline_window_count < 1:
        raise SystemExit("--baseline-window-count 必须 >= 1")
    script_dir = Path(__file__).resolve().parent
    root = args.out.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    catalog_path = Path(__file__).resolve().parents[2] / "apps/wps/catalog.json"
    destination_catalog = root / "operation_catalog.json"
    if not destination_catalog.exists():
        destination_catalog.write_text(catalog_path.read_text(encoding="utf-8"), encoding="utf-8")
    if not (root / "operation_window_sequences.jsonl").exists():
        (root / "operation_window_sequences.jsonl").touch()

    trial_results = []
    next_trial = _next_trial_number(root)
    catalog = _load_operation_catalog(script_dir)
    formal_samples_per_trial = len(catalog["operations"])
    for offset in range(args.trials):
        trial_number = next_trial + offset
        print(f"[wps-dataset] trial {offset + 1}/{args.trials}: trial_{trial_number:03d}", flush=True)
        result = _run_trial(args, root, trial_number)
        trial_results.append(result)
        status_line = f"[wps-dataset] trial_{trial_number:03d}: {result['status']}"
        if result["status"] != "success" and result.get("error"):
            status_line += f" | {result['error']}"
        print(status_line, flush=True)

    build_error = ""
    summary: dict[str, Any] = {}
    try:
        summary = build_dataset(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        build_error = str(exc)
        print(f"[wps-dataset] dataset build failed: {exc}", file=sys.stderr, flush=True)
    metadata = {
        "schema_version": "wps.operation-dataset-run.v1",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": args.target,
        "trials_requested": args.trials,
        "formal_samples_per_trial": formal_samples_per_trial,
        "formal_samples_expected": args.trials * formal_samples_per_trial,
        "action_window_s": args.action_window_s,
        "post_window_s": args.post_window_s,
        "baseline_window_count": args.baseline_window_count,
        "baseline_window_s": args.baseline_window_s,
        "trial_results": trial_results,
        "dataset_summary": summary,
        "build_error": build_error,
        "notes": [
            "第一轮 Save As 仅用于建立 SAVE_DOCUMENT 的既有路径，不计入正式 SAVE_DOCUMENT 样本。",
            f"每个 trial 包含 {formal_samples_per_trial} 个 WPS Writer 常用操作；默认 6 个 trial 产生至少 100 个正式带标签样本。",
            "每个正式操作保留两个 baseline、一个 ACTION 和一个 POST_ACTION 窗口；ACTION/POST_ACTION 的同一语义特征取最大 baseline-relative excess pages。",
            "失败 trial 目录保留；重复执行同一输出目录会从下一个 trial 编号继续，不覆盖原始报告。",
        ],
    }
    (root / "dataset_run_metadata.json").write_text(json.dumps(_safe_json(metadata), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if build_error or any(item["status"] != "success" for item in trial_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
