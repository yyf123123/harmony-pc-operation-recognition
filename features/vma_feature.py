"""App-agnostic VMA feature construction.

The frozen WPS implementation is selectable with
``schema_version=LEGACY_WPS_FEATURE_SCHEMA_VERSION``; the default is the
cross-app v2 identifier and does not alter the transform or dimensionality.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from collector.snapshot import VmaSnapshot, aggregate_snapshot_features, aggregate_snapshot_metric
from .feature_schema import (
    ANON_SLOTS,
    CROSS_APP_FEATURE_SCHEMA_VERSION,
    CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION,
    FILE_SLOTS,
    SIGNED_VECTOR_DIM,
    VECTOR_DIM,
    schema_for,
)


def stable_hash_index(namespace: str, feature_key: str) -> int:
    namespace = namespace.upper()
    if namespace not in {"FILE", "ANON"}:
        raise ValueError(f"unsupported feature namespace: {namespace}")
    digest = hashlib.sha256(f"{namespace}:{feature_key}".encode("utf-8")).digest()
    slot = int.from_bytes(digest[:8], "big")
    return slot % FILE_SLOTS if namespace == "FILE" else FILE_SLOTS + slot % ANON_SLOTS


def build_raw_vector(file_features: dict[str, float], anon_features: dict[str, float]) -> list[float]:
    vector = [0.0] * VECTOR_DIM
    for key, pages in file_features.items():
        vector[stable_hash_index("FILE", key)] += math.log1p(max(float(pages), 0.0))
    for key, pages in anon_features.items():
        vector[stable_hash_index("ANON", key)] += math.log1p(max(float(pages), 0.0))
    return vector


_SIGNED_CHANNEL_INDEX = {
    ("referenced", "positive"): 0,
    ("referenced", "negative"): 1,
    ("rss", "positive"): 2,
    ("rss", "negative"): 3,
}


def signed_hash_index(metric: str, direction: str, namespace: str, feature_key: str) -> int:
    """Return the v3 slot for one metric/direction/semantic-VMA signal."""
    namespace = namespace.upper()
    if namespace not in {"FILE", "ANON"}:
        raise ValueError(f"unsupported feature namespace: {namespace}")
    try:
        channel = _SIGNED_CHANNEL_INDEX[(metric, direction)]
    except KeyError as error:
        raise ValueError(f"unsupported signed channel: {metric}/{direction}") from error
    digest = hashlib.sha256(f"{namespace}:{feature_key}".encode("utf-8")).digest()
    slot = int.from_bytes(digest[:8], "big") % FILE_SLOTS
    namespace_offset = 0 if namespace == "FILE" else FILE_SLOTS
    return channel * VECTOR_DIM + namespace_offset + slot


def build_signed_raw_vector(channels: dict[str, dict[tuple[str, str], float]]) -> list[float]:
    """Build the frozen v3 layout without app, process, PID, or path rules."""
    vector = [0.0] * SIGNED_VECTOR_DIM
    for channel_name, features in channels.items():
        metric, direction = channel_name.rsplit("_", 1)
        for (namespace, key), pages in features.items():
            if pages > 0.0:
                vector[signed_hash_index(metric, direction, namespace, key)] += math.log1p(float(pages))
    return vector


def l2_normalize(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    return values if norm == 0.0 else [value / norm for value in values]


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    denominator = math.sqrt(sum(value * value for value in left_values)) * math.sqrt(sum(value * value for value in right_values))
    if denominator == 0.0:
        return 1.0 if left_values == right_values else 0.0
    return sum(a * b for a, b in zip(left_values, right_values)) / denominator


@dataclass(frozen=True)
class VmaFeature:
    sample_id: str
    app_id: str
    operation_id: str
    schema_version: str
    raw_vector: tuple[float, ...]
    l2_vector: tuple[float, ...]
    file_features: dict[str, float] = field(default_factory=dict)
    anon_features: dict[str, float] = field(default_factory=dict)
    signal_strength: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    channel_features: dict[str, dict[str, float]] = field(default_factory=dict)


def _median_maps(baselines: Iterable[VmaSnapshot]) -> dict[tuple[str, str], float]:
    maps = [aggregate_snapshot_features(snapshot) for snapshot in baselines]
    keys = set().union(*(item.keys() for item in maps)) if maps else set()
    return {key: statistics.median(item.get(key, 0.0) for item in maps) for key in keys}


def _median_metric_maps(baselines: Iterable[VmaSnapshot], metric: str) -> dict[tuple[str, str], float]:
    maps = [aggregate_snapshot_metric(snapshot, metric) for snapshot in baselines]
    keys = set().union(*(item.keys() for item in maps)) if maps else set()
    return {key: statistics.median(item.get(key, 0.0) for item in maps) for key in keys}


def _build_signed_feature(
    *,
    sample_id: str,
    app_id: str,
    operation_id: str,
    baselines: list[VmaSnapshot],
    action: VmaSnapshot,
    post_action: VmaSnapshot,
    post_actions: Iterable[VmaSnapshot] | None,
    schema_version: str,
    metadata: dict[str, Any] | None,
) -> VmaFeature:
    after_snapshots = [action, post_action, *(post_actions or ())]
    channels: dict[str, dict[tuple[str, str], float]] = {
        f"{metric}_{direction}": {}
        for metric in ("referenced", "rss")
        for direction in ("positive", "negative")
    }
    for metric in ("referenced", "rss"):
        baseline = _median_metric_maps(baselines, metric)
        after_values = [aggregate_snapshot_metric(snapshot, metric) for snapshot in after_snapshots]
        keys = set(baseline).union(*(item.keys() for item in after_values)) if after_values else set(baseline)
        for key in keys:
            baseline_value = baseline.get(key, 0.0)
            values = [item.get(key, 0.0) for item in after_values]
            positive = max(values, default=0.0) - baseline_value
            negative = baseline_value - min(values, default=0.0)
            if positive > 0.0:
                channels[f"{metric}_positive"][key] = positive
            if negative > 0.0:
                channels[f"{metric}_negative"][key] = negative

    raw = build_signed_raw_vector(channels)
    channel_features: dict[str, dict[str, float]] = {}
    file_features: dict[str, float] = {}
    anon_features: dict[str, float] = {}
    channel_signal_strength: dict[str, float] = {}
    for channel_name, values in channels.items():
        for namespace in ("FILE", "ANON"):
            named = {key: value for (kind, key), value in values.items() if kind == namespace}
            display_name = f"{channel_name}_{namespace.lower()}"
            channel_features[display_name] = named
            channel_signal_strength[display_name] = math.sqrt(
                sum(math.log1p(value) ** 2 for value in named.values())
            )
            target = file_features if namespace == "FILE" else anon_features
            target.update({f"{channel_name}|{key}": value for key, value in named.items()})
    return VmaFeature(
        sample_id=sample_id,
        app_id=app_id,
        operation_id=operation_id,
        schema_version=schema_version,
        raw_vector=tuple(raw),
        l2_vector=tuple(l2_normalize(raw)),
        file_features=file_features,
        anon_features=anon_features,
        signal_strength=math.sqrt(sum(value * value for value in raw)),
        metadata={
            **(metadata or {}),
            "baseline_window_count": len(baselines),
            "after_window_count": len(after_snapshots),
            "channel_signal_strength": channel_signal_strength,
        },
        channel_features=channel_features,
    )


def build_vma_feature(
    *,
    sample_id: str,
    app_id: str,
    operation_id: str,
    baselines: Iterable[VmaSnapshot],
    action: VmaSnapshot,
    post_action: VmaSnapshot,
    post_actions: Iterable[VmaSnapshot] | None = None,
    schema_version: str = FEATURE_SCHEMA_VERSION,
    metadata: dict[str, Any] | None = None,
) -> VmaFeature:
    baseline_snapshots = list(baselines)
    schema_for(schema_version).validate()
    if schema_version == CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION:
        return _build_signed_feature(
            sample_id=sample_id,
            app_id=app_id,
            operation_id=operation_id,
            baselines=baseline_snapshots,
            action=action,
            post_action=post_action,
            post_actions=post_actions,
            schema_version=schema_version,
            metadata=metadata,
        )
    baseline = _median_maps(baseline_snapshots)
    action_values = aggregate_snapshot_features(action)
    post_values = aggregate_snapshot_features(post_action)
    keys = set(action_values) | set(post_values)
    excess: dict[tuple[str, str], float] = {}
    for key in keys:
        value = max(action_values.get(key, 0.0), post_values.get(key, 0.0)) - baseline.get(key, 0.0)
        if value > 0.0:
            excess[key] = value
    file_features = {key: value for (namespace, key), value in excess.items() if namespace == "FILE"}
    anon_features = {key: value for (namespace, key), value in excess.items() if namespace == "ANON"}
    raw = build_raw_vector(file_features, anon_features)
    return VmaFeature(
        sample_id=sample_id,
        app_id=app_id,
        operation_id=operation_id,
        schema_version=schema_version,
        raw_vector=tuple(raw),
        l2_vector=tuple(l2_normalize(raw)),
        file_features=file_features,
        anon_features=anon_features,
        signal_strength=math.sqrt(sum(value * value for value in raw)),
        metadata={**(metadata or {}), "baseline_window_count": len(baseline_snapshots)},
    )


def build_cross_app_feature_pair(
    *,
    sample_id: str,
    app_id: str,
    operation_id: str,
    baselines: Iterable[VmaSnapshot],
    action: VmaSnapshot,
    post_action: VmaSnapshot,
    post_actions: Iterable[VmaSnapshot] | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[VmaFeature, VmaFeature]:
    """Build frozen v2 and signed v3 from exactly the same raw windows."""
    baseline_snapshots = list(baselines)
    later_snapshots = list(post_actions or ())
    return (
        build_vma_feature(
            sample_id=sample_id,
            app_id=app_id,
            operation_id=operation_id,
            baselines=baseline_snapshots,
            action=action,
            post_action=post_action,
            schema_version=CROSS_APP_FEATURE_SCHEMA_VERSION,
            metadata=metadata,
        ),
        build_vma_feature(
            sample_id=sample_id,
            app_id=app_id,
            operation_id=operation_id,
            baselines=baseline_snapshots,
            action=action,
            post_action=post_action,
            post_actions=later_snapshots,
            schema_version=CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION,
            metadata=metadata,
        ),
    )
