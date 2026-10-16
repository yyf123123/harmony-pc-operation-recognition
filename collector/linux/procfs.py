"""Explicit PID-set access: no HDC, process-name search, or automatic elevation."""

from __future__ import annotations

import csv
import io
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from features.wps import _semantic_key


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int


def identity(pid: int, proc_root: Path = Path("/proc")) -> ProcessIdentity:
    if pid <= 0:
        raise CaptureError("PID must be positive")
    try:
        text = (proc_root / str(pid) / "stat").read_text()
        prefix, separator, tail = text.rpartition(") ")
        fields = tail.split()
        if not separator or int(prefix.split("(", 1)[0]) != pid or len(fields) < 20:
            raise ValueError("invalid process stat")
        if fields[0] in {"Z", "X"}:
            raise CaptureError(f"process {pid} has exited")
        return ProcessIdentity(pid, int(fields[19]))
    except (OSError, ValueError, IndexError) as error:
        raise CaptureError(f"cannot identify process {pid}") from error


def parse_compact(text: str, expected_pids: set[int], page_size: int) -> dict:
    """Keep the C collector's segments, actual page counts and WPS semantic keys."""
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    expected_header = ["pid", "process_name", "segment", "perms", "referenced_kib",
                       "referenced_pages", "pathname"]
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t")
    if reader.fieldnames != expected_header or page_size <= 0:
        raise CaptureError("invalid compact header or page size")
    seen: set[int] = set()
    pages: dict[str, float] = {}
    metadata: dict[str, dict] = {}
    for row in reader:
        try:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("truncated compact row")
            pid = int(row["pid"])
            count, kib = float(row["referenced_pages"]), float(row["referenced_kib"])
            if (pid not in expected_pids or not math.isfinite(count) or not math.isfinite(kib)
                    or count < 0 or kib < 0 or count != math.ceil(kib * 1024 / page_size)
                    or not re.fullmatch(r"[r-][w-][x-][ps]", row["perms"])):
                raise ValueError("invalid compact data")
        except (ValueError, TypeError, KeyError) as error:
            raise CaptureError("invalid compact VMA row") from error
        pathname = row["pathname"] or "(anonymous)"
        namespace = "FILE" if pathname.startswith("/") or row["segment"].lower().startswith("filepage") else "ANON"
        key = _semantic_key(namespace, row["segment"], row["perms"], pathname)
        feature_id = f"{namespace}\t{key}"
        pages[feature_id] = pages.get(feature_id, 0.0) + count
        metadata[feature_id] = {"process_role": row["process_name"], "pathname": pathname}
        seen.add(pid)
    if seen != expected_pids:
        raise CaptureError("empty or incomplete PID coverage in compact output")
    return {"feature_pages": pages, "feature_meta": metadata}


class LinuxProcfs:
    def __init__(self, pids: list[int], collector: Path, *, timeout: float = 15.0) -> None:
        if sys.platform != "linux":
            raise CaptureError("live /proc capture requires Linux; use replay for offline validation")
        if not pids or len(pids) != len(set(pids)) or timeout <= 0 or not math.isfinite(timeout):
            raise CaptureError("unique explicit PIDs and a finite positive timeout are required")
        self.collector = collector.resolve(strict=True)
        if not self.collector.is_file() or not os.access(self.collector, os.X_OK):
            raise CaptureError("collector must be an executable file")
        self.timeout = timeout
        self.identities = tuple(identity(pid) for pid in sorted(pids))
        for item in self.identities:
            try:
                with (Path("/proc") / str(item.pid) / "smaps").open() as handle:
                    if not handle.read(1):
                        raise CaptureError("target smaps is empty")
            except OSError as error:
                raise CaptureError("target smaps is unreadable; check process permissions") from error
        self.check()

    @property
    def pids(self) -> set[int]:
        return {item.pid for item in self.identities}

    def check(self) -> None:
        for original in self.identities:
            if identity(original.pid) != original:
                raise CaptureError(f"process {original.pid} restarted; discard this sample")

    def clear(self) -> None:
        self.check()
        try:
            for item in self.identities:
                (Path("/proc") / str(item.pid) / "clear_refs").write_text("1\n")
        except OSError as error:
            raise CaptureError("clear_refs failed; check target-process permissions") from error
        self.check()

    def sample(self, destination: Path, page_size: int) -> dict:
        self.check()
        try:
            result = subprocess.run(
                [str(self.collector), *map(str, sorted(self.pids)), "--compact-vma"],
                capture_output=True, text=True, timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CaptureError("native VMA collector could not finish") from error
        if result.returncode:
            raise CaptureError(f"native VMA collector failed (exit {result.returncode})")
        self.check()
        values = parse_compact(result.stdout, self.pids, page_size)
        destination.write_text(result.stdout, encoding="utf-8")
        values["report_paths"] = [str(destination)]
        return values
