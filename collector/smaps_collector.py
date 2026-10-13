"""Portable parser for Linux/HarmonyOS smaps-style VMA text."""

from __future__ import annotations

import re
from typing import Iterable

from .snapshot import VmaRegion, VmaSnapshot


class SmapsEmptyError(RuntimeError):
    """The device returned no parseable VMA rows for a required capture."""

_HEADER = re.compile(
    r"^(?P<start>[0-9a-fA-F]+)-(?P<end>[0-9a-fA-F]+)\s+"
    r"(?P<perms>\S+)\s+(?P<offset>\S+)\s+(?P<dev>\S+)\s+"
    r"(?P<inode>\S+)(?:\s+(?P<path>.*))?$"
)


def _number(line: str) -> float:
    match = re.search(r"(-?\d+(?:\.\d+)?)", line)
    return float(match.group(1)) if match else 0.0


def parse_smaps_text(text: str) -> list[VmaRegion]:
    regions: list[VmaRegion] = []
    current: dict[str, object] | None = None

    def finish() -> None:
        if current is None:
            return
        regions.append(
            VmaRegion(
                start=int(current["start"]),
                end=int(current["end"]),
                permissions=str(current["permissions"]),
                pathname=str(current["pathname"]),
                segment=str(current.get("segment", "FilePage other")),
                referenced_pages=float(current.get("referenced_pages", 0.0)),
                rss_pages=float(current.get("rss_pages", 0.0)),
                pss_pages=float(current.get("pss_pages", 0.0)),
                private_clean_pages=float(current.get("private_clean_pages", 0.0)),
                private_dirty_pages=float(current.get("private_dirty_pages", 0.0)),
                anonymous_pages=float(current.get("anonymous_pages", 0.0)),
            )
        )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        header = _HEADER.match(line)
        if header:
            finish()
            path = header.group("path") or "(anonymous)"
            segment = "FilePage other" if path.startswith("/") else "AnonPage other"
            current = {
                "start": int(header.group("start"), 16),
                "end": int(header.group("end"), 16),
                "permissions": header.group("perms"),
                "pathname": path,
                "segment": segment,
            }
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        number = _number(value)
        if key == "referenced":
            current["referenced_pages"] = number / 4.0
        elif key == "rss":
            current["rss_pages"] = number / 4.0
        elif key == "pss":
            current["pss_pages"] = number / 4.0
        elif key == "private_clean":
            current["private_clean_pages"] = number / 4.0
        elif key == "private_dirty":
            current["private_dirty_pages"] = number / 4.0
        elif key == "anonymous":
            current["anonymous_pages"] = number / 4.0
        elif key in {"vma_segment", "segment"}:
            current["segment"] = value.strip()
    finish()
    return regions


def collect_snapshot_from_smaps(
    text: str,
    *,
    snapshot_id: str,
    app_id: str,
    phase: str,
    source: str = "device",
    is_mock: bool = False,
    pid: str | None = None,
) -> VmaSnapshot:
    regions = parse_smaps_text(text)
    if not regions:
        raise SmapsEmptyError(f"empty smaps for {app_id}/{snapshot_id}")
    return VmaSnapshot.now(
        snapshot_id=snapshot_id,
        app_id=app_id,
        phase=phase,
        regions=regions,
        source=source,
        is_mock=is_mock,
        pid=pid,
    )
