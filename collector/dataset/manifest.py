"""Filesystem layout and immutable raw-file helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DatasetLayout:
    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.data = self.root / "data"
        self.raw_session = self.data / "raw" / "session"
        self.raw_event = self.data / "raw" / "event"
        self.raw_smaps = self.data / "raw" / "smaps"
        self.raw_metadata = self.data / "raw" / "metadata"
        self.derived_snapshot = self.data / "derived" / "vma_snapshot"
        self.derived_feature = self.data / "derived" / "vma_feature"
        self.derived_quality = self.data / "derived" / "quality"
        self.manifest = self.data / "manifest"

    def ensure(self) -> None:
        for path in (
            self.raw_session, self.raw_event, self.raw_smaps, self.raw_metadata,
            self.derived_snapshot, self.derived_feature, self.derived_quality, self.manifest,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def write_raw_json(self, kind: str, identifier: str, payload: Any) -> Path:
        self.ensure()
        folder = {
            "session": self.raw_session,
            "event": self.raw_event,
            "smaps": self.raw_smaps,
            "metadata": self.raw_metadata,
        }[kind]
        path = folder / f"{identifier}.json"
        if path.exists():
            raise FileExistsError(f"raw file is immutable and already exists: {path}")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        return path

    def write_derived_json(self, kind: str, identifier: str, payload: Any) -> Path:
        self.ensure()
        folder = {
            "snapshot": self.derived_snapshot,
            "feature": self.derived_feature,
            "quality": self.derived_quality,
        }[kind]
        path = folder / f"{identifier}.json"
        if path.exists():
            raise FileExistsError(f"derived file already exists; use a new version id: {path}")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        return path
