"""Versioned, explicit Markov artifacts; predictions never become history events.

The count/backoff formula is the existing operation_predictor OpMarkovModel.
No training runs on import and no synthetic fallback model is installed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Iterable

SCHEMA = "linux.next-operation-markov.v1"


def _integer(value, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


class OpMarkovModel:
    """Existing order 1..4 count model, with a checked canonical operation vocabulary."""

    def __init__(self, order: int = 1, op_vocab: dict[str, dict[str, int]] | None = None):
        if type(order) is not int or order not in (1, 2, 3, 4):
            raise ValueError("order must be between 1 and 4")
        self.order = order
        self.op_vocab = json.loads(json.dumps(op_vocab or {}))
        self.transitions = defaultdict(lambda: defaultdict(Counter))
        self.global_counts = defaultdict(Counter)
        self._validate_vocab()

    def _validate_vocab(self):
        if not isinstance(self.op_vocab, dict):
            raise ValueError("vocabulary must be an application dictionary")
        for app, vocab in self.op_vocab.items():
            if not isinstance(app, str) or not app or not isinstance(vocab, dict) or not vocab:
                raise ValueError("each application needs a nonempty vocabulary")
            ids = []
            for name, value in vocab.items():
                if not isinstance(name, str) or not name:
                    raise ValueError("empty operation name")
                ids.append(_integer(value, "vocabulary ID"))
            if len(set(ids)) != len(ids):
                raise ValueError("vocabulary IDs must be unique within an application")

    def _special_ids(self, app):
        return {value for name, value in self.op_vocab.get(app, {}).items() if name in ("<PAD>", "<UNK>")}

    def _check_ids(self, app, ids):
        if app not in self.op_vocab:
            raise ValueError("application is absent from the vocabulary")
        known = set(self.op_vocab[app].values())
        values = [_integer(value, "operation ID") for value in ids]
        if any(value not in known for value in values):
            raise ValueError("unknown operation ID")
        return [value for value in values if value not in self._special_ids(app)]

    def fit(self, samples: Iterable[dict]):
        # Preserve the original fit semantics, including counts from history.
        for sample in samples:
            app = sample["app"]
            history = self._check_ids(app, sample.get("history_ops", []))
            label = _integer(sample["label_next_op"], "next operation ID")
            checked = self._check_ids(app, [label])
            if not checked:
                continue
            for value in history:
                self.global_counts[app][value] += 1
            self.global_counts[app][label] += 1
            for idx in range(1, len(history)):
                for size in range(1, min(self.order, idx) + 1):
                    self.transitions[app][tuple(history[idx-size:idx])][history[idx]] += 1
            for size in range(1, min(self.order, len(history)) + 1):
                self.transitions[app][tuple(history[-size:])][label] += 1

    def distribution(self, app, history_ops):
        history = self._check_ids(app, history_ops)
        for size in range(min(self.order, len(history)), 0, -1):
            counts = self.transitions.get(app, {}).get(tuple(history[-size:]))
            if counts:
                return counts, size
        return self.global_counts.get(app, Counter()), 0

    def predict_topk(self, app, history_ops, k=5):
        _integer(k, "top k", 1)
        counts, _ = self.distribution(app, history_ops)
        return [op for op, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:k]]

    def export(self, *, model_id: str, vocab_version: str, source_kind: str,
               source_sessions: list[str]) -> dict:
        if not model_id or not vocab_version or source_kind not in ("real", "synthetic_fixture"):
            raise ValueError("model, vocabulary and explicit training provenance are required")
        if not source_sessions or any(not isinstance(item, str) or not item for item in source_sessions):
            raise ValueError("source session IDs are required")
        if len(set(source_sessions)) != len(source_sessions):
            raise ValueError("source sessions must be unique")
        artifact = {"schema_version": SCHEMA, "model_id": model_id, "vocab_version": vocab_version,
                    "order": self.order, "op_vocab": self.op_vocab,
                    "training_provenance": {"source_kind": source_kind,
                        "source_sessions": source_sessions, "split_unit": "session"},
                    "global_counts": {app: sorted(counts.items()) for app, counts in sorted(self.global_counts.items())},
                    "transitions": {app: [{"context": list(context), "counts": sorted(counts.items())}
                        for context, counts in sorted(contexts.items())]
                        for app, contexts in sorted(self.transitions.items())}}
        # Export and import use exactly the same validation.
        MarkovArtifact.from_dict(artifact)
        return artifact


class MarkovArtifact:
    def __init__(self, model: OpMarkovModel, payload: dict):
        self.model = model
        self.payload = payload
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.sha256 = hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, payload: dict):
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
            raise ValueError("unsupported Markov artifact schema")
        for key in ("model_id", "vocab_version"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise ValueError(f"missing {key}")
        provenance = payload.get("training_provenance", {})
        if not isinstance(provenance, dict):
            raise ValueError("invalid training provenance")
        sessions = provenance.get("source_sessions")
        if (provenance.get("source_kind") not in ("real", "synthetic_fixture")
                or provenance.get("split_unit") != "session" or not isinstance(sessions, list)
                or not sessions or any(not isinstance(item, str) or not item for item in sessions)
                or len(set(sessions)) != len(sessions)):
            raise ValueError("invalid training provenance")
        model = OpMarkovModel(payload["order"], payload["op_vocab"])
        if not model.op_vocab:
            raise ValueError("empty artifact vocabulary")

        def counts(app, rows):
            if not isinstance(rows, list) or not rows:
                raise ValueError("empty or invalid counts")
            result = Counter()
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    raise ValueError("invalid count row")
                op, count = row
                valid = model._check_ids(app, [op])
                if not valid or op in result:
                    raise ValueError("special or duplicate output ID")
                result[op] = _integer(count, "count", 1)
            return result

        globals_ = payload["global_counts"]
        transitions = payload["transitions"]
        if not isinstance(globals_, dict) or not globals_ or not isinstance(transitions, dict):
            raise ValueError("artifact has no trained counts")
        for app, rows in globals_.items():
            model.global_counts[app] = counts(app, rows)
        for app, contexts in transitions.items():
            if app not in model.global_counts or not isinstance(contexts, list):
                raise ValueError("transition application has no global counts")
            for row in contexts:
                if not isinstance(row, dict):
                    raise ValueError("invalid transition row")
                context = row["context"]
                if not isinstance(context, list) or not 1 <= len(context) <= model.order:
                    raise ValueError("invalid transition order")
                checked = model._check_ids(app, context)
                if len(checked) != len(context) or tuple(context) in model.transitions[app]:
                    raise ValueError("special or duplicate context")
                model.transitions[app][tuple(context)] = counts(app, row["counts"])
        return cls(model, payload)

    @classmethod
    def load(cls, path: str | Path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path):
        with Path(path).open("x", encoding="utf-8") as handle:
            json.dump(self.payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")

    def predict(self, request: dict, *, k: int = 5, allow_fixture: bool = False) -> dict:
        base = {"schema_version": "linux.next-operation-result.v1", "source_kind": "prediction",
                "app_id": request.get("app_id"), "session_id": request.get("session_id"),
                "app_instance_id": request.get("app_instance_id"), "model_id": self.payload["model_id"],
                "model_sha256": self.sha256, "vocab_version": self.payload["vocab_version"], "candidates": []}
        if self.payload["training_provenance"]["source_kind"] != "real" and not allow_fixture:
            return {**base, "status": "not_ready", "reason": "synthetic_model_disabled"}
        if (request.get("schema_version") != "linux.next-operation-request.v1"
                or not all(isinstance(request.get(field), str) and request[field]
                           for field in ("app_id", "session_id", "app_instance_id"))
                or not isinstance(request.get("history_ops"), list)):
            raise ValueError("invalid history prediction request")
        _integer(k, "top k", 1)
        app = request["app_id"]
        vocab = self.model.op_vocab.get(app, {})
        history = request["history_ops"]
        if not history:
            return {**base, "status": "not_ready", "reason": "empty_history"}
        if any(not isinstance(op, str) or op not in vocab or op in ("<PAD>", "<UNK>") for op in history):
            return {**base, "status": "not_ready", "reason": "vocabulary_mismatch"}
        values, context_order = self.model.distribution(app, [vocab[op] for op in history])
        if not values:
            return {**base, "status": "not_ready", "reason": "application_untrained"}
        inverse = {value: name for name, value in vocab.items()}
        total = sum(values.values())
        candidates = [{"operation_id": inverse[op], "rank": rank, "score": count / total,
                       "score_kind": "markov_empirical_frequency", "support_count": count}
                      for rank, (op, count) in enumerate(sorted(values.items(), key=lambda item: (-item[1], item[0]))[:k], 1)]
        return {**base, "status": "ready", "context_order": context_order, "candidates": candidates}


def predict_or_not_ready(artifact: MarkovArtifact | None, request: dict, **kwargs) -> dict:
    if artifact is None:
        return {"schema_version": "linux.next-operation-result.v1", "source_kind": "prediction",
                "app_id": request.get("app_id"), "session_id": request.get("session_id"),
                "app_instance_id": request.get("app_instance_id"), "status": "not_ready",
                "reason": "model_unavailable", "candidates": []}
    return artifact.predict(request, **kwargs)
