"""Linux WPS collect/replay entry point. No device or automatic UI actions on import."""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
from pathlib import Path

from collector.linux.procfs import CaptureError, LinuxProcfs
from collector.linux.history import event_from_prediction
from collector.linux.session import capture_sample, validate_plan, save_json
from features.wps import build_vector, l2_normalize, map_fixed_window_sequence


def export_sample(sample: dict, output: Path) -> None:
    if sample.get("status") != "success" or sample.get("feature_schema") != "wps.vma.feature.v1":
        raise ValueError("only complete Linux WPS samples can be replayed")
    windows = [*sample["baseline_windows"], *sample["operation_windows"]]
    if (len(windows) != 4 or any(window.get("status") != "success" for window in windows)
            or {window["segment_label"] for window in windows} != {"BASELINE_01", "BASELINE_02", "ACTION", "POST_ACTION"}):
        raise ValueError("incomplete window set")
    for window in windows:
        if not window.get("feature_pages") or any(not math.isfinite(float(value)) or float(value) < 0
                                                    for value in window["feature_pages"].values()):
            raise ValueError("invalid window features")
    # Rebuild from the saved observation windows, not an unverified cached aggregate.
    sequence = map_fixed_window_sequence(
        operation_execution_id=sample["execution_id"], operation_id=sample["operation_label"],
        baseline_group_id=sample["sample_id"] + ".baseline", baseline_windows=sample["baseline_windows"],
        operation_windows=sample["operation_windows"])
    maps = {namespace: {} for namespace in ("FILE", "ANON")}
    for row in sequence["aggregated_features"]:
        maps[row["feature_namespace"]][row["feature_key"]] = float(row["aggregated_excess_pages"])
    vector = l2_normalize(build_vector(maps["FILE"], maps["ANON"]))
    if not any(vector) or not all(math.isfinite(value) for value in vector):
        raise ValueError("zero or nonfinite feature vector")
    output.mkdir(parents=True, exist_ok=False)
    with (output / "aggregated_features.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("namespace", "feature_key", "pages"))
        writer.writerows((namespace, key, pages) for namespace, values in maps.items() for key, pages in sorted(values.items()))
    with (output / "vma_vectors_l2.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_id", *[f"v{i:04d}" for i in range(2048)]])
        writer.writerow([sample["sample_id"], *vector])
    fields = ("sample_id", "trial_id", "session_id", "label_id", "operation_label")
    for filename in ("labels.csv", "dataset_manifest.csv"):
        row = {field: sample[field] for field in fields}
        if filename == "dataset_manifest.csv":
            row.update({"feature_schema_version": sample["feature_schema"], "source": sample["source"],
                        "is_mock": str(bool(sample["is_mock"])).lower(), "quality_status": "VALID",
                        "status": "success", "collection_stage": "smoke",
                        "capture_profile": json.dumps({**sample["capture_profile"],
                            "page_size_bytes": sample["page_size_bytes"], "app_version": sample["app_version"]}, sort_keys=True)})
        with (output / filename).open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
    save_json(output / "provenance.json", {key: sample[key] for key in (
        "sample_id", "session_id", "trial_id", "app_id", "app_version", "feature_schema",
        "capture_profile", "action_plan_sha256", "collector_sha256", "source", "is_mock")})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-plan", help="validate external plan; no processes or devices accessed")
    check.add_argument("--plan", type=Path, required=True)
    capture = commands.add_parser("capture", help="capture one smoke sample on Linux; executes supplied argv")
    for flag in ("plan", "collector", "output"):
        capture.add_argument("--" + flag, type=Path, required=True)
    capture.add_argument("--pid", type=int, action="append", required=True)
    for flag in ("session-id", "trial-id", "app-version", "device-id"):
        capture.add_argument("--" + flag, required=True)
    replay = commands.add_parser("replay", help="export an existing complete Linux sample without /proc access")
    replay.add_argument("--sample", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    predict = commands.add_parser("predict", help="call the existing SHA-checked Native centroid CLI")
    for flag in ("native", "models", "vectors", "output"):
        predict.add_argument("--" + flag, type=Path, required=True)
    predict.add_argument("--sample", type=Path, help="optional single capture provenance for an operation event")
    predict.add_argument("--event-output", type=Path, help="new JSON recognition-event file, used with --sample")
    args = parser.parse_args(argv)
    if args.command == "check-plan":
        plan = validate_plan(json.loads(args.plan.read_text(encoding="utf-8")))
        print(json.dumps({"status": "plan_valid", "operation_id": plan["operation_id"], "device_validated": False}))
    elif args.command == "capture":
        plan = validate_plan(json.loads(args.plan.read_text(encoding="utf-8")))
        backend = LinuxProcfs(args.pid, args.collector)
        sample = capture_sample(plan=plan, backend=backend, output=args.output, session_id=args.session_id,
                                trial_id=args.trial_id, app_version=args.app_version, device_id=args.device_id)
        print(json.dumps({"status": "success", "sample_id": sample["sample_id"], "collection_stage": "smoke"}))
    elif args.command == "replay":
        export_sample(json.loads(args.sample.read_text(encoding="utf-8")), args.output)
        print("exported one complete smoke sample; this is not a full 18-operation trial")
    else:
        if bool(args.sample) != bool(args.event_output):
            raise ValueError("--sample and --event-output must be provided together")
        if args.event_output and args.event_output.exists():
            raise FileExistsError("event output must be a new file")
        subprocess.run([str(args.native.resolve()), "--models", str(args.models.resolve()), "--app", "wps",
                        "--schema", "wps.vma.feature.v1", "--vectors", str(args.vectors.resolve()),
                        "--output", str(args.output.resolve())], check=True)
        if args.sample:
            with args.output.open(encoding="utf-8", newline="") as handle:
                predictions = list(csv.DictReader(handle))
            if len(predictions) != 1:
                raise ValueError("single-sample event export needs exactly one prediction")
            event = event_from_prediction(json.loads(args.sample.read_text(encoding="utf-8")), predictions[0])
            args.event_output.parent.mkdir(parents=True, exist_ok=True)
            with args.event_output.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CaptureError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"linux WPS: {error}")
