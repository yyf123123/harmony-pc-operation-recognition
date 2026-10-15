"""Compare the portable C++ executable with the existing Python prediction formula.

Reads external real vectors only; temporary selected rows and predictions are
created outside the repository and removed automatically.
"""

import argparse
import csv
import json
import subprocess
import tempfile
from pathlib import Path

from classification.predict import load_model, predict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--app", choices=("bilibili", "huawei_browser", "wps"), required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--vectors", type=Path, required=True)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--tolerance", type=float, default=1e-9)
    args = parser.parse_args()
    if args.count < 1 or args.tolerance <= 0:
        parser.error("count and tolerance must be positive")
    model, centers = load_model(args.models / args.app / "model.json", args.schema)
    with args.vectors.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["sample_id"] + [f"v{i:04d}" for i in range(model["vector_dim"])]:
            raise ValueError("vector header does not match the model dimension")
        total = sum(1 for _ in reader)
    if total == 0:
        raise ValueError("no samples")
    positions = {round(i * (total - 1) / max(1, args.count - 1)) for i in range(args.count)}
    with args.vectors.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        selected = [row for index, row in enumerate(reader) if index in positions]
    values = [[float(row[f"v{i:04d}"]) for i in range(model["vector_dim"])] for row in selected]
    python_predictions = predict(values, model, centers)
    with tempfile.TemporaryDirectory(prefix="harmony-native-compare-") as temp:
        input_file = Path(temp) / "vectors.csv"
        output_file = Path(temp) / "predictions.csv"
        with input_file.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sample_id"] + [f"v{i:04d}" for i in range(model["vector_dim"])])
            writer.writeheader()
            writer.writerows(selected)
        subprocess.run([str(args.native), "--models", str(args.models), "--app", args.app,
                        "--schema", args.schema, "--vectors", str(input_file),
                        "--output", str(output_file)], check=True, capture_output=True, text=True)
        with output_file.open(encoding="utf-8", newline="") as handle:
            native_predictions = list(csv.DictReader(handle))
    if len(native_predictions) != len(python_predictions):
        raise ValueError("prediction count mismatch")
    differences = []
    label_mismatches = 0
    for row, (label, score), native in zip(selected, python_predictions, native_predictions):
        if row["sample_id"] != native["sample_id"] or label != native["operation_label"]:
            label_mismatches += 1
        differences.append(abs(score - float(native["cosine_similarity"])))
    result = {"app": args.app, "schema": args.schema, "samples": len(differences),
              "label_mismatches": label_mismatches,
              "max_abs_score_difference": max(differences),
              "mean_abs_score_difference": sum(differences) / len(differences),
              "tolerance": args.tolerance}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if label_mismatches or max(differences) > args.tolerance:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
