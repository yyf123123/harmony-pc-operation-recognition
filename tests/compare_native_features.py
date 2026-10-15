"""Compare C++ feature math with Python on externally supplied real windows.

The input database, snapshots and WPS sequence stay outside Git. Temporary
semantic maps and generated vectors are removed when the check finishes.
"""

import argparse
import csv
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from collector.snapshot import aggregate_snapshot_features
from features.build import snapshot
from features.vma_feature import build_vma_feature
from features.wps import build_vector, l2_normalize


def map_csv(path: Path, values: dict) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("namespace", "feature_key", "pages"))
        for (namespace, key), pages in sorted(values.items()):
            writer.writerow((namespace, key, repr(float(pages))))


def native_vector(native: Path, schema: str, sample_id: str, arguments: list[str], temp: Path) -> list[float]:
    output = temp / "native_vector.csv"
    subprocess.run([str(native), "--schema", schema, "--sample-id", sample_id,
                    *arguments, "--output", str(output)], check=True, capture_output=True, text=True)
    with output.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    return [float(row[f"v{i:04d}"]) for i in range(2048)]


def resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_file() else root / "data" / "raw" / "smaps" / path.name


def cross_app(args, temp: Path) -> tuple[list[float], list[float], list[float], str]:
    with args.dataset.joinpath("dataset_manifest.csv").open(encoding="utf-8-sig", newline="") as handle:
        sample = next(csv.DictReader(handle))
    sid = sample["trial_id"]
    db = sqlite3.connect(f"file:{args.database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT phase,raw_path FROM vma_snapshot WHERE trial_id=?", (sid,)).fetchall()
        feature_path = db.execute("SELECT derived_path FROM vma_feature WHERE trial_id=? AND feature_schema_version=?",
                                  (sid, "cross_app.vma.feature.v2")).fetchone()[0]
    finally:
        db.close()
    windows = {phase: snapshot(resolve(args.workspace, path)) for phase, path in rows}
    baseline_phases = sorted(phase for phase in windows if phase.startswith("BASELINE"))
    action_phase = next(phase for phase in windows if phase.startswith("ACTION"))
    post_phase = sorted(phase for phase in windows if phase.startswith("POST"))[0]
    baselines = [windows[phase] for phase in baseline_phases]
    action, post = windows[action_phase], windows[post_phase]
    native_args = []
    for i, item in enumerate(baselines):
        path = temp / f"baseline_{i}.csv"
        map_csv(path, aggregate_snapshot_features(item))
        native_args += ["--baseline", str(path)]
    for name, item in (("action", action), ("post", post)):
        path = temp / f"{name}.csv"
        map_csv(path, aggregate_snapshot_features(item))
        native_args += [f"--{name}", str(path)]
    derived_path = Path(feature_path)
    if not derived_path.is_file():
        derived_path = args.workspace / "data" / "derived" / "vma_feature" / derived_path.name
    existing = json.loads(derived_path.read_text(encoding="utf-8"))
    python = build_vma_feature(sample_id=sid, app_id=existing["app_id"],
                               operation_id=existing["operation_id"],
                               baselines=baselines, action=action, post_action=post,
                               schema_version="cross_app.vma.feature.v2")
    native = native_vector(args.native, "cross_app.vma.feature.v2", sid, native_args, temp)
    return native, list(python.l2_vector), list(existing["l2_vector"]), existing["schema_version"]


def wps(args, temp: Path) -> tuple[list[float], list[float], list[float], str]:
    with args.sequence.open(encoding="utf-8") as handle:
        sample = json.loads(handle.readline())
    aggregated = sample["sequence"]["aggregated_features"]
    features = {(row["feature_namespace"], row["feature_key"]): float(row["aggregated_excess_pages"])
                for row in aggregated if row["feature_namespace"] in ("FILE", "ANON")}
    path = temp / "aggregated.csv"
    map_csv(path, features)
    file_map = {key: pages for (namespace, key), pages in features.items() if namespace == "FILE" and pages > 0}
    anon_map = {key: pages for (namespace, key), pages in features.items() if namespace == "ANON" and pages > 0}
    python = l2_normalize(build_vector(file_map, anon_map))
    native = native_vector(args.native, "wps.vma.feature.v1", str(sample["sample_id"]),
                           ["--input", str(path)], temp)
    stored = python
    if args.vectors:
        with args.vectors.open(encoding="utf-8-sig", newline="") as handle:
            row = next((row for row in csv.DictReader(handle)
                        if row["sample_id"] == str(sample["sample_id"])), None)
        if row is None:
            raise ValueError("WPS sample is absent from existing vectors")
        stored = [float(row[f"v{i:04d}"]) for i in range(2048)]
    return native, python, stored, "wps.vma.feature.v1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    sub = parser.add_subparsers(dest="kind", required=True)
    wps_parser = sub.add_parser("wps")
    wps_parser.add_argument("--sequence", type=Path, required=True)
    wps_parser.add_argument("--vectors", type=Path, help="Optional existing exported WPS vectors")
    cross_parser = sub.add_parser("cross")
    cross_parser.add_argument("--dataset", type=Path, required=True)
    cross_parser.add_argument("--database", type=Path, required=True)
    cross_parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="harmony-native-features-") as directory:
        native, python, stored, schema = (wps(args, Path(directory)) if args.kind == "wps"
                                           else cross_app(args, Path(directory)))
    differences = [abs(left - right) for left, right in zip(native, python)]
    stored_differences = [abs(left - right) for left, right in zip(native, stored)]
    print(json.dumps({"schema": schema, "dimensions": len(differences),
                      "max_abs_feature_difference": max(differences),
                      "max_abs_stored_difference": max(stored_differences),
                      "nonzero_python": sum(value != 0 for value in python)}, indent=2))
    if len(differences) != 2048 or len(stored_differences) != 2048 or max(differences) > 1e-9 or max(stored_differences) > 1e-9:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
