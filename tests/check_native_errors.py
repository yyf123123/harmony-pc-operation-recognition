"""Exercise native inference rejection paths without any device or dataset."""

import argparse
import csv
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(native: Path, models: Path, schema: str, vector: list[str], directory: Path) -> bool:
    source = directory / "input.csv"
    output = directory / "output.csv"
    if output.exists():
        output.unlink()
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_id"] + [f"v{i:04d}" for i in range(len(vector))])
        writer.writerow(["contract-check", *vector])
    result = subprocess.run([str(native), "--models", str(models), "--app", "bilibili",
                             "--schema", schema, "--vectors", str(source),
                             "--output", str(output)], capture_output=True, text=True)
    return result.returncode != 0 and not output.exists()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    args = parser.parse_args()
    native, models = args.native.resolve(), args.models.resolve()
    with tempfile.TemporaryDirectory(prefix="harmony-native-errors-") as path:
        temp = Path(path)
        unit = ["1"] + ["0"] * 2047
        checks = {
            "signed_v3_schema": run(native, models, "cross_app.vma.feature.v3_signed", unit, temp),
            "wrong_dimension": run(native, models, "cross_app.vma.feature.v2", unit[:-1], temp),
            "zero_vector": run(native, models, "cross_app.vma.feature.v2", ["0"] * 2048, temp),
            "nonfinite_vector": run(native, models, "cross_app.vma.feature.v2", ["nan"] + unit[1:], temp),
        }
        altered = temp / "altered_models" / "bilibili"
        altered.mkdir(parents=True)
        shutil.copyfile(models / "bilibili" / "centers.csv", altered / "centers.csv")
        center_path = altered / "centers.csv"
        with center_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        rows[1][1] = "wrong_label"
        with center_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        checks["wrong_center_mapping"] = run(native, altered.parent,
                                              "cross_app.vma.feature.v2", unit, temp)
    print(checks)
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
