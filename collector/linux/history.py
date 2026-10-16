"""Bounded, session-local recognition history; no hidden smoothing or next model."""

from collections import OrderedDict, deque
import math

from apps.wps.adapter import WpsAdapter


def event_from_prediction(sample: dict, prediction: dict) -> dict:
    """Join one Native prediction to capture provenance without reading its truth label."""
    if sample.get("status") != "success" or prediction.get("sample_id") != sample.get("sample_id"):
        raise ValueError("prediction and complete capture must have the same sample ID")
    score = float(prediction["cosine_similarity"])
    if not math.isfinite(score):
        raise ValueError("nonfinite recognition score")
    labels = {str(operation.action_spec["legacy_label"]): operation.operation_id
              for operation in WpsAdapter().catalog.operations}
    operation_id = labels[prediction["operation_label"]]
    return {"schema_version": "linux.operation-event.v1", "app_id": "wps",
            "session_id": sample["session_id"], "app_instance_id": sample["app_instance_id"],
            "event_id": sample["sample_id"], "operation_id": operation_id,
            "end_monotonic_ns": max(window["end_monotonic_ns"] for window in sample["operation_windows"]),
            "source_kind": "recognition", "method": "native_class_centroid_cosine",
            "score": score, "score_kind": "cosine_similarity", "is_mock": sample["is_mock"]}


def event_from_native_runtime(event: dict) -> dict:
    """Convert a successful unlabeled Native runtime result using catalog labels.

    Native replay fixtures or error/health events cannot enter live history.
    No automation operation name or validation truth is read by this adapter.
    """
    if (event.get("schema") != "harmony.ops.recognized.v1" or event.get("status") != "success"
            or event.get("app_id") != "wps" or event.get("source") != "controlled_live_window"
            or event.get("method") != "vma-centroid" or event.get("clock") != "monotonic_ns"
            or event.get("feature_schema") != "wps.vma.feature.v1"):
        raise ValueError("a successful Native controlled-window recognition event is required")
    for field in ("event_id", "window_id", "session_id", "app_instance_id"):
        if not isinstance(event.get(field), str) or not event[field]:
            raise ValueError("incomplete Native runtime event identity")
    start, end = event.get("start_ns"), event.get("end_ns")
    if type(start) is not int or type(end) is not int or not 0 < start < end:
        raise ValueError("invalid Native runtime monotonic interval")
    score = event.get("cosine_similarity")
    if type(score) not in (int, float) or not math.isfinite(score):
        raise ValueError("nonfinite Native recognition score")
    labels = {str(operation.action_spec["legacy_label"]): operation.operation_id
              for operation in WpsAdapter().catalog.operations}
    label = event.get("operation_label")
    if label not in labels:
        raise ValueError("Native label is absent from the frozen WPS catalog")
    return {"schema_version": "linux.operation-event.v1", "app_id": "wps",
            "session_id": event["session_id"], "app_instance_id": event["app_instance_id"],
            "event_id": event["event_id"], "window_id": event["window_id"],
            "operation_id": labels[label], "end_monotonic_ns": end,
            "source_kind": "recognition", "method": "native_class_centroid_cosine",
            "score": score, "score_kind": "cosine_similarity", "is_mock": False,
            "native_schema": event["schema"]}


class OperationHistory:
    """Use one store per running session; explicit event IDs identify duplicates.

    A caller invokes reset on application restarts/session changes and on gaps
    for which no reliable operation was observed. Repeated labels are retained.
    This class consumes recognition events, never ground-truth labels.
    """

    def __init__(self, history_size: int = 64, dedup_size: int = 1024) -> None:
        if history_size <= 0 or dedup_size < history_size:
            raise ValueError("dedup capacity must cover a positive history capacity")
        self.history_size = history_size
        self.dedup_size = dedup_size
        self.key = None
        self.events = deque(maxlen=history_size)
        self.seen = OrderedDict()
        self.last_end_ns = -1

    def reset(self, session_id: str, app_instance_id: str) -> None:
        if not session_id or not app_instance_id:
            raise ValueError("session and application instance are required")
        self.key = (session_id, app_instance_id)
        self.events.clear()
        self.seen.clear()
        self.last_end_ns = -1

    def append(self, event: dict) -> bool:
        required = ("session_id", "app_instance_id", "event_id", "operation_id", "end_monotonic_ns")
        if any(not event.get(field) for field in required) or event.get("source_kind") != "recognition":
            raise ValueError("a complete recognition event is required")
        if self.key != (event["session_id"], event["app_instance_id"]):
            raise ValueError("reset explicitly before changing application/session")
        end = int(event["end_monotonic_ns"])
        signature = (event["operation_id"], end)
        previous = self.seen.get(event["event_id"])
        if previous is not None:
            if previous != signature:
                raise ValueError("conflicting duplicate event ID")
            return False
        if end <= self.last_end_ns:
            raise ValueError("out-of-order recognition event")
        self.seen[event["event_id"]] = signature
        if len(self.seen) > self.dedup_size:
            self.seen.popitem(last=False)
        self.events.append({field: event[field] for field in required})
        self.last_end_ns = end
        return True

    def prediction_request(self) -> dict:
        if self.key is None:
            raise ValueError("history has no application/session")
        return {"schema_version": "linux.next-operation-request.v1", "app_id": "wps",
                "session_id": self.key[0], "app_instance_id": self.key[1],
                "history_ops": [event["operation_id"] for event in self.events]}
