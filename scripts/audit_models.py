"""Audit the frozen model files without reading or writing any dataset."""

import argparse
import csv
import hashlib
import json
from pathlib import Path


EXPECTED = {
    "bilibili": ("cross_app.vma.feature.v2", 6,
                  "0c0b171bca7b7e1d861e15da3b4845ebf66cbbe899d7cd6e02fab809e0068337",
                  "51f93ac0895c3e8900f600ce01d1aa3c27394d7836708c04790c1b03674d8f80"),
    "huawei_browser": ("cross_app.vma.feature.v2", 5,
                       "0836b119d00326c4a8e419050d5306cbf33c4abc0f8a5bb1fd70c9444dc28c08",
                       "718b6b7d3bd37d8cbd41ba753aa1a7b100c932c17419848aaf29a3fe48a3d878"),
    "wps": ("wps.vma.feature.v1", 18,
            "2ca6bcce2856798c0998c28414053d9338e28a61923c50184c3f228a7cd42c7f",
            "dbd1277f21252f2827ec965a2b195365b872b0f1eae4c7b2eb709d7aaba233ea"),
}


def audit(root: Path) -> dict:
    results = {}
    for app, (schema, count, model_sha, centers_sha) in EXPECTED.items():
        directory = root / app
        model_file = directory / "model.json"
        centers_file = directory / "centers.csv"
        actual_model_sha = hashlib.sha256(model_file.read_bytes()).hexdigest()
        actual_centers_sha = hashlib.sha256(centers_file.read_bytes()).hexdigest()
        if (actual_model_sha, actual_centers_sha) != (model_sha, centers_sha):
            raise ValueError(f"{app}: model SHA-256 differs from verified export")
        model = json.loads(model_file.read_text(encoding="utf-8"))
        if (model["app_id"] != app or model["feature_schema"] != schema
                or model["vector_dim"] != 2048 or model["center_count"] != count
                or model["method"] != "class_centroid_cosine"
                or model["center_csv"] != "centers.csv"):
            raise ValueError(f"{app}: model metadata mismatch")
        with centers_file.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != ["center_id", "operation_label"] + [f"v{i:04d}" for i in range(2048)]:
                raise ValueError(f"{app}: center vector columns mismatch")
            rows = list(reader)
        if len(rows) != count or set(model["center_to_operation"]) != {str(i) for i in range(count)}:
            raise ValueError(f"{app}: center count or mapping mismatch")
        for i, row in enumerate(rows):
            if row["center_id"] != str(i) or row["operation_label"] != model["center_to_operation"][str(i)]:
                raise ValueError(f"{app}: center-to-label mapping mismatch")
        results[app] = {"schema": schema, "dimension": 2048, "centers": count,
                        "model_sha256": actual_model_sha, "centers_sha256": actual_centers_sha}
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=Path(__file__).resolve().parents[1] / "models")
    args = parser.parse_args()
    print(json.dumps(audit(args.models), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
