"""Verified file-page candidates; no kernel apply and no inferred cold pages.

Content SHA-256 plus byte length is the cross-session file identity. Virtual
addresses and PFNs are deliberately not accepted as file identity fields.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time

SCHEMA = "linux.operation-file-pages.v1"
_HEX = re.compile(r"[0-9a-f]{64}")


def _positive_int(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def identity_key(identity: dict) -> tuple[str, int]:
    if (not isinstance(identity, dict) or set(identity) != {"sha256", "size_bytes"}
            or not isinstance(identity["sha256"], str) or not _HEX.fullmatch(identity["sha256"])):
        raise ValueError("file identity needs content SHA-256 and byte length only")
    return identity["sha256"], _positive_int(identity["size_bytes"], "file size")


def inspect_file(path: Path) -> dict:
    """Hash an explicitly selected test file without publishing its path/content."""
    digest = hashlib.sha256()
    before = path.stat()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("file changed during identity inspection")
    result = {"sha256": digest.hexdigest(), "size_bytes": after.st_size}
    identity_key(result)
    return result


class PageMapping:
    def __init__(self, payload: dict):
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
            raise ValueError("unsupported file-page mapping schema")
        for field in ("mapping_id", "mapping_version", "app_id", "app_version", "label_schema_version"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise ValueError(f"missing {field}")
        if payload.get("source_kind") not in ("real", "synthetic_fixture"):
            raise ValueError("explicit mapping source kind is required")
        sources = payload.get("source_sessions")
        if (not isinstance(sources, list) or not sources or any(not isinstance(item, str) or not item for item in sources)
                or len(set(sources)) != len(sources)):
            raise ValueError("source sessions are required")
        created = _positive_int(payload.get("created_unix_ns"), "mapping creation time")
        expires = _positive_int(payload.get("expires_unix_ns"), "mapping expiration time")
        if expires <= created:
            raise ValueError("mapping expiration must follow creation")
        _positive_int(payload.get("candidate_ttl_ns"), "candidate TTL")
        rows = payload.get("entries")
        if not isinstance(rows, list) or not rows:
            raise ValueError("mapping entries are required")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid mapping row")
            if not isinstance(row.get("operation_id"), str) or not row["operation_id"]:
                raise ValueError("missing mapped operation")
            file_key = identity_key(row.get("file_identity"))
            page_size = _positive_int(row.get("page_size_bytes"), "page size")
            if page_size & (page_size - 1):
                raise ValueError("page size must be a power of two")
            index = _positive_int(row.get("file_page_index"), "page index", 0)
            if index >= (file_key[1] + page_size - 1) // page_size:
                raise ValueError("page index is outside the file")
            _positive_int(row.get("support_count"), "support count")
            frequency = row.get("access_frequency")
            if (type(frequency) not in (float, int) or not math.isfinite(frequency)
                    or not 0 < frequency <= 1):
                raise ValueError("access frequency must be in (0,1]")
            if row.get("evidence_kind") != "observed_file_page_access":
                raise ValueError("VMA aggregate evidence cannot identify exact file pages")
            key = (row["operation_id"], file_key, page_size, index)
            if key in seen:
                raise ValueError("duplicate operation/file page entry")
            seen.add(key)
        self.payload = payload
        self.sha256 = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def query(self, prediction: dict, *, app_version: str, label_schema_version: str,
              observed_file_identities: list[dict], page_size_bytes: int, clock_domain: str,
              now_monotonic_ns: int | None = None, now_unix_ns: int | None = None,
              allow_fixture: bool = False) -> dict:
        if not clock_domain:
            raise ValueError("host/boot clock domain is required")
        _positive_int(page_size_bytes, "observed system page size")
        mono = time.monotonic_ns() if now_monotonic_ns is None else _positive_int(now_monotonic_ns, "monotonic time")
        wall = time.time_ns() if now_unix_ns is None else _positive_int(now_unix_ns, "wall time")
        base = {"schema_version": "linux.file-page-candidates.v1", "app_id": prediction.get("app_id"),
                "session_id": prediction.get("session_id"), "app_instance_id": prediction.get("app_instance_id"),
                "mapping_id": self.payload["mapping_id"], "mapping_version": self.payload["mapping_version"],
                "mapping_sha256": self.sha256, "clock_domain": clock_domain,
                "generated_monotonic_ns": mono, "candidates": [], "scope": "user_space_candidates_only"}

        def unknown(reason):
            return {**base, "status": "unknown", "reason": reason}

        if prediction.get("status") != "ready" or prediction.get("source_kind") != "prediction":
            return unknown("prediction_not_ready")
        if self.payload["source_kind"] != "real" and not allow_fixture:
            return unknown("synthetic_mapping_disabled")
        if (prediction.get("app_id") != self.payload["app_id"] or app_version != self.payload["app_version"]
                or label_schema_version != self.payload["label_schema_version"]):
            return unknown("application_version_mismatch")
        if not self.payload["created_unix_ns"] <= wall < self.payload["expires_unix_ns"]:
            return unknown("mapping_expired_or_not_yet_valid")
        observed = {identity_key(identity) for identity in observed_file_identities}
        operations = {}
        for candidate in prediction.get("candidates", []):
            op = candidate.get("operation_id")
            score = candidate.get("score")
            if (not isinstance(op, str) or op in operations or type(score) not in (int, float)
                    or not math.isfinite(score) or not 0 <= score <= 1
                    or candidate.get("score_kind") != "markov_empirical_frequency"):
                raise ValueError("invalid next-operation candidate")
            operations[op] = candidate
        candidates = {}
        for row in self.payload["entries"]:
            op = operations.get(row["operation_id"])
            if not op or identity_key(row["file_identity"]) not in observed or row["page_size_bytes"] != page_size_bytes:
                continue
            key = (identity_key(row["file_identity"]), row["file_page_index"])
            # Multiple operations may support the same page: preserve evidence, do
            # not sum correlated frequencies into a fabricated probability.
            candidate = candidates.setdefault(key, {"file_identity": row["file_identity"],
                "file_page_index": row["file_page_index"], "page_size_bytes": page_size_bytes,
                "label": "hot_candidate", "identity_status": "verified_content_sha256",
                "expires_monotonic_ns": mono + min(self.payload["candidate_ttl_ns"],
                                                  self.payload["expires_unix_ns"] - wall), "evidence": []})
            candidate["evidence"].append({"operation_id": row["operation_id"],
                "prediction_score": op["score"], "score_kind": op["score_kind"],
                "access_frequency": row["access_frequency"], "support_count": row["support_count"]})
        if not candidates:
            return unknown("no_verified_mapping_match")
        return {**base, "status": "candidates", "candidates": list(candidates.values())}


def candidates_or_unknown(mapping: PageMapping | None, prediction: dict, **kwargs):
    if mapping is None:
        return {"schema_version": "linux.file-page-candidates.v1", "status": "unknown",
                "reason": "mapping_unavailable", "candidates": [], "scope": "user_space_candidates_only"}
    return mapping.query(prediction, **kwargs)
