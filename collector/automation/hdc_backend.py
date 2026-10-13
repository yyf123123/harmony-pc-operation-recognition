"""Defensive HDC transport and device-discovery boundary.

UI actions remain device-validation gated.  This module nevertheless owns all
transport failure handling so a future adapter cannot accidentally treat an HDC
exit code of zero as proof that an action happened.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from apps.base import OperationSpec
from collector.smaps_collector import SmapsEmptyError
from .base import AutomationBackend, BackendActionResult, DeviceValidationRequired


class HdcError(RuntimeError):
    def __init__(self, code: str, message: str, *, result: "HdcCommandResult | None" = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.result = result


@dataclass(frozen=True)
class HdcCommandResult:
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    utf8 = data.decode("utf-8", errors="replace")
    if "\ufffd" not in utf8:
        return utf8
    return data.decode("gbk", errors="replace")


class HdcBackend(AutomationBackend):
    source = "hdc"
    is_mock = False
    _SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
    _PID = re.compile(r"\b\d+\b")

    def __init__(
        self,
        hdc: str | None = None,
        target: str | None = None,
        *,
        timeout_s: float = 30.0,
        retries: int = 2,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.hdc = hdc or shutil.which("hdc") or "hdc"
        self.target = target
        self.timeout_s = float(timeout_s)
        self.retries = max(0, int(retries))
        self._runner = runner or subprocess.run
        self.validated_app_identifiers: dict[str, dict[str, str]] = {}
        self.last_target_inventory: tuple[str, ...] = ()
        self.last_process_restart: dict[str, Any] | None = None
        self._temporary_paths: set[Path] = set()

    def _argv(self, args: Sequence[str], *, target: str | None = None) -> list[str]:
        selected = target if target is not None else self.target
        # argv is passed directly to CreateProcess; shell=True is forbidden so
        # Windows spaces and quoting cannot alter the command boundary.
        prefix = ["-t", selected] if selected else []
        return [self.hdc, *prefix, *map(str, args)]

    def run(self, args: Sequence[str], *, timeout_s: float | None = None) -> HdcCommandResult:
        argv = tuple(self._argv(args))
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        last: HdcCommandResult | None = None
        for attempt in range(1, self.retries + 2):
            try:
                completed = self._runner(argv, capture_output=True, timeout=timeout, shell=False)
                result = HdcCommandResult(argv, _decode(completed.stdout), _decode(completed.stderr), int(completed.returncode), False, attempt)
            except subprocess.TimeoutExpired as error:
                result = HdcCommandResult(argv, _decode(getattr(error, "output", b"")), _decode(getattr(error, "stderr", b"")), -1, True, attempt)
            except OSError as error:
                result = HdcCommandResult(argv, "", str(error), -1, False, attempt)
            last = result
            if result.ok:
                return result
            if result.timed_out or self._is_transient(result):
                continue
            return result
        return last or HdcCommandResult(argv, "", "no result", -1, False, 0)

    @staticmethod
    def _is_transient(result: HdcCommandResult) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        return any(token in text for token in ("offline", "not connected", "transport", "connection reset", "daemon"))

    @staticmethod
    def _raise_for_result(result: HdcCommandResult, *, operation: str) -> None:
        text = f"{result.stdout}\n{result.stderr}".lower()
        if result.timed_out:
            raise HdcError("HDC_TIMEOUT", operation, result=result)
        if any(token in text for token in ("offline", "not connected", "transport", "device not found")):
            raise HdcError("HDC_DISCONNECTED", operation, result=result)
        if "permission denied" in text or "access denied" in text:
            raise HdcError("PERMISSION_DENIED", operation, result=result)
        raise HdcError("HDC_COMMAND_FAILED", operation, result=result)

    def discover_targets(self) -> tuple[str, ...]:
        result = self.run(("list", "targets"))
        if not result.ok:
            self._raise_for_result(result, operation="device discovery")
        targets: list[str] = []
        for line in result.stdout.splitlines():
            value = line.strip()
            if not value or value.lower() in {"list targets", "targets"}:
                continue
            # HDC may print a status column after the serial.  The first token
            # is the stable target identifier.
            target = value.split()[0]
            if target.lower() not in {"serial", "device"} and target not in targets:
                targets.append(target)
        self.last_target_inventory = tuple(targets)
        if self.target and self.target not in targets:
            raise HdcError("HDC_DISCONNECTED", f"requested target {self.target!r} is not listed")
        if len(targets) > 1 and not self.target:
            raise HdcError("MULTIPLE_TARGETS", "more than one target is connected; select --target")
        if len(targets) == 1:
            self.target = targets[0]
        return tuple(targets)

    def check_permissions(self) -> dict[str, Any]:
        self._ensure_target()
        result = self.run(("shell", "id"))
        if not result.ok:
            self._raise_for_result(result, operation="permission check")
        return {"target": self.target, "id": result.stdout.strip(), "permission_checked": True, "stderr": result.stderr}

    def _ensure_target(self) -> str:
        if not self.target:
            self.discover_targets()
        if not self.target:
            raise HdcError("HDC_DISCONNECTED", "no target connected")
        return self.target

    def register_validated_app(self, app_id: str, *, bundle: str, process: str) -> None:
        if not self._SAFE_IDENTIFIER.fullmatch(bundle) or not self._SAFE_IDENTIFIER.fullmatch(process):
            raise ValueError("bundle and process must be validated simple identifiers")
        self.validated_app_identifiers[app_id] = {"bundle": bundle, "process": process}

    def _require_validation(self, app_id: str) -> dict[str, str]:
        if app_id not in self.validated_app_identifiers:
            raise DeviceValidationRequired(f"{app_id} requires package/process validation before HDC automation")
        return self.validated_app_identifiers[app_id]

    def resolve_pid(self, app_id: str) -> str:
        self._ensure_target()
        identity = self._require_validation(app_id)
        result = self.run(("shell", "pidof", identity["process"]))
        if not result.ok:
            self._raise_for_result(result, operation=f"resolve PID for {app_id}")
        pids = self._PID.findall(result.stdout)
        if not pids:
            raise HdcError("PROCESS_NOT_FOUND", f"process {identity['process']} is not running", result=result)
        unique_pids = tuple(dict.fromkeys(pids))
        if len(unique_pids) != 1:
            raise HdcError(
                "PROCESS_AMBIGUOUS",
                f"process {identity['process']} resolved to multiple PIDs {unique_pids}; select a single process boundary",
                result=result,
            )
        return unique_pids[0]

    def ensure_process_alive(self, app_id: str, previous_pid: str | None = None) -> str:
        pid = self.resolve_pid(app_id)
        # A changed PID is an explicit restart event; callers must attach it to
        # the new session/trial rather than silently mixing process lifetimes.
        self.last_process_restart = {"app_id": app_id, "previous_pid": previous_pid, "current_pid": pid, "restarted": previous_pid is not None and previous_pid != pid}
        return pid

    def clear_refs(self, pid: str) -> HdcCommandResult:
        self._ensure_target()
        if not pid.isdigit():
            raise ValueError("PID must be numeric")
        result = self.run(("shell", "sh", "-c", f"printf 1 > /proc/{pid}/clear_refs"))
        if not result.ok:
            if result.timed_out:
                raise HdcError("HDC_TIMEOUT", f"clear_refs {pid}", result=result)
            if "permission denied" in f"{result.stdout}\n{result.stderr}".lower():
                raise HdcError("PERMISSION_DENIED", f"clear_refs {pid}", result=result)
            raise HdcError("CLEAR_REFS_FAILED", f"clear_refs {pid}", result=result)
        return result

    def capture_smaps(self, pid: str) -> str:
        self._ensure_target()
        if not pid.isdigit():
            raise ValueError("PID must be numeric")
        result = self.run(("shell", "cat", f"/proc/{pid}/smaps"))
        if not result.ok:
            if result.timed_out:
                raise HdcError("HDC_TIMEOUT", f"capture smaps {pid}", result=result)
            if "permission denied" in f"{result.stdout}\n{result.stderr}".lower():
                raise HdcError("PERMISSION_DENIED", f"capture smaps {pid}", result=result)
            raise HdcError("CAPTURE_FAILED", f"capture smaps {pid}", result=result)
        if not result.stdout.strip():
            raise SmapsEmptyError(f"empty smaps for pid {pid}")
        return result.stdout

    def recv(self, remote_path: str, destination_dir: str | Path) -> Path:
        self._ensure_target()
        directory = Path(destination_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="hdc_", suffix=".tmp", dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
        self._temporary_paths.add(temporary)
        result = self.run(("file", "recv", remote_path, str(temporary)))
        if not result.ok:
            temporary.unlink(missing_ok=True)
            self._temporary_paths.discard(temporary)
            if result.timed_out:
                raise HdcError("HDC_TIMEOUT", f"transfer {remote_path}", result=result)
            raise HdcError("TRANSFER_FAILED", f"transfer {remote_path}", result=result)
        return temporary

    def close(self) -> None:
        for path in list(self._temporary_paths):
            path.unlink(missing_ok=True)
            self._temporary_paths.discard(path)

    def prepare(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        self._require_validation(app_id)
        raise DeviceValidationRequired("HDC PREPARE requires an app-specific device adapter and Gate 3 smoke test")

    def execute(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        self._require_validation(app_id)
        raise DeviceValidationRequired("HDC ACTION requires an app-specific device adapter and Gate 3 smoke test")

    def confirm(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        self._require_validation(app_id)
        raise DeviceValidationRequired("ACTION_CONFIRM is NEEDS_DEVICE_VALIDATION until Gate 3")

    def validate(self, app_id: str, operation: OperationSpec, state: Mapping[str, Any]) -> BackendActionResult:
        self._require_validation(app_id)
        raise DeviceValidationRequired("HDC POSTCONDITION validation requires a device pilot")
