"""Read-only intake of a completed batch; frozen Native predictions stay separate from truth."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path

from apps.wps.adapter import WpsAdapter
from collector.linux.batch import atomic_json, digest, make_schedule, verify_receipt
from collector.linux.history import event_from_prediction


def _one_csv(path, expected_header=None):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        if expected_header is not None and reader.fieldnames != expected_header:
            raise ValueError("invalid vector schema")
    if len(rows) != 1 or None in rows[0] or any(v is None for v in rows[0].values()):
        raise ValueError("one complete CSV row is required")
    return rows[0]


def export_batch(batch: Path, output: Path, *, allow_fixture=False):
    root = batch.resolve(strict=True)
    binding = json.loads((root / "binding.json").read_text())
    config = json.loads((root / "config.json").read_text())
    schedule = make_schedule(config)
    index = json.loads((root / "accepted.json").read_text())
    report = json.loads((root / "report.json").read_text())
    fixture = binding.get("fixture_mode")
    if type(fixture) is not bool or (fixture and not allow_fixture):
        raise ValueError("synthetic fixtures cannot be exported as real data")
    if (binding["config_sha256"] != digest(config) or report["status"] != ("fixture_passed" if fixture else "passed")
            or not report.get("preflight_passed") or index.get("is_mock") is not fixture):
        raise ValueError("batch is incomplete or its configuration differs")
    receipts = index["units"]
    if [r["unit_id"] for r in receipts] != [u["unit_id"] for u in schedule]:
        raise ValueError("accepted units differ from complete frozen schedule")
    journal = (root/"journal.jsonl").read_bytes()
    if not journal.endswith(b"\n"):
        raise ValueError("truncated journal")
    committed = {}
    for line in journal.splitlines():
        record = json.loads(line)
        if record["event"] == "passed":
            if record["unit_id"] in committed:
                raise ValueError("duplicate committed unit")
            committed[record["unit_id"]] = record
    if len(committed) != len(schedule) or any(committed.get(r["unit_id"]) != r for r in receipts):
        raise ValueError("accepted index differs from committed journal")
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError("export destination must be new")
    catalog = {op.operation_id: op for op in WpsAdapter().catalog.operations}
    label_ids = {operation_id: n for n, operation_id in enumerate(catalog)}
    labels = {str(op.action_spec["legacy_label"]) for op in catalog.values()}
    groups = defaultdict(list)
    sequences = []
    ids, families = set(), {}
    vector_header = ["sample_id"] + [f"v{i:04d}" for i in range(2048)]
    # Fully verify inputs before creating a successful-looking output directory.
    for unit, receipt in zip(schedule, receipts):
        if receipt.get("stage") != unit["stage"]:
            raise ValueError("receipt stage differs from schedule")
        samples = verify_receipt(root, receipt, fixture)
        if [s["operation_id"] for s in samples] != unit["operations"]:
            raise ValueError("sample truth differs from planned operation")
        if unit["stage"] != "formal":
            continue
        if families.setdefault(unit["family_id"], unit["split"]) != unit["split"]:
            raise ValueError("document family crosses dataset splits")
        ordered, events = [], []
        last_end, instance = -1, None
        for sequence_index, (name, sample) in enumerate(zip(receipt["samples"], samples)):
            sample_id = sample["sample_id"]
            if sample_id in ids:
                raise ValueError("duplicate sample ID")
            ids.add(sample_id)
            operation = catalog[sample["operation_id"]]
            if (sample["operation_label"] != operation.action_spec["legacy_label"] or sample["label_id"] != label_ids[sample["operation_id"]]
                    or sample["capture_context"]["sequence_index"] != sequence_index):
                raise ValueError("sample label or sequence identity differs from frozen catalog")
            directory = (root / name).parent.parent
            names = [directory / "export/native_vectors.csv", directory / "prediction.csv", directory / "recognition.json"]
            if any(str(p.relative_to(root)) not in receipt["files"] for p in names):
                raise ValueError("unbound Native output")
            vector_row = _one_csv(names[0], vector_header)
            vector = [float(vector_row[f"v{i:04d}"]) for i in range(2048)]
            norm = math.sqrt(sum(v*v for v in vector))
            if vector_row["sample_id"] != sample_id or not all(math.isfinite(v) for v in vector) or abs(norm-1) > 1e-9:
                raise ValueError("invalid Native normalized vector")
            prediction = _one_csv(names[1])
            if (prediction.get("sample_id") != sample_id or prediction.get("operation_label") not in labels
                    or not math.isfinite(float(prediction["cosine_similarity"]))):
                raise ValueError("invalid frozen Native prediction")
            event = json.loads(names[2].read_text())
            if event != event_from_prediction(sample, prediction):
                raise ValueError("recognition event was not derived from Native prediction")
            end = event["end_monotonic_ns"]
            if type(end) is not int or end <= last_end:
                raise ValueError("unordered recognition timestamps")
            last_end = end
            if instance is not None and instance != event["app_instance_id"]:
                raise ValueError("application instance changed in accepted session")
            instance = event["app_instance_id"]
            row = {"sample_id": sample_id, "trial_id": sample["trial_id"], "session_id": sample["session_id"],
                   "label_id": sample["label_id"], "operation_label": sample["operation_label"],
                   "feature_schema_version": sample["feature_schema"], "source": sample["source"],
                   "is_mock": str(fixture).lower(), "quality_status": "VALID", "status": "success", "collection_stage": "formal",
                   "capture_profile": json.dumps({**sample["capture_profile"], "page_size_bytes": sample["page_size_bytes"],
                                                  "app_version": sample["app_version"]}, sort_keys=True),
                   "document_id": unit["document_id"], "family_id": unit["family_id"],
                   "split": unit["split"], "subset": unit["subset"], "sequence_index": sequence_index,
                   "group_id": unit["family_id"], "evidence_receipt": str(Path("attempts") / unit["unit_id"])}
            groups[(unit["subset"], unit["split"])].append((row, vector_row, prediction))
            ordered.append({"sample_id": sample_id, "operation_id": sample["operation_id"], "end_monotonic_ns": end})
            events.append(event)
        if unit["subset"] == "continuous":
            sequences.append({"unit_id": unit["unit_id"], "session_id": samples[0]["session_id"],
                              "split": unit["split"], "family_id": unit["family_id"],
                              "sequence_kind": "scripted_controlled_sequence", "truth": ordered, "recognition": events})
    expected = {("independent", "train"): 540, ("independent", "validation"): 180, ("independent", "test"): 180,
                ("continuous", "train"): 360, ("continuous", "validation"): 120, ("continuous", "test"): 120}
    if {key: len(rows) for key, rows in groups.items()} != expected or len(sequences) != 30:
        raise ValueError("formal quota or continuous-session coverage is incomplete")
    counts = Counter(row["operation_label"] for key, rows in groups.items() if key[0] == "independent" for row, _, _ in rows)
    if counts != dict.fromkeys(labels, 50):
        raise ValueError("each independent operation must have fifty valid windows")
    if len({row["capture_profile"] for rows in groups.values() for row, _, _ in rows}) != 1:
        raise ValueError("mixed capture profiles or application versions")
    output.mkdir(parents=True)
    result = {"schema_version": "linux.wps.batch-export.v1", "status": "fixture_exported" if fixture else "exported",
              "is_mock": fixture, "formal_windows": len(ids), "continuous_sessions": len(sequences), "groups": {},
              "metrics_scope": "not_scored_synthetic" if fixture else "offline_frozen_model_controlled_windows_only",
              "macro_f1_labels": "frozen_catalog_18_including_unobserved_as_zero",
              "source_config_sha256": binding["config_sha256"]}
    for (subset, split), rows in groups.items():
        directory = output / subset / split
        directory.mkdir(parents=True)
        for filename, fields, values in (
            ("dataset_manifest.csv", list(rows[0][0]), [row for row, _, _ in rows]),
            ("labels.csv", ["sample_id", "trial_id", "session_id", "label_id", "operation_label"],
             [{key: row[key] for key in ("sample_id", "trial_id", "session_id", "label_id", "operation_label")} for row, _, _ in rows]),
            ("vma_vectors_l2.csv", vector_header, [vector for _, vector, _ in rows]),
            ("predictions.csv", list(rows[0][2]), [prediction for _, _, prediction in rows])):
            with (directory / filename).open("x", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(values)
        entry = {"windows": len(rows), "class_counts": dict(Counter(row["operation_label"] for row, _, _ in rows))}
        if not fixture:
            confusion = Counter((row["operation_label"], pred["operation_label"]) for row, _, pred in rows)
            f1s = []
            for label in sorted(labels):
                true_positive = confusion[(label, label)]
                denominator = sum(n for (truth, predicted), n in confusion.items() if truth == label) + sum(n for (truth, predicted), n in confusion.items() if predicted == label)
                f1s.append(2*true_positive/denominator if denominator else 0)
            entry.update(accuracy=sum(n for (truth, predicted), n in confusion.items() if truth == predicted)/len(rows),
                         macro_f1=sum(f1s)/len(f1s), confusion=[{"truth": a, "predicted": b, "count": n} for (a,b),n in sorted(confusion.items())])
        result["groups"][f"{subset}/{split}"] = entry
    for sequence in sequences:
        directory = output / "sequences" / sequence["unit_id"]
        directory.mkdir(parents=True)
        atomic_json(directory / "truth.json", {key: value for key, value in sequence.items() if key != "recognition"})
        with (directory / "recognition.jsonl").open("x", encoding="utf-8") as handle:
            for event in sequence["recognition"]:
                handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    atomic_json(output / "quality.json", result)
    return result
