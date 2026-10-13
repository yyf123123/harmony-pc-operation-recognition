#!/usr/bin/env python3
"""Build fixed-dimension, labelled WPS VMA vectors from collected windows.

The collector keeps raw Markdown reports.  This module only converts the
stable semantic FILE/ANON keys into a deterministic 2048-dimensional vector;
PID, addresses, timestamps and experiment identifiers stay in metadata.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


VECTOR_DIM = 2048
FILE_SLOTS = 1024
ANON_SLOTS = 1024
PAGE_SIZE_KB = 4
SCHEMA_VERSION = "wps.vma-dataset.v1"


def stable_hash_index(namespace: str, feature_key: str) -> int:
    """Return a process-independent slot in the FILE or ANON namespace."""
    namespace = namespace.upper()
    if namespace not in {"FILE", "ANON"}:
        raise ValueError(f"unsupported feature namespace: {namespace}")
    digest = hashlib.sha256(f"{namespace}:{feature_key}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    if namespace == "FILE":
        return value % FILE_SLOTS
    return FILE_SLOTS + value % ANON_SLOTS


def build_vector(file_features: dict[str, float], anon_features: dict[str, float]) -> list[float]:
    """Build the raw log1p feature-hashing vector."""
    vector = [0.0] * VECTOR_DIM
    for key, pages in file_features.items():
        vector[stable_hash_index("FILE", key)] += math.log1p(max(float(pages), 0.0))
    for key, pages in anon_features.items():
        vector[stable_hash_index("ANON", key)] += math.log1p(max(float(pages), 0.0))
    return vector


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    denominator = math.sqrt(sum(value * value for value in left_values)) * math.sqrt(
        sum(value * value for value in right_values)
    )
    if denominator == 0.0:
        return 1.0 if left_values == right_values else 0.0
    return sum(a * b for a, b in zip(left_values, right_values)) / denominator


def weighted_jaccard(left: dict[str, float], right: dict[str, float]) -> float:
    keys = set(left) | set(right)
    denominator = sum(max(float(left.get(key, 0.0)), float(right.get(key, 0.0))) for key in keys)
    if denominator == 0.0:
        return 1.0
    numerator = sum(min(float(left.get(key, 0.0)), float(right.get(key, 0.0))) for key in keys)
    return numerator / denominator


def top_k_overlap(left: dict[str, float], right: dict[str, float], k: int = 10) -> float:
    left_top = {key for key, _ in sorted(left.items(), key=lambda item: (-item[1], item[0]))[:k]}
    right_top = {key for key, _ in sorted(right.items(), key=lambda item: (-item[1], item[0]))[:k]}
    union = left_top | right_top
    return len(left_top & right_top) / len(union) if union else 1.0


def _clean_cell(value: str) -> str:
    return value.strip().strip("`").strip()


def _number(value: str, default: float = 0.0) -> float:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group(0)) if match else default


def _process_metadata(text: str) -> tuple[str, str]:
    pid_match = re.search(r"\|\s*PID\s*\|\s*`?(\d+)", text)
    name_match = re.search(r"\|\s*进程名\s*\|\s*`?([^|`\n]+)", text)
    return (pid_match.group(1) if pid_match else "", name_match.group(1).strip() if name_match else "")


def _normalise_path(pathname: str) -> str:
    pathname = pathname.replace("\\", "/").strip()
    pathname = re.sub(r"/{2,}", "/", pathname)
    return pathname or "(anonymous)"


def _feature_namespace(segment: str, pathname: str) -> str:
    if segment.lower().startswith("filepage") or pathname.startswith("/"):
        return "FILE"
    return "ANON"


def _semantic_key(namespace: str, segment: str, perms: str, pathname: str) -> str:
    pathname = _normalise_path(pathname)
    if namespace == "FILE":
        return f"{pathname}|segment={segment}|perms={perms}"
    if pathname in {"", "(anonymous)"}:
        pathname = "(anonymous)"
    return f"{pathname}|segment={segment}|perms={perms}"


def parse_vma_report(path: Path) -> list[dict[str, Any]]:
    """Parse the VMA table emitted by ``mem_analyze-v6 --with-vma``."""
    text = path.read_text(encoding="utf-8", errors="replace")
    pid, process_name = _process_metadata(text)
    rows: list[dict[str, Any]] = []
    in_vma_table = False
    for line in text.splitlines():
        if line.startswith("## Referenced VMA 定位"):
            in_vma_table = True
            continue
        if not in_vma_table or not line.startswith("|") or "|---" in line:
            continue
        cells = [_clean_cell(cell) for cell in line.split("|")[1:-1]]
        if len(cells) < 10 or not re.fullmatch(r"[0-9a-fA-F]+-[0-9a-fA-F]+", cells[0]):
            continue
        segment = cells[1]
        perms = cells[2]
        pathname = cells[-1]
        referenced_kb = max(_number(cells[6]), 0.0)
        referenced_pages = max(_number(cells[7]), 0.0)
        if referenced_pages == 0.0 and referenced_kb > 0.0:
            referenced_pages = referenced_kb / PAGE_SIZE_KB
        namespace = _feature_namespace(segment, pathname)
        key = _semantic_key(namespace, segment, perms, pathname)
        rows.append(
            {
                "report_path": str(path),
                "pid": pid,
                "process_role": process_name or "unknown",
                "vma": cells[0],
                "segment": segment,
                "perms": perms,
                "size_kb": _number(cells[3]),
                "rss_kb": _number(cells[4]),
                "pss_kb": _number(cells[5]),
                "referenced_kb": referenced_kb,
                "referenced_pages": referenced_pages,
                "pathname": pathname or "(anonymous)",
                "feature_namespace": namespace,
                "feature_key": key,
            }
        )
    return rows


def aggregate_report_features(report_paths: Iterable[Path]) -> dict[str, Any]:
    """Aggregate VMA referenced pages across all WPS process reports."""
    feature_pages: dict[str, float] = defaultdict(float)
    feature_meta: dict[str, dict[str, Any]] = {}
    raw_rows: list[dict[str, Any]] = []
    for path in report_paths:
        rows = parse_vma_report(path)
        raw_rows.extend(rows)
        for row in rows:
            feature_id = f"{row['feature_namespace']}\t{row['feature_key']}"
            feature_pages[feature_id] += float(row["referenced_pages"])
            metadata = feature_meta.setdefault(
                feature_id,
                {
                    "feature_namespace": row["feature_namespace"],
                    "feature_key": row["feature_key"],
                    "process_roles": set(),
                    "pathnames": set(),
                },
            )
            metadata["process_roles"].add(row["process_role"])
            metadata["pathnames"].add(row["pathname"])
    for metadata in feature_meta.values():
        metadata["process_role"] = ";".join(sorted(metadata.pop("process_roles")))
        metadata["pathname"] = ";".join(sorted(metadata.pop("pathnames")))
    return {
        "feature_pages": dict(feature_pages),
        "feature_meta": feature_meta,
        "raw_rows": raw_rows,
    }


def _window_features(window: dict[str, Any]) -> dict[str, Any]:
    if "feature_pages" in window:
        return {
            "feature_pages": {str(key): float(value) for key, value in window.get("feature_pages", {}).items()},
            "feature_meta": window.get("feature_meta", {}),
            "raw_rows": window.get("raw_rows", []),
        }
    paths = [Path(path) for path in window.get("report_paths", [])]
    return aggregate_report_features(paths)


def _activity_level(pages: float) -> str:
    if pages <= 1.0:
        return "low"
    if pages <= 32.0:
        return "medium"
    return "high"


def map_fixed_window_sequence(
    *,
    operation_execution_id: str,
    operation_id: str,
    baseline_group_id: str,
    baseline_windows: list[dict[str, Any]],
    operation_windows: list[dict[str, Any]],
    include_raw_rows: bool = False,
) -> dict[str, Any]:
    """Map ACTION/POST_ACTION pages to baseline-relative feature records."""
    baseline_maps = [_window_features(window) for window in baseline_windows]
    baseline_keys = set().union(*(item["feature_pages"] for item in baseline_maps)) if baseline_maps else set()
    baseline_pages: dict[str, float] = {}
    for feature_id in baseline_keys:
        values = [item["feature_pages"].get(feature_id, 0.0) for item in baseline_maps]
        baseline_pages[feature_id] = float(statistics.median(values))

    mapped_windows: list[dict[str, Any]] = []
    all_excess: dict[str, dict[str, Any]] = {}
    for window in operation_windows:
        window_map = _window_features(window)
        excess_rows: list[dict[str, Any]] = []
        for feature_id, pages in window_map["feature_pages"].items():
            baseline = baseline_pages.get(feature_id, 0.0)
            excess = max(float(pages) - baseline, 0.0)
            namespace, feature_key = feature_id.split("\t", 1)
            metadata = window_map["feature_meta"].get(feature_id, {})
            if excess > 0.0:
                row = {
                    "feature_namespace": namespace,
                    "feature_key": feature_key,
                    "raw_pages": float(pages),
                    "baseline_pages": float(baseline),
                    "estimated_excess_pages": excess,
                    "process_role": metadata.get("process_role", "unknown"),
                    "pathname": metadata.get("pathname", ""),
                    "activity_level": _activity_level(excess),
                    "operation_path": window.get("segment_label", "ACTION"),
                    "operation_mapping_type": "baseline_excess",
                }
                excess_rows.append(row)
                aggregate = all_excess.setdefault(
                    feature_id,
                    {
                        "feature_namespace": namespace,
                        "feature_key": feature_key,
                        "action_excess_pages": 0.0,
                        "post_action_excess_pages": 0.0,
                        "process_roles": set(),
                        "pathnames": set(),
                    },
                )
                phase_key = "action_excess_pages" if window.get("segment_label") == "ACTION" else "post_action_excess_pages"
                aggregate[phase_key] = max(float(aggregate[phase_key]), excess)
                if metadata.get("process_role"):
                    aggregate["process_roles"].add(metadata["process_role"])
                if metadata.get("pathname"):
                    aggregate["pathnames"].add(metadata["pathname"])
        mapped_windows.append(
            {
                "window_id": window.get("window_id", ""),
                "segment_label": window.get("segment_label", ""),
                "window_kind": window.get("window_kind", "OPERATION"),
                "status": window.get("status", "success"),
                "report_paths": list(window.get("report_paths", [])),
                "feature_pages": window_map["feature_pages"],
                "features": excess_rows,
                "raw_rows": window_map["raw_rows"] if include_raw_rows else [],
                "action_started_at": window.get("action_started_at", ""),
                "action_ended_at": window.get("action_ended_at", ""),
                "window_started_at": window.get("window_started_at", ""),
                "window_ended_at": window.get("window_ended_at", ""),
                "report_count": window.get("report_count", 0),
                "hash_mismatch_count": window.get("hash_mismatch_count", 0),
                "collection_quality": window.get("collection_quality", ""),
            }
        )

    aggregated_features: list[dict[str, Any]] = []
    for aggregate in all_excess.values():
        aggregate["process_role"] = ";".join(sorted(aggregate.pop("process_roles")))
        aggregate["pathname"] = ";".join(sorted(aggregate.pop("pathnames")))
        aggregate["aggregated_excess_pages"] = max(
            aggregate["action_excess_pages"], aggregate["post_action_excess_pages"]
        )
        aggregate["estimated_excess_pages"] = aggregate["aggregated_excess_pages"]
        aggregate["activity_level"] = _activity_level(aggregate["aggregated_excess_pages"])
        aggregate["operation_path"] = "ACTION+POST_ACTION"
        aggregate["operation_mapping_type"] = "max_action_post_action"
        aggregated_features.append(aggregate)
    aggregated_features.sort(key=lambda row: (row["feature_namespace"], row["feature_key"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_execution_id": operation_execution_id,
        "operation_id": operation_id,
        "baseline_group_id": baseline_group_id,
        "baseline_window_ids": [window.get("window_id", "") for window in baseline_windows],
        "baseline_pages": baseline_pages,
        "baseline_feature_count": len(baseline_pages),
        "operation_windows": mapped_windows,
        "aggregated_features": aggregated_features,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _read_sequences(root: Path) -> list[dict[str, Any]]:
    path = root / "operation_window_sequences.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"missing collector output: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _relative_or_original(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return path


def _resolve_report(root: Path, trial_dir: Path, path: str) -> Path:
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    candidate = trial_dir / path
    if candidate.is_file():
        return candidate
    candidate = root / path
    if candidate.is_file():
        return candidate
    return Path(path)


def _window_report_paths(root: Path, sequence: dict[str, Any], window: dict[str, Any]) -> list[Path]:
    trial_dir = root / sequence.get("trial_dir", sequence.get("trial_id", ""))
    return [_resolve_report(root, trial_dir, str(path)) for path in window.get("report_paths", [])]


def _median_or_none(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _build_pairwise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairwise: list[dict[str, Any]] = []
    for index, left in enumerate(rows):
        if not left["support_eligible"]:
            continue
        for right in rows[index + 1 :]:
            if not right["support_eligible"]:
                continue
            same_class = left["operation_label"] == right["operation_label"] and left["trial_id"] != right["trial_id"]
            pairwise.append(
                {
                    "sample_id_left": left["sample_id"],
                    "sample_id_right": right["sample_id"],
                    "operation_label_left": left["operation_label"],
                    "operation_label_right": right["operation_label"],
                    "pair_type": "intra_class" if same_class else "inter_class",
                    "cosine_similarity": f"{cosine_similarity(left['vector_l2'], right['vector_l2']):.12f}",
                    "weighted_jaccard": f"{weighted_jaccard(left['feature_pages'], right['feature_pages']):.12f}",
                    "top_k_overlap": f"{top_k_overlap(left['feature_pages'], right['feature_pages']):.12f}",
                }
            )
    return pairwise


def build_dataset(
    root: Path,
    include_raw_reports: bool = True,
    include_pairwise: bool = True,
    output: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    destination = (output or root).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sequences = _read_sequences(root)
    manifest_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    long_rows: list[dict[str, Any]] = []
    file_raw_rows: list[dict[str, Any]] = []
    anon_raw_rows: list[dict[str, Any]] = []

    for sequence in sequences:
        mapping = sequence.get("sequence") or {}
        aggregated = mapping.get("aggregated_features", [])
        file_features = {
            row["feature_key"]: float(row["aggregated_excess_pages"])
            for row in aggregated
            if row.get("feature_namespace") == "FILE" and float(row.get("aggregated_excess_pages", 0.0)) > 0.0
        }
        anon_features = {
            row["feature_key"]: float(row["aggregated_excess_pages"])
            for row in aggregated
            if row.get("feature_namespace") == "ANON" and float(row.get("aggregated_excess_pages", 0.0)) > 0.0
        }
        raw_vector = build_vector(file_features, anon_features)
        vector_l2 = l2_normalize(raw_vector)
        action_window = next((item for item in mapping.get("operation_windows", []) if item.get("segment_label") == "ACTION"), {})
        post_window = next((item for item in mapping.get("operation_windows", []) if item.get("segment_label") == "POST_ACTION"), {})
        all_windows = [*sequence.get("baseline_windows", []), action_window, post_window]
        statuses = [item.get("status", "failed") for item in all_windows if item]
        hash_mismatch_count = sum(int(item.get("hash_mismatch_count", 0) or 0) for item in all_windows if item)
        report_count = sum(len(item.get("report_paths", [])) for item in all_windows if item)
        complete = sequence.get("status") == "success" and statuses and all(status == "success" for status in statuses)
        support_eligible = bool(complete and report_count > 0 and hash_mismatch_count == 0)
        sample_id = str(sequence["sample_id"])
        operation_label = str(sequence["operation_label"])
        label_id = int(sequence["label_id"])
        trial_id = str(sequence["trial_id"])
        file_count = len(file_features)
        anon_count = len(anon_features)
        manifest = {
            "sample_id": sample_id,
            "trial_id": trial_id,
            "session_id": sequence.get("session_id", ""),
            "label_id": label_id,
            "operation_label": operation_label,
            "precondition": sequence.get("precondition", ""),
            "execution_id": sequence.get("execution_id", mapping.get("operation_execution_id", "")),
            "action_window_id": action_window.get("window_id", ""),
            "post_window_id": post_window.get("window_id", ""),
            "action_started_at": action_window.get("action_started_at", ""),
            "action_ended_at": action_window.get("action_ended_at", ""),
            "sample_started_at": sequence.get("sample_started_at", ""),
            "sample_ended_at": sequence.get("sample_ended_at", ""),
            "action_window_s": sequence.get("action_window_s", ""),
            "post_window_s": sequence.get("post_window_s", ""),
            "baseline_window_count": sequence.get("baseline_window_count", ""),
            "support_eligible": str(support_eligible).lower(),
            "window_quality": "complete" if complete else "incomplete",
            "collection_quality": "pass" if support_eligible else ("empty_feature" if complete else "failed"),
            "hash_mismatch_count": hash_mismatch_count,
            "file_feature_count": file_count,
            "anon_feature_count": anon_count,
            "vector_nonzero_count": sum(value != 0.0 for value in raw_vector),
            "document_path": sequence.get("document_path", ""),
            "startup_state": sequence.get("startup_state", ""),
            "device_target": sequence.get("device_target", ""),
            "system_version": sequence.get("system_version", ""),
            "wps_version": sequence.get("wps_version", ""),
            "collector_version": sequence.get("collector_version", "mem_analyze-v6"),
            "operation_path": sequence.get("trial_dir", trial_id),
            "feature_pages": {f"FILE\t{key}": value for key, value in file_features.items()} | {f"ANON\t{key}": value for key, value in anon_features.items()},
            "vector_raw": raw_vector,
            "vector_l2": vector_l2,
        }
        manifest_rows.append(manifest)
        label_rows.append({
            "sample_id": sample_id,
            "label_id": label_id,
            "operation_label": operation_label,
            "trial_id": trial_id,
            "session_id": sequence.get("session_id", ""),
        })
        for row in aggregated:
            if float(row.get("aggregated_excess_pages", 0.0)) <= 0.0:
                continue
            long_rows.append({
                "sample_id": sample_id,
                "trial_id": trial_id,
                "session_id": sequence.get("session_id", ""),
                "label_id": label_id,
                "operation_label": operation_label,
                "execution_id": sequence.get("execution_id", ""),
                "phase": "AGGREGATED",
                "feature_namespace": row.get("feature_namespace", ""),
                "feature_key": row.get("feature_key", ""),
                "estimated_excess_pages": row.get("estimated_excess_pages", 0.0),
                "aggregated_excess_pages": row.get("aggregated_excess_pages", 0.0),
                "action_excess_pages": row.get("action_excess_pages", 0.0),
                "post_action_excess_pages": row.get("post_action_excess_pages", 0.0),
                "process_role": row.get("process_role", ""),
                "activity_level": row.get("activity_level", ""),
                "operation_path": row.get("operation_path", "ACTION+POST_ACTION"),
                "operation_mapping_type": row.get("operation_mapping_type", "max_action_post_action"),
            })
        if include_raw_reports:
            for original_window in sequence.get("baseline_windows", []) + sequence.get("operation_windows", []):
                window = original_window
                phase = window.get("segment_label", window.get("window_kind", ""))
                for report_path in _window_report_paths(root, sequence, window):
                    rows = parse_vma_report(report_path) if report_path.is_file() else []
                    for raw_row in rows:
                        output_row = {
                            "sample_id": sample_id,
                            "trial_id": trial_id,
                            "operation_label": operation_label,
                            "window_id": window.get("window_id", ""),
                            "phase": phase,
                            "report_path": _relative_or_original(str(report_path), root),
                            **raw_row,
                        }
                        if raw_row["feature_namespace"] == "FILE":
                            file_raw_rows.append(output_row)
                        else:
                            anon_raw_rows.append(output_row)

    manifest_fields = [
        "sample_id", "trial_id", "session_id", "label_id", "operation_label", "precondition", "execution_id",
        "action_window_id", "post_window_id", "action_started_at", "action_ended_at", "sample_started_at",
        "sample_ended_at", "action_window_s", "post_window_s", "baseline_window_count", "support_eligible",
        "window_quality", "collection_quality", "hash_mismatch_count", "file_feature_count", "anon_feature_count",
        "vector_nonzero_count", "document_path", "startup_state", "device_target", "system_version", "wps_version",
        "collector_version", "operation_path",
    ]
    _write_csv(destination / "dataset_manifest.csv", manifest_fields, manifest_rows)
    _write_csv(destination / "labels.csv", ["sample_id", "label_id", "operation_label", "trial_id", "session_id"], label_rows)
    long_fields = [
        "sample_id", "trial_id", "session_id", "label_id", "operation_label", "execution_id", "phase",
        "feature_namespace", "feature_key", "estimated_excess_pages", "aggregated_excess_pages",
        "action_excess_pages", "post_action_excess_pages", "process_role", "activity_level", "operation_path",
        "operation_mapping_type",
    ]
    _write_csv(destination / "vma_features_long.csv", long_fields, long_rows)

    vector_fields = ["sample_id", *[f"v{index:04d}" for index in range(VECTOR_DIM)]]
    with (destination / "vma_vectors_raw.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(vector_fields)
        for row in manifest_rows:
            writer.writerow([row["sample_id"], *[f"{value:.12g}" for value in row["vector_raw"]]])
    with (destination / "vma_vectors_l2.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(vector_fields)
        for row in manifest_rows:
            writer.writerow([row["sample_id"], *[f"{value:.12g}" for value in row["vector_l2"]]])

    if include_raw_reports:
        for filename, rows in (
            ("operation_window_file_vma_samples.jsonl", file_raw_rows),
            ("operation_window_anon_vma_samples.jsonl", anon_raw_rows),
        ):
            with (destination / filename).open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(_json_safe(row), ensure_ascii=False, sort_keys=True) + "\n")

    pairwise = _build_pairwise(manifest_rows) if include_pairwise else []
    _write_csv(
        destination / "pairwise_similarity.csv",
        [
            "sample_id_left", "sample_id_right", "operation_label_left", "operation_label_right", "pair_type",
            "cosine_similarity", "weighted_jaccard", "top_k_overlap",
        ],
        pairwise,
    )

    class_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "eligible": 0, "zero_vector": 0})
    for row in manifest_rows:
        counts = class_counts[row["operation_label"]]
        counts["total"] += 1
        counts["eligible"] += int(row["support_eligible"] == "true")
        counts["zero_vector"] += int(row["vector_nonzero_count"] == 0)
    intra_cos = [float(row["cosine_similarity"]) for row in pairwise if row["pair_type"] == "intra_class"]
    inter_cos = [float(row["cosine_similarity"]) for row in pairwise if row["pair_type"] == "inter_class"]
    intra_jaccard = [float(row["weighted_jaccard"]) for row in pairwise if row["pair_type"] == "intra_class"]
    inter_jaccard = [float(row["weighted_jaccard"]) for row in pairwise if row["pair_type"] == "inter_class"]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "sample_count": len(manifest_rows),
        "eligible_sample_count": sum(row["support_eligible"] == "true" for row in manifest_rows),
        "label_count": len(class_counts),
        "class_counts": dict(sorted(class_counts.items())),
        "hash_mismatch_count": sum(int(row["hash_mismatch_count"]) for row in manifest_rows),
        "vector_dim": VECTOR_DIM,
        "file_slots": FILE_SLOTS,
        "anon_slots": ANON_SLOTS,
        "intra_class_cosine_median": _median_or_none(intra_cos),
        "inter_class_cosine_median": _median_or_none(inter_cos),
        "cosine_stability_margin": (
            _median_or_none(intra_cos) - _median_or_none(inter_cos)
            if intra_cos and inter_cos else None
        ),
        "intra_class_weighted_jaccard_median": _median_or_none(intra_jaccard),
        "inter_class_weighted_jaccard_median": _median_or_none(inter_jaccard),
        "pairwise_generated": include_pairwise,
    }
    (destination / "dataset_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    top_features: dict[str, list[str]] = {}
    for label in class_counts:
        counter: Counter[str] = Counter()
        for row in manifest_rows:
            if row["operation_label"] != label or row["support_eligible"] != "true":
                continue
            counter.update(key.split("\t", 1)[1] for key in row["feature_pages"] if key.startswith("FILE\t"))
        top_features[label] = [key for key, _ in counter.most_common(10)]
    lines = [
        "# WPS 用户操作—VMA 特征数据集分析",
        "",
        "本文件只做采集质量和向量相似度检查，不训练分类器，也不对分类效果作结论。",
        "",
        f"- 样本总数：`{summary['sample_count']}`；有效样本：`{summary['eligible_sample_count']}`。",
        f"- 固定向量维度：`{VECTOR_DIM}`（FILE `0-1023`，ANON `1024-2047`）。",
        f"- hash mismatch：`{summary['hash_mismatch_count']}`。",
        "",
        "## 各类样本",
        "",
        "| operation | total | eligible | zero vector | Top FILE features |",
        "|---|---:|---:|---:|---|",
    ]
    for label, counts in sorted(class_counts.items()):
        lines.append(f"| {label} | {counts['total']} | {counts['eligible']} | {counts['zero_vector']} | {'; '.join(top_features[label]) or '—'} |")
    lines.extend([
        "",
        "## 相似度摘要",
        "",
        f"- 同类 cosine 中位数：`{summary['intra_class_cosine_median']}`；异类 cosine 中位数：`{summary['inter_class_cosine_median']}`。",
        f"- cosine stability margin：`{summary['cosine_stability_margin']}`。",
        f"- 同类 weighted-Jaccard 中位数：`{summary['intra_class_weighted_jaccard_median']}`；异类 weighted-Jaccard 中位数：`{summary['inter_class_weighted_jaccard_median']}`。",
        "",
        "## 解释边界",
        "",
        "向量仅使用相对 baseline 的 FILE/ANON VMA referenced pages；PID、绝对地址、时间戳、trial/session/sample 标识和标签字段没有写入 2048 维特征。原始报告和窗口元数据保留在各 trial 目录中，便于追溯。",
    ])
    (destination / "dataset_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Read-only source dataset root")
    parser.add_argument("--output", type=Path, required=True, help="Separate output directory")
    parser.add_argument(
        "--compact-only",
        action="store_true",
        help="只使用 sequence 中的 aggregated_features 生成向量和汇总，不重新解析大型 VMA Markdown 报告",
    )
    parser.add_argument(
        "--skip-pairwise",
        action="store_true",
        help="跳过 O(n^2) pairwise_similarity 生成；核心向量和标签不受影响",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = build_dataset(
        args.root,
        include_raw_reports=not args.compact_only,
        include_pairwise=not args.skip_pairwise,
        output=args.output,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
