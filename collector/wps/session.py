#!/usr/bin/env python3
"""Run a real HarmonyOS WPS document workload and collect v6 reports.

The runner intentionally keeps the device UI actions and the memory evidence
in one auditable session.  Every measured stage follows the same order:

    before snapshot -> clear_refs -> UI operation -> settle -> after snapshot
    -> collect all WPS PID reports -> pull and hash every report

WPS uses an XComponent editor on this target, so the editor itself is driven
by coordinates and the saved-document/file-system checks are used as the
stronger success evidence.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable


BUNDLE = "cn.wps.office.hap"
ABILITY = "PromeAbility"
MODULE = "prome"
DEFAULT_DEVICE_DIR = "/data/local/tmp/harmony_collector"
DEFAULT_DEVICE_OUT_ROOT = "/data/local/tmp/harmony_collector/wps_reports"
DOCUMENT_ROOT = "/storage/media/100/local/files/Docs"
DESKTOP_ROOT = "/storage/media/100/local/files/Docs/Desktop"
EXPERIMENT_ROOT = f"{DOCUMENT_ROOT}/WPS_VMA_Experiments"
DOCUMENT_SCAN_ROOTS = (
    DOCUMENT_ROOT,
    "/storage/media/100/local/files/Download",
    "/storage/media/100/local/files/Documents",
)
DEFAULT_TEST_SERIAL = "WPS-TEST-0001"

# HarmonyOS key codes.  ``uitest uiInput keyEvent`` accepts up to three key
# values, so the dataset runner can use the same keyboard shortcuts as WPS.
KEY_HOME = "1"
KEY_ENTER = "2054"
KEY_DPAD_LEFT = "2014"
KEY_DPAD_RIGHT = "2015"
KEY_ESCAPE = "2070"
KEY_PAGE_DOWN = "2069"
KEY_TAB = "2049"
KEY_CTRL_LEFT = "2072"
KEY_SHIFT_LEFT = "2047"
KEY_A = "2017"
KEY_B = "2018"
KEY_C = "2019"
KEY_F = "2022"
KEY_H = "2024"
KEY_I = "2025"
KEY_N = "2030"
KEY_S = "2035"
KEY_U = "2037"
KEY_V = "2038"
KEY_W = "2039"
KEY_X = "2040"
KEY_Y = "2041"
KEY_Z = "2042"

SCREENSHOT_NAMES = (
    "01_open_wps",
    "02_new_word",
    "03_write_metadata",
    "04_heavy_edit_scroll",
    "05_save_document",
    "06_background",
    "07_foreground",
    "08_reopen_saved_document",
    "09_reopen_edit_scroll",
    "close_before_reopen",
    "close_final",
)


class HdcError(RuntimeError):
    """A failed host-to-device command."""


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def q(value: str) -> str:
    return shlex.quote(value)


def find_hdc() -> str:
    configured = os.environ.get("HDC", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return str(configured_path)
        if configured_path.is_dir():
            for name in ("hdc.exe", "hdc"):
                candidate = configured_path / name
                if candidate.is_file():
                    return str(candidate)

    value = shutil.which("hdc")
    if value:
        return value
    candidates = (
        Path.home() / "Library/OpenHarmony/Sdk/23/toolchains/hdc",
        Path("/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise HdcError("找不到 hdc；请先 将 hdc 加入 PATH 或设置 HDC")


def run_host(command: list[str], *, check: bool = True, timeout_s: float = 180.0) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise HdcError(f"命令超时: {' '.join(command)}") from exc
    output = result.stdout or ""
    if check and result.returncode != 0:
        raise HdcError(f"命令失败({result.returncode}): {' '.join(command)}\n{output.strip()}")
    return result


def list_targets(hdc: str) -> list[str]:
    result = run_host([hdc, "list", "targets"])
    targets: list[str] = []
    for line in (result.stdout or "").splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token and not token.startswith("[") and token not in targets:
            targets.append(token)
    return targets


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Device:
    def __init__(self, hdc: str, target: str):
        self.hdc = hdc
        self.target = target

    def run(self, *args: str, check: bool = True, timeout_s: float = 180.0) -> str:
        result = run_host([self.hdc, "-t", self.target, *args], check=False, timeout_s=timeout_s)
        output = (result.stdout or "").strip()
        lower = output.lower()
        remote_error = any(
            marker in lower
            for marker in ("error code:", "failed to start ability", "[fail]", "no such file")
        )
        if check and (result.returncode != 0 or remote_error):
            raise HdcError(f"设备命令失败({result.returncode}): {output}")
        return output

    def shell(self, command: str, *, check: bool = True, timeout_s: float = 180.0) -> str:
        return self.run("shell", command, check=check, timeout_s=timeout_s)

    def send(self, local: Path, remote: str) -> str:
        return self.run("file", "send", str(local), remote)

    def recv(self, remote: str, local: Path) -> str:
        local.parent.mkdir(parents=True, exist_ok=True)
        return self.run("file", "recv", remote, str(local))


def process_snapshot(device: Device) -> list[dict[str, str]]:
    output = device.shell("ps -A -o PID,PPID,UID,VSZ,RSS,ARGS", check=False)
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        fields = line.strip().split(maxsplit=5)
        if len(fields) < 6 or BUNDLE not in fields[5]:
            continue
        rows.append(
            {
                "pid": fields[0],
                "ppid": fields[1],
                "uid": fields[2],
                "vsz_kb": fields[3],
                "rss_kb": fields[4],
                "args": fields[5],
            }
        )
    return rows


def parse_device_documents(output: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for line in output.splitlines():
        path, sep, rest = line.partition("|")
        if not sep:
            continue
        size_text, sep, mtime = rest.partition("|")
        if not sep or not path.lower().endswith((".docx", ".doc", ".wps")):
            continue
        try:
            size = int(size_text)
        except ValueError:
            continue
        documents.append({"path": path, "size_bytes": size, "mtime": mtime})
    return documents


def phase_pids(rows: list[dict[str, str]]) -> list[str]:
    return [row["pid"] for row in rows]


def parse_report_metrics(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")

    def number(pattern: str) -> int:
        match = re.search(pattern, text, re.MULTILINE)
        return int(match.group(1).replace(",", "")) if match else 0

    pid_match = re.search(r"\|\s*PID\s*\|\s*`?(\d+)", text)
    name_match = re.search(r"\|\s*进程名\s*\|\s*`?([^|`\n]+)", text)
    return {
        "pid": pid_match.group(1) if pid_match else "",
        "process_name": name_match.group(1).strip() if name_match else "",
        "rss_kib": number(r"\|\s*Rss\s*\|[^\n]*?([\d,]+)\s*KiB"),
        "pss_kib": number(r"\|\s*Pss\s*\|[^\n]*?([\d,]+)\s*KiB"),
        "referenced_kib": number(r"\|\s*Referenced\s*\|[^\n]*?([\d,]+)\s*KiB"),
        "swap_kib": number(r"\|\s*Swap\s*\|[^\n]*?([\d,]+)\s*KiB"),
    }


class Session:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.hdc = find_hdc()
        targets = list_targets(self.hdc)
        if args.target:
            if args.target not in targets:
                raise HdcError(f"指定设备不在线: {args.target}; 当前设备: {targets}")
            target = args.target
        elif len(targets) == 1:
            target = targets[0]
        elif not targets:
            raise HdcError("hdc list targets 为空")
        else:
            raise HdcError(f"检测到多个设备，请用 --target 指定: {targets}")
        self.device = Device(self.hdc, target)
        self.session_timestamp = args.session_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"wps_v6_{self.session_timestamp}" if not self.session_timestamp.startswith("wps_v6_") else self.session_timestamp
        self.experiment_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", self.session_timestamp)
        self.experiment_dir = f"{EXPERIMENT_ROOT}/WPS_VMA_Experiment_{self.experiment_slug}"
        self.experiment_file_name = f"WPS_VMA_{self.experiment_slug}.docx"
        self.script_dir = Path(__file__).resolve().parent
        self.local_out = Path(args.out).expanduser() if args.out else self.script_dir / "hdc_out" / f"wps_session_{self.session_id}"
        self.local_out.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir = self.local_out / "screenshots"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.device_out = f"{args.device_out.rstrip('/')}/wps_session_{self.session_id}"
        self.device_screenshot_out = f"{args.device_dir.rstrip('/')}/wps_screenshots/{self.session_id}"
        self.device_dir = args.device_dir.rstrip("/")
        self.device_bin = f"{self.device_dir}/collector"
        self.operations_path = self.local_out / "operations.csv"
        self.operations_file = self.operations_path.open("w", encoding="utf-8", newline="")
        self.operation_fields = [
            "session_id", "index", "stage", "label", "operation", "success", "status",
            "started_at", "ended_at", "before_pids", "after_pids",
            "operation_started_at", "operation_ended_at", "operation_elapsed_s",
            "settle_wait_s", "observation_wait_s", "clear_refs_elapsed_s",
            "collection_started_at", "collection_ended_at", "collection_elapsed_s",
            "report_pull_started_at", "report_pull_ended_at", "report_pull_elapsed_s",
            "stage_total_elapsed_s", "phase_elapsed_s", "report_count", "report",
            "device_report_count", "local_report_count", "matched_report_count",
            "hash_mismatch_count", "missing_local_count", "missing_device_count",
            "document_path", "document_size_bytes", "document_mtime", "error",
        ]
        self.operations = csv.DictWriter(self.operations_file, fieldnames=self.operation_fields)
        self.operations.writeheader()
        self.operations_file.flush()
        self.hash_path = self.local_out / "report_hashes.csv"
        self.hash_file = self.hash_path.open("w", encoding="utf-8", newline="")
        self.hashes = csv.DictWriter(
            self.hash_file,
            fieldnames=["report", "device_path", "local_path", "device_sha256", "local_sha256", "match"],
        )
        self.hashes.writeheader()
        self.hash_file.flush()
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.timing_records: list[dict[str, Any]] = []
        self.report_records: list[dict[str, Any]] = []
        self.device_report_hashes: dict[str, str] = {}
        self.local_report_hashes: dict[str, str] = {}
        self.screenshots: list[str] = []
        self.collector_local: Path | None = None
        self.document_baseline: dict[str, tuple[int, str]] = {}
        self.saved_document: dict[str, Any] | None = None
        self.reopen_verified = False
        self.normal_close_before_reopen = False
        self.final_close_success = False
        self.force_stop_used = False
        self.started_at = now_iso()

    def close_files(self) -> None:
        if not self.operations_file.closed:
            self.operations_file.flush()
            self.operations_file.close()
        if not self.hash_file.closed:
            self.hash_file.flush()
            self.hash_file.close()

    def verify_access(self) -> None:
        identity = self.device.shell("id")
        if "uid=0" not in identity and "uid:0" not in identity:
            raise HdcError(f"hdc shell 不是 root: {identity}")
        self.device.shell(
            "test -r /proc/1/maps -a -r /proc/1/smaps -a -r /proc/1/pagemap "
            "-a -w /proc/self/clear_refs"
        )
        self.device.shell(
            f"mkdir -p {q(self.device_dir)} {q(self.device_out)} {q(self.device_screenshot_out)} "
            f"&& test -d {q(self.device_dir)} -a -d {q(self.device_out)}"
        )

    def build_and_push(self) -> None:
        from collector.build import build
        project = Path(__file__).resolve().parents[2]
        local_bin = Path(getattr(self.args, "collector_binary", None) or project / "build/collector")
        if not self.args.no_build:
            sdk = os.environ.get("OHOS_SDK")
            if not sdk:
                raise HdcError("Set OHOS_SDK or build the collector and pass --no-build --collector-binary")
            local_bin = build(sdk, local_bin)
        if not local_bin.is_file():
            raise HdcError(f"采集器不存在: {local_bin}")
        self.collector_local = local_bin
        self.device.send(local_bin, self.device_bin)
        self.device.shell(f"chmod 755 {q(self.device_bin)}")

    def clear_refs(self) -> None:
        self.device.shell(f"{q(self.device_bin)} --clear-refs --app {q(BUNDLE)}")

    def list_documents(self) -> list[dict[str, Any]]:
        scan_roots = " ".join(q(path) for path in DOCUMENT_SCAN_ROOTS)
        command = (
            f"find {scan_roots} -type f "
            "\\( -iname '*.docx' -o -iname '*.doc' -o -iname '*.wps' \\) "
            "2>/dev/null | while read -r f; do "
            "stat -c '%n|%s|%y' \"$f\"; done"
        )
        return parse_device_documents(self.device.shell(command, check=False))

    def record_document_baseline(self) -> None:
        self.document_baseline = {
            item["path"]: (item["size_bytes"], item["mtime"])
            for item in self.list_documents()
        }

    def find_saved_document(self, *, wait_s: float = 0.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + wait_s
        while True:
            documents = self.list_documents()
            candidates = [
                item for item in documents
                if item["size_bytes"] > 0
                and (
                    item["path"] not in self.document_baseline
                    or (item["size_bytes"], item["mtime"]) != self.document_baseline[item["path"]]
                )
            ]
            candidates.sort(key=lambda item: (item["mtime"], item["size_bytes"]), reverse=True)
            if candidates:
                return candidates[0]
            if time.monotonic() >= deadline:
                return None
            time.sleep(1.0)

    def verify_document_content(self, path: str) -> dict[str, bool]:
        """Verify the fixed serial and required text markers without images."""
        with tempfile.TemporaryDirectory(prefix="wps-doc-verify-") as temp_dir:
            local = Path(temp_dir) / Path(path).name
            self.device.recv(path, local)
            try:
                with zipfile.ZipFile(local) as archive:
                    xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
            except (KeyError, zipfile.BadZipFile) as exc:
                raise HdcError(f"保存文件不是可读取的 docx: {path}") from exc
        markers = {
            "test_serial": self.args.test_serial in xml,
            "exact_time": "Exact_time" in xml,
            "purpose": "Purpose" in xml,
            "operation_chain": "Operation_chain" in xml,
            "preliminary_conclusion": "Preliminary_conclusion" in xml,
        }
        if not all(markers.values()):
            missing = [key for key, present in markers.items() if not present]
            raise HdcError(f"Word 正文缺少自动化写入标记: {missing}")
        return markers

    def rename_saved_document(self) -> dict[str, Any]:
        if not self.saved_document:
            raise HdcError("没有可重命名的已保存文档")
        source = str(self.saved_document["path"])
        final_name = f"WPS_memory_test_{self.session_timestamp}.docx"
        destination = f"{DESKTOP_ROOT}/{final_name}"
        if source != destination:
            self.device.shell(f"mv {q(source)} {q(destination)}")
        verified = next((item for item in self.list_documents() if item["path"] == destination), None)
        if not verified or verified["size_bytes"] <= 0:
            raise HdcError(f"重命名后未验证文档: {destination}")
        self.saved_document = {
            **verified,
            "original_path": source,
            "final_path": destination,
            "file_name": final_name,
        }
        return self.saved_document

    def capture_screen(self, label: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label)
        remote = f"{self.device_screenshot_out}/{safe}.png"
        local = self.screenshot_dir / f"{safe}.png"
        self.device.shell(f"mkdir -p {q(self.device_screenshot_out)}")
        self.device.shell(f"uitest screenCap -p {q(remote)}", check=False)
        try:
            self.device.recv(remote, local)
            if local.is_file() and local.stat().st_size:
                self.screenshots.append(str(local))
        except HdcError:
            # Screenshots are automatic evidence only.  No stage depends on
            # an AI/visual inspection or becomes failed because a screenshot
            # transfer is unavailable.
            self.warnings.append(f"截图未拉回: {label}")

    def sample(self, index: int, stage: str) -> dict[str, Any]:
        collection_started_at = now_iso()
        collection_started = time.perf_counter()
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_stage = re.sub(r"[^A-Za-z0-9_-]+", "_", stage)
        remote_base = f"{self.device_out}/referenced_{index:02d}_{safe_stage}_{stamp}.md"
        self.device.shell(f"mkdir -p {q(self.device_out)}")
        collector_started = time.perf_counter()
        output = self.device.shell(
            f"{q(self.device_bin)} --app {q(BUNDLE)} -o {q(remote_base)} --with-vma",
            timeout_s=300.0,
        )
        collector_elapsed_s = time.perf_counter() - collector_started
        remote_reports: list[str] = []
        for line in output.splitlines():
            match = re.search(r"报告已写入:\s*(\S+)$", line)
            if match and match.group(1) not in remote_reports:
                remote_reports.append(match.group(1))
        if not remote_reports:
            raise HdcError(f"采集器没有返回报告路径: {output}")

        pull_started_at = now_iso()
        pull_started = time.perf_counter()
        local_reports: list[str] = []
        window_hash_mismatch_count = 0
        for remote_report in remote_reports:
            remote_hash = self.device.shell(f"sha256sum {q(remote_report)} | cut -d ' ' -f 1")
            local = self.local_out / Path(remote_report).name
            self.device.recv(remote_report, local)
            if not local.is_file() or not local.stat().st_size:
                raise HdcError(f"报告未拉回或为空: {local}")
            local_hash = sha256_file(local)
            match = remote_hash.strip() == local_hash
            name = local.name
            self.device_report_hashes[name] = remote_hash.strip()
            self.local_report_hashes[name] = local_hash
            self.hashes.writerow({
                "report": name,
                "device_path": remote_report,
                "local_path": str(local),
                "device_sha256": remote_hash.strip(),
                "local_sha256": local_hash,
                "match": str(match).lower(),
            })
            if not match:
                window_hash_mismatch_count += 1
            local_reports.append(str(local))
            metrics = parse_report_metrics(local)
            self.report_records.append({
                "stage": stage,
                "index": index,
                "report": str(local),
                **metrics,
            })
        self.hash_file.flush()
        pull_elapsed_s = time.perf_counter() - pull_started
        collection_elapsed_s = time.perf_counter() - collection_started
        return {
            "report": ";".join(local_reports),
            "report_count": len(local_reports),
            "collection_started_at": collection_started_at,
            "collection_ended_at": now_iso(),
            "collection_elapsed_s": collection_elapsed_s,
            "collector_elapsed_s": collector_elapsed_s,
            "report_pull_started_at": pull_started_at,
            "report_pull_ended_at": now_iso(),
            "report_pull_elapsed_s": pull_elapsed_s,
            "device_report_count": len(remote_reports),
            "local_report_count": len(local_reports),
            "matched_report_count": sum(
                self.device_report_hashes[Path(remote).name] == self.local_report_hashes[Path(remote).name]
                for remote in local_reports
            ),
            "hash_mismatch_count": window_hash_mismatch_count,
        }

    def start_wps(self) -> None:
        self.device.shell(f"aa start -a {q(ABILITY)} -b {q(BUNDLE)} -m {q(MODULE)}")

    def force_stop_wps(self) -> None:
        self.device.shell(f"aa force-stop {q(BUNDLE)}", check=False)

    def ui_key(self, key: str) -> None:
        self.device.shell(f"uitest uiInput keyEvent {q(key)}")

    def ui_key_chord(self, *keys: str) -> None:
        """Inject one HarmonyOS key combination, e.g. Ctrl+A or Ctrl+F."""
        if not 2 <= len(keys) <= 3:
            raise ValueError("ui_key_chord requires two or three key codes")
        self.device.shell(
            "uitest uiInput keyEvent " + " ".join(q(key) for key in keys)
        )

    def ui_click(self, x: int, y: int) -> None:
        self.device.shell(f"uitest uiInput click {x} {y}")

    def set_save_filename(self) -> None:
        """Replace WPS's reused default name with this trial's unique name."""
        # Native WPS save picker filename field on the 3120x2080 target.
        # The field is above the file-type selector; y=1585 lands on the
        # selector and leaves WPS's reused filename unchanged.
        self.ui_click(1800, 1500)
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_A)
        self.ui_text_payload(self.experiment_file_name)

    def ui_text_payload(self, text: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.:/+-]+", "_", text)
        if safe:
            self.device.shell(f"uitest uiInput text x{safe}", timeout_s=120.0)

    def ui_text(self, text: str) -> None:
        """Type safe ASCII lines and use Enter for real paragraph breaks.

        On this target ``hdc shell`` passes quote characters through to
        ``uitest uiInput text`` and embedded newlines are not reliable.  The
        old quoted/chunked implementation consequently produced a one-line
        ``vvvv...`` document.  Prefixing a safe payload and sending Enter as a
        separate key event avoids that parser/IME path while keeping the
        actual document content auditable in screenshots and the saved file.
        """
        lines = text.splitlines() or [text]
        for line in lines:
            safe = re.sub(r"[^A-Za-z0-9_.:/+-]+", "_", line)
            if safe:
                for offset in range(0, len(safe), 1200):
                    chunk = safe[offset : offset + 1200]
                    # The first character of a direct uiInput text payload is
                    # dropped by this HarmonyOS build for some characters.
                    self.ui_text_payload(chunk)
            self.ui_key(KEY_ENTER)

    def write_dataset_text(self, text: str) -> dict[str, object]:
        """Write one controlled ASCII payload for the dataset collector."""
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_text(text)
        return {
            "action_count": 1,
            "payload_length": len(text),
        }

    def select_all(self) -> dict[str, object]:
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_A)
        time.sleep(0.5)
        return {"action_count": 1, "shortcut": "CTRL+A"}

    def copy_selection(self) -> dict[str, object]:
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_C)
        time.sleep(0.5)
        return {"action_count": 1, "shortcut": "CTRL+C"}

    def cut_selection(self) -> dict[str, object]:
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_X)
        time.sleep(0.5)
        return {"action_count": 1, "shortcut": "CTRL+X"}

    def paste_selection(self) -> dict[str, object]:
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_V)
        time.sleep(0.5)
        return {"action_count": 1, "shortcut": "CTRL+V"}

    def undo_edit(self) -> dict[str, object]:
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_Z)
        time.sleep(0.5)
        return {"action_count": 1, "shortcut": "CTRL+Z"}

    def redo_edit(self) -> dict[str, object]:
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_Y)
        time.sleep(0.5)
        return {"action_count": 1, "shortcut": "CTRL+Y"}

    def find_text(self, text: str = "WPS_FIND_TARGET") -> dict[str, object]:
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_F)
        time.sleep(1.0)
        self.ui_text_payload(text)
        self.ui_key(KEY_ENTER)
        time.sleep(0.5)
        self.ui_key(KEY_ESCAPE)
        return {"action_count": 1, "shortcut": "CTRL+F", "query": text}

    def replace_text(
        self,
        find_text: str = "WPS_FIND_TARGET",
        replacement: str = "WPS_REPLACED_TARGET",
    ) -> dict[str, object]:
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_H)
        time.sleep(1.0)
        # WPS opens the replace panel with the find field focused.  Tab moves
        # to the replacement field on this editor build; Escape closes the
        # panel after the replacement command has been issued.
        self.ui_text_payload(find_text)
        self.ui_key(KEY_TAB)
        self.ui_text_payload(replacement)
        self.ui_key(KEY_ENTER)
        time.sleep(0.8)
        self.ui_key(KEY_ESCAPE)
        return {
            "action_count": 1,
            "shortcut": "CTRL+H",
            "find": find_text,
            "replacement": replacement,
        }

    def insert_page_break(self) -> dict[str, object]:
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_ENTER)
        time.sleep(0.8)
        return {"action_count": 1, "shortcut": "CTRL+ENTER"}

    def insert_table(self) -> dict[str, object]:
        """Insert a small 2x2 table through WPS's Insert > Table menu."""
        # Native 3120x2080 coordinates for this device/WPS layout.  The
        # table grid is intentionally selected at 2x2 to keep the document
        # small enough for long repeated collection.
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_click(1455, 555)  # Insert tab
        time.sleep(0.6)
        self.ui_click(1210, 640)  # Table dropdown
        time.sleep(0.6)
        self.ui_click(1435, 155)  # second column, second row
        time.sleep(1.5)
        return {"action_count": 1, "table_rows": 2, "table_columns": 2}

    def format_bold(self) -> dict[str, object]:
        self.ui_click(1360, 555)  # Home tab
        time.sleep(0.4)
        self.ui_click(960, 700)
        time.sleep(0.5)
        return {"action_count": 1, "toolbar": "bold"}

    def format_italic(self) -> dict[str, object]:
        self.ui_click(1360, 555)  # Home tab
        time.sleep(0.4)
        self.ui_click(1020, 700)
        time.sleep(0.5)
        return {"action_count": 1, "toolbar": "italic"}

    def format_underline(self) -> dict[str, object]:
        self.ui_click(1360, 555)  # Home tab
        time.sleep(0.4)
        self.ui_click(1080, 700)
        time.sleep(0.5)
        return {"action_count": 1, "toolbar": "underline"}

    def align_center(self) -> dict[str, object]:
        self.ui_click(1360, 555)  # Home tab
        time.sleep(0.4)
        self.ui_click(1735, 700)
        time.sleep(0.5)
        return {"action_count": 1, "toolbar": "align_center"}

    def ui_swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.device.shell(f"uitest uiInput swipe {x1} {y1} {x2} {y2} 800")

    def snapshot(self) -> list[dict[str, str]]:
        return process_snapshot(self.device)

    def metadata_text(self) -> str:
        timestamp = now_iso()
        return (
            "WPS_HarmonyOS_PC_memory_collection\n"
            f"Test_serial_{self.args.test_serial}\n"
            f"Exact_time_{timestamp}\n"
            "Purpose_measure_WPS_related_process_RSS_PSS_Referenced_and_Swap_during_a_real_Word_workflow\n"
            "Operation_chain_open_WPS_new_Word_write_metadata_heavy_edit_line_breaks_page_scroll_cursor_move_save_background_foreground_close_reopen_saved_document_reopen_edit_scroll_close\n"
            "Preliminary_conclusion_profiling_evidence_only_compare_stage_aggregates_and_do_not_infer_reclamation_from_Referenced_alone\n"
        )

    def new_word(self) -> None:
        # Device coordinates are logical 3120x2080 coordinates, not the
        # resized screenshot dimensions shown by the desktop app.
        self.ui_click(1075, 720)
        time.sleep(2)
        self.ui_click(1485, 995)
        time.sleep(4)
        self.ui_click(1735, 1045)
        time.sleep(5)

    def write_metadata(self) -> None:
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_text(self.metadata_text())

    def heavy_edit_scroll(self) -> None:
        self.ui_click(self.args.editor_x, self.args.editor_y)
        block = (
            "WPS_memory_profiling_stress_paragraph_repeated_text_creates_a_multi_page_Word_document_"
            f"for_observing_layout_rendering_cache_and_process_memory_behavior_test_serial_{self.args.test_serial}_controlled_workload_"
        )
        payload = block * self.args.heavy_repeats
        # Keep the requested repeat volume but avoid one HDC/IME round-trip per
        # paragraph.  Ten-ish large payloads plus real Enter events create
        # multiple paragraphs while keeping the device responsive.
        for offset in range(0, len(payload), 1200):
            self.ui_text_payload(payload[offset : offset + 1200])
            self.ui_key(KEY_ENTER)
        # Multiple explicit line breaks and navigation gestures keep the
        # XComponent active while allowing layout/scroll work to settle.
        for _ in range(8):
            self.ui_text("\n")
        for _ in range(8):
            self.ui_swipe(self.args.editor_x, 1500, self.args.editor_x, 620)
        for _ in range(4):
            self.ui_swipe(self.args.editor_x, 620, self.args.editor_x, 1500)
        self.ui_click(self.args.editor_x - 120, self.args.editor_y - 80)
        self.ui_key(KEY_DPAD_LEFT)
        self.ui_key(KEY_DPAD_RIGHT)
        self.ui_click(self.args.editor_x + 120, self.args.editor_y + 80)

    def save_document(self, *, verify_content: bool = True) -> dict[str, Any]:
        before = {item["path"]: (item["size_bytes"], item["mtime"]) for item in self.list_documents()}
        self.document_baseline = before
        self.device.shell(f"mkdir -p {q(self.experiment_dir)}")
        self.capture_screen("05_save_before")
        # Toolbar save icon on the current WPS editor window.  On this
        # 3120x2080 display it is x≈1055; x≈850 is the auto-save toggle.
        self.ui_click(1055, 555)
        time.sleep(4)
        # Choose the user-visible Desktop in the WPS save picker.  A first
        # access may show a HarmonyOS permission dialog; allow it and retry.
        self.set_save_filename()
        self.ui_click(650, 842)
        time.sleep(2)
        self.ui_click(2300, 1665)
        time.sleep(4)
        self.ui_click(1885, 1275)
        time.sleep(2)
        # If the permission dialog was absent, this is harmless only when it
        # lands outside the app; retrying Save is the useful action.
        self.ui_click(2300, 1665)
        saved = self.find_saved_document(wait_s=8.0)
        if not saved:
            # Some WPS builds do not activate the toolbar Save button after a
            # formatting/menu operation.  Reset the picker/editor state and
            # retry through the standard Ctrl+S shortcut.
            self.ui_key(KEY_ESCAPE)
            time.sleep(1)
            self.ui_click(self.args.editor_x, self.args.editor_y)
            self.ui_key_chord(KEY_CTRL_LEFT, KEY_S)
            time.sleep(4)
            self.set_save_filename()
            self.ui_click(650, 842)
            time.sleep(2)
            self.ui_click(2300, 1665)
            time.sleep(4)
            self.ui_click(1885, 1275)
            time.sleep(2)
            self.ui_click(2300, 1665)
            saved = self.find_saved_document(wait_s=20.0)
        if not saved:
            self.capture_screen("05_save_failed")
            current = self.list_documents()
            current_text = "; ".join(
                f"{item['path']} ({item['size_bytes']} bytes)" for item in current[-8:]
            ) or "(none)"
            raise HdcError(
                "保存操作后未发现新增或变化的文档；已扫描 Docs/Download/Documents，"
                f"当前文档={current_text}"
            )
        source_path = saved["path"]
        final_path = f"{self.experiment_dir}/{self.experiment_file_name}"
        self.device.shell(f"mkdir -p {q(self.experiment_dir)} && mv {q(source_path)} {q(final_path)}")
        moved = next((item for item in self.list_documents() if item["path"] == final_path), None)
        if not moved or moved["size_bytes"] <= 0:
            raise HdcError(f"文档已保存但归档到实验文件夹失败: {final_path}")
        saved = moved
        content_markers = self.verify_document_content(saved["path"]) if verify_content else {}
        self.saved_document = {
            **saved,
            "original_path": source_path,
            "final_path": final_path,
            "file_name": Path(saved["path"]).name,
            "content_markers_verified": content_markers,
        }
        self.capture_screen("05_save_after")
        return self.saved_document

    def save_existing_document(self) -> dict[str, object]:
        """Save an already-created document without opening Save As."""
        if not self.saved_document:
            raise HdcError("普通保存前没有已保存文档记录")
        path = str(self.saved_document.get("final_path") or self.saved_document.get("path") or "")
        before = next((item for item in self.list_documents() if item["path"] == path), None)
        self.ui_key_chord(KEY_CTRL_LEFT, KEY_S)
        time.sleep(5)
        after = next((item for item in self.list_documents() if item["path"] == path), None)
        if after:
            self.saved_document.update(after)
        return {
            "action_count": 1,
            "document_path": path,
            "before_size_bytes": before.get("size_bytes", 0) if before else 0,
            "after_size_bytes": after.get("size_bytes", 0) if after else 0,
        }

    def close_document(self) -> dict[str, object]:
        """Close the current document tab while keeping WPS alive."""
        before = self.snapshot()
        self.ui_click(1695, 478)
        time.sleep(5)
        after = self.snapshot()
        if not after:
            raise HdcError("关闭文档后 WPS 相关进程已退出")
        return {
            "action_count": 1,
            "before_process_count": len(before),
            "after_process_count": len(after),
            "wps_process_alive": bool(after),
        }

    def open_saved_document(self, *, start_wps: bool = True) -> None:
        if not self.saved_document:
            raise HdcError("重新打开前没有已保存文档记录")
        final_path = str(self.saved_document.get("final_path") or self.saved_document["path"])
        if not final_path.startswith(DESKTOP_ROOT + "/"):
            raise HdcError(f"保存文件不在预期的 Desktop 目录，无法用固定文件选择器重开: {final_path}")
        if start_wps:
            self.start_wps()
            time.sleep(self.args.launch_wait_s)
        self.ui_click(1050, 810)
        time.sleep(3)
        # Open picker: double-click the main-pane Desktop row, then the most
        # recently modified document row.  The formal document is newer than
        # preserved older files and therefore appears immediately after the
        # Desktop folder in this picker.
        self.ui_click(1750, 625)
        self.ui_click(1750, 625)
        time.sleep(2)
        self.capture_screen("08_reopen_picker")
        self.ui_click(1600, 683)
        self.ui_click(1600, 683)
        time.sleep(8)
        self.capture_screen("08_reopen_after")
        if not self.snapshot():
            raise HdcError("双击保存文件后未发现 WPS 进程")
        # The path and non-empty file were already verified on-device; this
        # successful picker action plus an editor screenshot is the reopen
        # evidence, since the editor is an XComponent with no readable body.
        self.reopen_verified = True

    def reopen_edit_scroll(self) -> None:
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_text("Reopen verification edit marker.\n")
        for _ in range(4):
            self.ui_swipe(self.args.editor_x, 1450, self.args.editor_x, 650)
        for _ in range(2):
            self.ui_swipe(self.args.editor_x, 650, self.args.editor_x, 1450)
        self.ui_click(self.args.editor_x, self.args.editor_y)
        self.ui_key(KEY_DPAD_LEFT)
        self.ui_key(KEY_DPAD_RIGHT)

    def write_operation(self, record: dict[str, Any]) -> None:
        row = {field: "" for field in self.operation_fields}
        row.update({
            "session_id": self.session_id,
            "index": record["index"],
            "stage": record["stage"],
            "label": record["label"],
            "operation": record["operation"],
            "success": str(bool(record["success"])).lower(),
            "status": "success" if record["success"] else "failed",
            "started_at": record["started_at"],
            "ended_at": record["ended_at"],
            "before_pids": json.dumps(record.get("before", []), ensure_ascii=False),
            "after_pids": json.dumps(record.get("after", []), ensure_ascii=False),
            "operation_started_at": record.get("operation_started_at", ""),
            "operation_ended_at": record.get("operation_ended_at", ""),
            "operation_elapsed_s": f"{record.get('operation_elapsed_s', 0.0):.6f}",
            "settle_wait_s": f"{record.get('settle_wait_s', 0.0):.6f}",
            "observation_wait_s": f"{record.get('settle_wait_s', 0.0):.6f}",
            "clear_refs_elapsed_s": f"{record.get('clear_refs_elapsed_s', 0.0):.6f}",
            "collection_started_at": record.get("collection_started_at", ""),
            "collection_ended_at": record.get("collection_ended_at", ""),
            "collection_elapsed_s": f"{record.get('collection_elapsed_s', 0.0):.6f}",
            "report_pull_started_at": record.get("report_pull_started_at", ""),
            "report_pull_ended_at": record.get("report_pull_ended_at", ""),
            "report_pull_elapsed_s": f"{record.get('report_pull_elapsed_s', 0.0):.6f}",
            "stage_total_elapsed_s": f"{record.get('stage_total_elapsed_s', 0.0):.6f}",
            "phase_elapsed_s": f"{record.get('stage_total_elapsed_s', 0.0):.6f}",
            "report_count": record.get("report_count", 0),
            "report": record.get("report", ""),
            "device_report_count": record.get("device_report_count", 0),
            "local_report_count": record.get("local_report_count", 0),
            "matched_report_count": record.get("matched_report_count", 0),
            "hash_mismatch_count": record.get("hash_mismatch_count", 0),
            "missing_local_count": record.get("missing_local_count", 0),
            "missing_device_count": record.get("missing_device_count", 0),
            "document_path": record.get("document_path", ""),
            "document_size_bytes": record.get("document_size_bytes", ""),
            "document_mtime": record.get("document_mtime", ""),
            "error": record.get("error", ""),
        })
        self.operations.writerow(row)
        self.operations_file.flush()
        self.timing_records.append({
            key: record.get(key, 0.0)
            for key in ("index", "stage", "label", "success", "operation_elapsed_s", "settle_wait_s", "clear_refs_elapsed_s", "collection_elapsed_s", "report_pull_elapsed_s", "stage_total_elapsed_s", "report_count")
        })

    def measured_stage(
        self,
        index: int,
        stage: str,
        label: str,
        operation: str,
        action: Callable[[], None],
        settle_wait_s: float,
    ) -> bool:
        stage_started = time.perf_counter()
        started_at = now_iso()
        before = self.snapshot()
        record: dict[str, Any] = {
            "index": index, "stage": stage, "label": label, "operation": operation,
            "success": False, "started_at": started_at, "before": before, "after": [],
            "settle_wait_s": settle_wait_s, "stage_total_elapsed_s": 0.0,
        }
        try:
            if not before:
                raise HdcError("操作前未发现 WPS 相关进程")
            clear_started = time.perf_counter()
            try:
                self.clear_refs()
            finally:
                record["clear_refs_elapsed_s"] = time.perf_counter() - clear_started
            record["operation_started_at"] = now_iso()
            operation_started = time.perf_counter()
            try:
                action()
            finally:
                record["operation_elapsed_s"] = time.perf_counter() - operation_started
                record["operation_ended_at"] = now_iso()
            settle_started = time.perf_counter()
            time.sleep(settle_wait_s)
            record["settle_wait_s"] = time.perf_counter() - settle_started
            record["after"] = self.snapshot()
            if not record["after"]:
                raise HdcError("操作后未发现 WPS 相关进程")
            sample = self.sample(index, stage)
            record.update(sample)
            record["document_path"] = (self.saved_document or {}).get("final_path", "")
            record["document_size_bytes"] = (self.saved_document or {}).get("size_bytes", "")
            record["document_mtime"] = (self.saved_document or {}).get("mtime", "")
            record["success"] = True
            print(
                f"[wps-v6] {stage}: success operation={record['operation_elapsed_s']:.3f}s "
                f"collection={record['collection_elapsed_s']:.3f}s reports={record['report_count']}",
                flush=True,
            )
            return True
        except (HdcError, OSError, subprocess.SubprocessError) as exc:
            message = str(exc)
            self.failures.append(f"{stage}: {message}")
            record["error"] = message
            record["after"] = self.snapshot()
            print(f"[wps-v6] {stage}: FAILED: {message}", file=sys.stderr, flush=True)
            return False
        finally:
            record["stage_total_elapsed_s"] = time.perf_counter() - stage_started
            record["ended_at"] = now_iso()
            self.write_operation(record)

    def open_stage(self, index: int) -> bool:
        stage = "01_open_wps"
        started = time.perf_counter()
        record: dict[str, Any] = {
            "index": index, "stage": stage, "label": "打开 WPS", "operation": "清理残留后打开 WPS",
            "success": False, "started_at": now_iso(), "before": self.snapshot(),
        }
        try:
            self.force_stop_wps()
            time.sleep(2)
            record["operation_started_at"] = now_iso()
            op_started = time.perf_counter()
            try:
                self.start_wps()
                time.sleep(self.args.launch_wait_s)
            finally:
                record["operation_elapsed_s"] = time.perf_counter() - op_started
                record["operation_ended_at"] = now_iso()
            if not self.snapshot():
                raise HdcError("打开 WPS 后未发现进程")
            # Launch has no pre-existing WPS process to clear.  We clear
            # immediately after launch and record this exception explicitly.
            clear_started = time.perf_counter()
            self.clear_refs()
            record["clear_refs_elapsed_s"] = time.perf_counter() - clear_started
            settle_started = time.perf_counter()
            time.sleep(3)
            record["settle_wait_s"] = time.perf_counter() - settle_started
            record["after"] = self.snapshot()
            sample = self.sample(index, stage)
            record.update(sample)
            record["success"] = True
            self.capture_screen(stage)
            print(
                f"[wps-v6] {stage}: success operation={record['operation_elapsed_s']:.3f}s "
                f"collection={record['collection_elapsed_s']:.3f}s reports={record['report_count']}",
                flush=True,
            )
            return True
        except (HdcError, OSError, subprocess.SubprocessError) as exc:
            message = str(exc)
            self.failures.append(f"{stage}: {message}")
            record["error"] = message
            record["after"] = self.snapshot()
            print(f"[wps-v6] {stage}: FAILED: {message}", file=sys.stderr, flush=True)
            return False
        finally:
            record["stage_total_elapsed_s"] = time.perf_counter() - started
            record["ended_at"] = now_iso()
            self.write_operation(record)

    def reopen_stage(self, index: int) -> bool:
        """Measure reopening from a deliberately closed WPS baseline.

        Unlike ordinary measured stages, the operation starts with no WPS
        process.  WPS is launched as the stage precondition, then clear_refs
        is applied before the actual file-picker/open action.
        """
        stage = "08_reopen_saved_document"
        started = time.perf_counter()
        record: dict[str, Any] = {
            "index": index, "stage": stage, "label": "重新打开已保存 Word",
            "operation": "启动 WPS 后打开文件选择器并双击最新保存文档",
            "success": False, "started_at": now_iso(), "before": self.snapshot(),
        }
        try:
            self.start_wps()
            time.sleep(self.args.launch_wait_s)
            if not self.snapshot():
                raise HdcError("重新打开阶段启动 WPS 后未发现进程")
            clear_started = time.perf_counter()
            self.clear_refs()
            record["clear_refs_elapsed_s"] = time.perf_counter() - clear_started
            record["operation_started_at"] = now_iso()
            operation_started = time.perf_counter()
            try:
                self.open_saved_document(start_wps=False)
            finally:
                record["operation_elapsed_s"] = time.perf_counter() - operation_started
                record["operation_ended_at"] = now_iso()
            settle_started = time.perf_counter()
            time.sleep(6)
            record["settle_wait_s"] = time.perf_counter() - settle_started
            record["after"] = self.snapshot()
            if not record["after"]:
                raise HdcError("重新打开文档后未发现 WPS 进程")
            sample = self.sample(index, stage)
            record.update(sample)
            record["success"] = True
            print(
                f"[wps-v6] {stage}: success operation={record['operation_elapsed_s']:.3f}s "
                f"collection={record['collection_elapsed_s']:.3f}s reports={record['report_count']}",
                flush=True,
            )
            return True
        except (HdcError, OSError, subprocess.SubprocessError) as exc:
            message = str(exc)
            self.failures.append(f"{stage}: {message}")
            record["error"] = message
            record["after"] = self.snapshot()
            print(f"[wps-v6] {stage}: FAILED: {message}", file=sys.stderr, flush=True)
            return False
        finally:
            record["stage_total_elapsed_s"] = time.perf_counter() - started
            record["ended_at"] = now_iso()
            self.write_operation(record)
            self.capture_screen(stage)

    def close_wps(self, index: int, label: str) -> bool:
        started = time.perf_counter()
        before = self.snapshot()
        record: dict[str, Any] = {
            "index": index, "stage": label, "label": label,
            "operation": "正常关闭文档和 WPS；必要时记录 force-stop 回退",
            "success": False, "started_at": now_iso(), "before": before,
        }
        op_started = time.perf_counter()
        try:
            # Close current document tab, then close the WPS window.  These
            # are native window coordinates already verified on this device.
            self.ui_click(1695, 478)
            time.sleep(2)
            self.ui_click(2675, 475)
            time.sleep(5)
            after = self.snapshot()
            if after:
                self.force_stop_used = True
                record["error"] = "正常关闭后仍有 WPS 进程，已按回退规则使用 aa force-stop"
                self.force_stop_wps()
                time.sleep(3)
                after = self.snapshot()
            record["after"] = after
            record["success"] = not after
            if after:
                raise HdcError("force-stop 后仍存在 WPS 相关进程")
            if label == "close_before_reopen":
                self.normal_close_before_reopen = not self.force_stop_used
            else:
                self.final_close_success = True
            return True
        except (HdcError, OSError, subprocess.SubprocessError) as exc:
            message = str(exc)
            record["error"] = message
            self.failures.append(f"{label}: {message}")
            record["after"] = self.snapshot()
            return False
        finally:
            record["operation_elapsed_s"] = time.perf_counter() - op_started
            record["operation_ended_at"] = now_iso()
            record["stage_total_elapsed_s"] = time.perf_counter() - started
            record["ended_at"] = now_iso()
            self.write_operation(record)
            self.capture_screen(label)
            print(
                f"[wps-v6] {label}: {'success' if record['success'] else 'failed'} "
                f"operation={record['operation_elapsed_s']:.3f}s force_stop={self.force_stop_used}",
                flush=True,
            )

    def write_memory_summary(self) -> None:
        path = self.local_out / "memory_summary.csv"
        fields = [
            "stage", "pid", "process_name", "rss_kib", "pss_kib", "referenced_kib", "swap_kib",
            "operation_elapsed_s", "collection_elapsed_s", "report",
        ]
        timings = {item.get("stage"): item for item in self.timing_records}
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in self.report_records:
                timing = timings.get(item["stage"], {})
                writer.writerow({
                    **{field: item.get(field, "") for field in fields},
                    "operation_elapsed_s": f"{timing.get('operation_elapsed_s', 0.0):.6f}",
                    "collection_elapsed_s": f"{timing.get('collection_elapsed_s', 0.0):.6f}",
                })

    def hash_summary(self) -> dict[str, int]:
        names = set(self.device_report_hashes) | set(self.local_report_hashes)
        matched = sum(
            name in self.device_report_hashes
            and name in self.local_report_hashes
            and self.device_report_hashes[name] == self.local_report_hashes[name]
            for name in names
        )
        return {
            "device_report_count": len(self.device_report_hashes),
            "local_report_count": len(self.local_report_hashes),
            "matched_report_count": matched,
            "hash_mismatch_count": sum(
                name in self.device_report_hashes
                and name in self.local_report_hashes
                and self.device_report_hashes[name] != self.local_report_hashes[name]
                for name in names
            ),
            "missing_local_count": len(set(self.device_report_hashes) - set(self.local_report_hashes)),
            "missing_device_count": len(set(self.local_report_hashes) - set(self.device_report_hashes)),
        }

    def write_experiment_summary(self) -> None:
        operations = []
        if self.operations_path.is_file():
            with self.operations_path.open(encoding="utf-8", newline="") as handle:
                operations = list(csv.DictReader(handle))
        by_stage: dict[str, list[dict[str, Any]]] = {}
        for item in self.report_records:
            by_stage.setdefault(item["stage"], []).append(item)
        lines = [
            f"# WPS HarmonyOS v6 正式实验总结 — `{self.session_id}`",
            "",
            f"- 设备：`{self.device.target}`；包名：`{BUNDLE}`；采集器：`mem_analyze-v6 --with-vma`。",
            f"- 自动化全部关键阶段成功：`{'是' if all(row.get('success') == 'true' for row in operations if row.get('stage','').startswith(('0','close'))) else '否'}`。",
            f"- Word 实际保存：`{'是' if self.saved_document else '否'}`。",
        ]
        if self.saved_document:
            lines.extend([
                f"- 保存文件真实路径：`{self.saved_document.get('final_path', self.saved_document.get('path',''))}`。",
                f"- 文件大小：`{self.saved_document.get('size_bytes', 0)} bytes`；设备修改时间：`{self.saved_document.get('mtime', '')}`。",
                f"- 重新打开验证：`{'是' if self.reopen_verified else '否'}`。原始保存路径：`{self.saved_document.get('original_path', '')}`。",
                f"- Word 正文固定测试序列号和时间/目的/操作链/初步结论标记：`{'是' if self.saved_document.get('content_markers_verified') else '否'}`。",
            ])
        lines.extend([
            f"- 关闭前正常关闭无 force-stop：`{'是' if self.normal_close_before_reopen else '否'}`；最终 WPS 已退出：`{'是' if self.final_close_success else '否'}`；force-stop 回退：`{'是' if self.force_stop_used else '否'}`。",
            "",
            "## 阶段耗时与聚合内存",
            "",
            "| stage | success | operation s | settle s | collection s | pull s | reports | RSS KiB | PSS KiB | Referenced KiB | Swap KiB |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        peak_stage = ""
        peak_rss = -1
        for row in operations:
            stage = row.get("stage", "")
            if not stage or stage.startswith("close"):
                continue
            metrics = by_stage.get(stage, [])
            sums = {key: sum(int(item.get(key, 0) or 0) for item in metrics) for key in ("rss_kib", "pss_kib", "referenced_kib", "swap_kib")}
            rss = sums["rss_kib"]
            if rss > peak_rss:
                peak_rss, peak_stage = rss, stage
            lines.append(
                f"| {stage} | {row.get('success','')} | {float(row.get('operation_elapsed_s') or 0):.3f} | "
                f"{float(row.get('settle_wait_s') or 0):.3f} | {float(row.get('collection_elapsed_s') or 0):.3f} | "
                f"{float(row.get('report_pull_elapsed_s') or 0):.3f} | {row.get('report_count','0')} | "
                f"{sums['rss_kib']} | {sums['pss_kib']} | {sums['referenced_kib']} | {sums['swap_kib']} |"
            )
        lines.extend([
            "",
            f"- RSS 聚合峰值阶段：`{peak_stage}`（{peak_rss} KiB）。",
            "- RSS/PSS/Referenced/Swap 是独立列，未将 Referenced 当作 RSS 增量；Referenced 表示 clear_refs 后观察窗口内访问到的驻留页。",
            "",
            "## 关键变化",
            "",
        ])
        stage_sums = {
            stage: {key: sum(int(item.get(key, 0) or 0) for item in items) for key in ("rss_kib", "pss_kib", "referenced_kib", "swap_kib")}
            for stage, items in by_stage.items()
        }
        for left, right, label in (
            ("03_write_metadata", "04_heavy_edit_scroll", "写入元数据到重编辑"),
            ("04_heavy_edit_scroll", "05_save_document", "重编辑到保存"),
            ("06_background", "07_foreground", "后台到前台"),
            ("07_foreground", "08_reopen_saved_document", "前台到重新打开"),
        ):
            if left in stage_sums and right in stage_sums:
                delta = {key: stage_sums[right][key] - stage_sums[left][key] for key in stage_sums[left]}
                lines.append(f"- {label}：RSS `{delta['rss_kib']:+d}` KiB；PSS `{delta['pss_kib']:+d}` KiB；Referenced `{delta['referenced_kib']:+d}` KiB；Swap `{delta['swap_kib']:+d}` KiB。")
        hashes = self.hash_summary()
        lines.extend([
            "",
            "## 报告一致性",
            "",
            f"`device_report_count={hashes['device_report_count']}`, `local_report_count={hashes['local_report_count']}`, `matched_report_count={hashes['matched_report_count']}`, `hash_mismatch_count={hashes['hash_mismatch_count']}`, `missing_local_count={hashes['missing_local_count']}`, `missing_device_count={hashes['missing_device_count']}`。",
            "",
            "## 失败或不确定项",
            "",
        ])
        if self.failures:
            lines.extend(f"- {failure}" for failure in self.failures)
        else:
            lines.append("- 未记录失败项；重新打开成功依据为设备文件已验证、文件选择器双击完成、WPS 进程仍存活并取得重新打开截图。")
        (self.local_out / "experiment_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_metadata_file(self, return_code: int) -> None:
        self.write_memory_summary()
        self.write_experiment_summary()
        hashes = self.hash_summary()
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "target": self.device.target,
            "bundle": BUNDLE,
            "ability": ABILITY,
            "collector": "collect.c",
            "collector_semantics": "clear_refs -> operation -> smaps/Referenced",
            "started_at": self.started_at,
            "finished_at": now_iso(),
            "return_code": return_code,
            "failures": self.failures,
            "warnings": self.warnings,
            "local_output": str(self.local_out),
            "device_output": self.device_out,
            "device_screenshot_output": self.device_screenshot_out,
            "screenshots": self.screenshots,
            "document": self.saved_document,
            "document_saved": bool(self.saved_document),
            "reopen_verified": self.reopen_verified,
            "normal_close_before_reopen": self.normal_close_before_reopen,
            "final_close_success": self.final_close_success,
            "force_stop_used": self.force_stop_used,
            "report_hash_verification": hashes,
            "timing": self.timing_records,
            "timing_totals": {
                "stage_total_elapsed_s": round(sum(float(item.get("stage_total_elapsed_s", 0.0) or 0.0) for item in self.timing_records), 6),
                "operation_elapsed_s": round(sum(float(item.get("operation_elapsed_s", 0.0) or 0.0) for item in self.timing_records), 6),
                "collection_elapsed_s": round(sum(float(item.get("collection_elapsed_s", 0.0) or 0.0) for item in self.timing_records), 6),
                "report_pull_elapsed_s": round(sum(float(item.get("report_pull_elapsed_s", 0.0) or 0.0) for item in self.timing_records), 6),
            },
            "notes": [
                "01_open_wps 在没有 WPS 进程的干净起点启动后才 clear_refs；其余测量阶段严格按 before -> clear_refs -> operation -> settle -> collection 执行。",
                "Referenced 只覆盖 clear_refs 后到 smaps 读取之间访问过的驻留页；RSS、PSS、Referenced、Swap 分列聚合。",
                "collection_elapsed_s 包含设备端报告生成和全部报告拉回；report_pull_elapsed_s 单独记录 HDC 拉回耗时。",
                "WPS 编辑区是 XComponent，uitest 不能读取正文；保存文件、设备路径/大小/mtime、重新打开截图和最终进程状态作为验证证据。",
                f"Word 固定测试序列号：{self.args.test_serial}；截图仅由脚本自动保存，不参与成功判定。",
                "报告在拉回前计算设备端 SHA-256，拉回后计算本机 SHA-256；report_hashes.csv 保存逐文件清单。",
            ],
        }
        (self.local_out / "session_metadata.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def run(self) -> int:
        self.verify_access()
        self.build_and_push()
        self.record_document_baseline()
        index = 1
        self.open_stage(index)
        index += 1
        self.measured_stage(index, "02_new_word", "新建 Word 文档", "打开新建菜单、选择 Word、创建空白文档", self.new_word, 5)
        index += 1
        self.measured_stage(index, "03_write_metadata", "写入时间、目的、操作链和初步结论", "写入实验元数据", self.write_metadata, 5)
        index += 1
        self.measured_stage(index, "04_heavy_edit_scroll", "写入大文本、换行、翻页、滚动和光标移动", "高负载文档编辑与导航", self.heavy_edit_scroll, 8)
        index += 1
        self.measured_stage(index, "05_save_document", "保存 Word 文档并验证设备文件", "保存到用户可见 Desktop 并检查真实路径/大小/mtime", self.save_document, 5)
        index += 1
        self.measured_stage(index, "06_background", "切换 WPS 到后台", "Home 切后台", lambda: self.ui_key(KEY_HOME), 4)
        index += 1
        self.measured_stage(index, "07_foreground", "切回 WPS 前台", "重新拉起 WPS 前台", self.start_wps, 4)
        index += 1
        self.close_wps(index, "close_before_reopen")
        if self.saved_document:
            try:
                self.rename_saved_document()
            except HdcError as exc:
                self.failures.append(f"rename_saved_document: {exc}")
        index += 1
        self.reopen_stage(index)
        index += 1
        self.measured_stage(index, "09_reopen_edit_scroll", "重新打开后滚动、翻页和少量编辑", "重新打开文档的轻量编辑与导航", self.reopen_edit_scroll, 6)
        index += 1
        self.close_wps(index, "close_final")
        return 1 if self.failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HarmonyOS WPS v6 document workflow and memory collector")
    parser.add_argument("--target", help="hdc target serial")
    parser.add_argument("--out", help="local output directory")
    parser.add_argument("--device-dir", default=DEFAULT_DEVICE_DIR)
    parser.add_argument("--device-out", default=DEFAULT_DEVICE_OUT_ROOT)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--no-build", action="store_true", help="reuse v6 device binary")
    parser.add_argument("--launch-wait-s", type=float, default=10.0)
    parser.add_argument("--editor-x", type=int, default=1100, help="WPS XComponent text focus x")
    parser.add_argument("--editor-y", type=int, default=1020, help="WPS XComponent text focus y")
    parser.add_argument("--heavy-repeats", type=int, default=180, help="repeated paragraphs in heavy phase")
    parser.add_argument("--test-serial", default=DEFAULT_TEST_SERIAL, help="fixed ASCII test serial inserted into the Word document")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session: Session | None = None
    return_code = 2
    try:
        session = Session(args)
        return_code = session.run()
        return return_code
    except (HdcError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if session is not None:
            session.failures.append(str(exc))
        return return_code
    finally:
        if session is not None:
            try:
                session.write_metadata_file(return_code)
            finally:
                session.close_files()


if __name__ == "__main__":
    raise SystemExit(main())
