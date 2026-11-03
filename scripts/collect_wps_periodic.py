#!/usr/bin/env python3
"""Linux WPS main-process-only, unlabeled periodic smaps capture (stdlib only).

Direct execution is supported; no repository imports, models or automation are
needed. The explicit PID/executable and start identity are checked throughout.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PROFILE = "linux.wps.main-periodic.v1"
CORE = {"Size", "Rss", "Referenced"}
FIELDS = CORE | {"Pss", "Anonymous", "Private_Clean", "Private_Dirty",
                 "Shared_Clean", "Shared_Dirty", "Swap", "KernelPageSize", "MMUPageSize"}
HEADER = re.compile(r"^([0-9a-fA-F]+)-([0-9a-fA-F]+) ([r-][w-][x-][ps]) "
                    r"([0-9a-fA-F]+) ([0-9a-fA-F]+:[0-9a-fA-F]+) (\d+)(?:\s+(.*))?$")


class CaptureError(RuntimeError):
    """A failed observation, never a valid zero vector."""


class IdentityChanged(CaptureError):
    pass


def now_ns():
    return time.monotonic_ns()


def identity(pid, expected_exe, proc_root=Path("/proc")):
    process = proc_root / str(pid)
    if process.stat().st_uid != os.getuid():
        raise CaptureError("目标进程不属于当前账号")
    text = (process / "stat").read_text()
    prefix, separator, tail = text.rpartition(") ")
    fields = tail.split()
    if not separator or int(prefix.split("(", 1)[0]) != pid or len(fields) < 20:
        raise CaptureError("进程 stat 格式错误")
    if fields[0] in {"Z", "X"}:
        raise IdentityChanged("目标进程已退出")
    executable = (process / "exe").resolve(strict=True)
    if executable != expected_exe:
        raise IdentityChanged("PID 对应的可执行文件不匹配")
    return {"pid": pid, "start_ticks": int(fields[19]), "ppid": int(fields[1]),
            "executable": str(executable)}


def check_target(pid, expected_exe, original):
    current = identity(pid, expected_exe)
    if current["start_ticks"] != original["start_ticks"]:
        raise IdentityChanged("目标启动身份改变；不会自动重新绑定 PID")


def parse_smaps(data):
    """Retain VMA headers and all kB metrics; reject truncated/core-missing input."""
    if not data or not data.endswith(b"\n"):
        raise CaptureError("smaps 为空或末行截断")
    regions = []
    current = None
    previous_end = 0
    for line in data.decode("utf-8", errors="surrogateescape").splitlines():
        match = HEADER.fullmatch(line)
        if match:
            start, end, permissions, offset, device, inode, pathname = match.groups()
            if int(start, 16) < previous_end or int(end, 16) <= int(start, 16):
                raise CaptureError("VMA 地址顺序或范围非法")
            previous_end = int(end, 16)
            current = {"region_index": len(regions), "start": start, "end": end,
                       "permissions": permissions, "offset": offset, "device": device,
                       "inode": int(inode), "pathname": pathname or "", "metrics_kib": {}}
            regions.append(current)
        elif current is None or ":" not in line:
            raise CaptureError("smaps VMA/字段结构非法")
        else:
            key, value = line.split(":", 1)
            value = value.strip()
            if key == "VmFlags":
                current["vm_flags"] = value.split()
            elif value.endswith(" kB"):
                if key in current["metrics_kib"]:
                    raise CaptureError("smaps 字段重复")
                try:
                    number = float(value[:-3].strip())
                except ValueError:
                    raise CaptureError("smaps 数值非法") from None
                if not math.isfinite(number) or number < 0:
                    raise CaptureError("smaps 数值非法")
                current["metrics_kib"][key] = number
            elif key in FIELDS:
                raise CaptureError("smaps 必需内存字段单位非法")
    for region in regions:
        if not CORE <= region["metrics_kib"].keys():
            raise CaptureError("smaps 缺少 Size/Rss/Referenced")
        region["missing_fields"] = sorted(FIELDS - region["metrics_kib"].keys())
        if "vm_flags" not in region:
            region["missing_fields"].append("VmFlags")
    if not regions:
        raise CaptureError("smaps 没有 VMA")
    return regions


def focus_evidence(enabled, pid):
    if not enabled:
        return {"status": "unknown", "source": "not_collected"}
    try:
        def call(*argv):
            result = subprocess.run(["xdotool", *argv], capture_output=True, text=True,
                                    timeout=1, check=True)
            value = result.stdout.strip()
            if not value.isdecimal():
                raise ValueError("invalid focus evidence")
            return value
        window = call("getactivewindow")
        owner = int(call("getwindowpid", window))
        return {"status": "target" if owner == pid else "other", "window_id": window,
                "window_pid": owner, "source": "xdotool-boundary-check",
                "checked_ns": now_ns()}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unknown", "source": "xdotool-failed", "checked_ns": now_ns()}


@contextmanager
def target_lock(pid, start_ticks):
    import fcntl
    directory = Path(tempfile.gettempdir()) / ("harmony-wps-capture-" + str(os.getuid()))
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise CaptureError("采集锁目录权限不安全")
    fd = os.open(directory / f"{pid}-{start_ticks}.lock",
                 os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptureError("本脚本已有实例正在采集该 PID") from None
        yield
    finally:
        os.close(fd)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


def append_json(handle, value):
    handle.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
    handle.flush()


def capture(args):
    """Only this explicitly selected process is read/cleared; no child discovery."""
    import resource
    if sys.platform != "linux":
        raise CaptureError("真实采集只能在 Linux 运行")
    if (args.pid <= 0 or not math.isfinite(args.interval) or not math.isfinite(args.seconds)
            or not 0.1 <= args.interval <= 60 or not args.interval <= args.seconds <= 86400):
        raise CaptureError("PID、时长或采样周期非法")
    expected_exe = args.wps_executable.resolve(strict=True)
    original = identity(args.pid, expected_exe)
    process = Path("/proc") / str(args.pid)
    # Check access before creating output or changing reference bits.
    with (process / "smaps").open("rb") as handle:
        if not handle.read(1):
            raise CaptureError("目标 smaps 为空")
    fd = os.open(process / "clear_refs", os.O_WRONLY | os.O_CLOEXEC)
    os.close(fd)
    check_target(args.pid, expected_exe, original)
    output = args.output.absolute()
    focus_enabled = args.focus_x11
    if focus_enabled and not shutil.which("xdotool"):
        raise CaptureError("--focus-x11 需要现有 xdotool；不会自动安装")
    with target_lock(args.pid, original["start_ticks"]):
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        (output / "raw").mkdir(mode=0o700)
        session = {"schema": "wps.periodic-session.v1", "capture_profile": PROFILE,
                   "capture_profile_identity": f"{PROFILE}|interval_seconds={args.interval:.9g}|scope=main_only",
                   "scope": "main_process_only", "child_processes_collected": False,
                   "target": original, "interval_seconds": args.interval,
                   "requested_seconds": args.seconds, "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
                   "app_version": args.app_version, "kernel": os.uname().release,
                   "created_utc": datetime.now(timezone.utc).isoformat(),
                   "clock": "CLOCK_MONOTONIC nanoseconds", "clear_refs_value": 1,
                   "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "focus_checks": "boundary_only" if focus_enabled else "not_collected",
                   "automation_communication": False, "labels_collected": False,
                   "status": "running"}
        write_json(output / "session.json", session)
        start = now_ns()
        cpu_start = time.process_time()
        children_start = resource.getrusage(resource.RUSAGE_CHILDREN)
        step = round(args.interval * 1e9)
        end = start + round(args.seconds * 1e9)
        deadline = start + step
        attempted = valid = skipped = failures = interrupted = 0
        consecutive_errors = 0
        status = "completed"
        terminal_error = None
        active_frame = None
        with (output / "frames.jsonl").open("x", encoding="utf-8") as frames, \
                (output / "quality_events.jsonl").open("x", encoding="utf-8") as events, \
                gzip.open(output / "vma.jsonl.gz", "xt", encoding="utf-8") as vmas:
            try:
                while deadline <= end:
                    attempted += 1
                    frame = active_frame = {"schema": "wps.periodic-frame.v1", "frame_id": attempted,
                        "pid": args.pid, "start_ticks": original["start_ticks"],
                        "scheduled_read_ns": deadline, "capture_valid": False,
                        "foreground_evidence": "unknown"}
                    if now_ns() >= deadline:
                        skipped += 1
                        frame.update(status="skipped_deadline", finished_ns=now_ns())
                        append_json(frames, frame)
                        active_frame = None
                        deadline += step
                        continue
                    try:
                        check_target(args.pid, expected_exe, original)
                        frame["focus_before"] = focus_evidence(focus_enabled, args.pid)
                        check_target(args.pid, expected_exe, original)
                        frame["clear_start_ns"] = now_ns()
                        with (process / "clear_refs").open("w") as handle:
                            if handle.write("1\n") != 2:
                                raise CaptureError("clear_refs 短写")
                        frame["clear_end_ns"] = now_ns()
                        check_target(args.pid, expected_exe, original)
                        if now_ns() >= deadline:
                            raise CaptureError("清引用或校验超出本轮观察预算")
                        remaining = deadline - now_ns()
                        if remaining > 0:
                            time.sleep(remaining / 1e9)
                        check_target(args.pid, expected_exe, original)
                        frame["read_start_ns"] = now_ns()
                        data = (process / "smaps").read_bytes()
                        frame["read_end_ns"] = now_ns()
                        raw = output / "raw" / f"{attempted:06d}.smaps.gz"
                        with gzip.open(raw, "xb") as handle:
                            handle.write(data)
                        frame.update(raw_path=str(raw.relative_to(output)), raw_bytes=len(data),
                                     raw_sha256=hashlib.sha256(data).hexdigest())
                        check_target(args.pid, expected_exe, original)
                        regions = parse_smaps(data)
                        frame["focus_after"] = focus_evidence(focus_enabled, args.pid)
                        check_target(args.pid, expected_exe, original)
                        frame["validated_ns"] = now_ns()
                        frame["read_lateness_ns"] = frame["read_start_ns"] - deadline
                        frame["observation_bounds_ns"] = [frame["read_start_ns"] - frame["clear_end_ns"],
                                                          frame["read_end_ns"] - frame["clear_start_ns"]]
                        frame["timing_valid"] = (frame["read_lateness_ns"] <= step // 2
                                                  and frame["read_end_ns"] - frame["read_start_ns"] <= step)
                        for region in regions:
                            append_json(vmas, {"frame_id": attempted, "pid": args.pid,
                                               "start_ticks": original["start_ticks"], **region})
                        states = [frame["focus_before"]["status"], frame["focus_after"]["status"]]
                        frame["foreground_evidence"] = ("boundary_target" if states == ["target", "target"]
                            else "other" if "other" in states else "unknown")
                        frame.update(vma_count=len(regions), capture_valid=frame["timing_valid"],
                                     status="ok" if frame["timing_valid"] else "timing_invalid")
                        if frame["capture_valid"]:
                            valid += 1
                            consecutive_errors = 0
                        else:
                            failures += 1
                            consecutive_errors += 1
                    except (OSError, CaptureError) as error:
                        failures += 1
                        consecutive_errors += 1
                        # Do not persist arbitrary OS error strings or command output.
                        frame.update(status="failed", error_type=type(error).__name__)
                        if isinstance(error, IdentityChanged) or isinstance(error, ProcessLookupError) or \
                                not (process / "stat").exists():
                            terminal_error = "target_exited_or_changed"
                    frame["record_write_start_ns"] = now_ns()
                    append_json(frames, frame)
                    if not frame["capture_valid"]:
                        append_json(events, {"frame_id": attempted, "status": frame["status"],
                                             "error_type": frame.get("error_type")})
                    active_frame = None
                    if terminal_error or consecutive_errors >= 3:
                        status = "failed"
                        terminal_error = terminal_error or "three_consecutive_invalid_frames"
                        break
                    deadline += step
            except KeyboardInterrupt:
                status = "interrupted"
                if active_frame is not None:
                    interrupted += 1
                    active_frame.update(status="interrupted", capture_valid=False, finished_ns=now_ns())
                    append_json(frames, active_frame)
            except Exception as error:
                status = "failed"
                terminal_error = type(error).__name__
                if active_frame is not None:
                    failures += 1
                    active_frame.update(status="failed", capture_valid=False, error_type=terminal_error,
                                        finished_ns=now_ns())
                    append_json(frames, active_frame)
        elapsed = (now_ns() - start) / 1e9
        children_end = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = time.process_time() - cpu_start + children_end.ru_utime + children_end.ru_stime \
            - children_start.ru_utime - children_start.ru_stime
        session.update(status=status, terminal_error=terminal_error, planned_slots=(end - start) // step,
                       attempted_slots=attempted, valid_frames=valid, failed_frames=failures,
                       interrupted_frames=interrupted,
                       skipped_slots=skipped, elapsed_seconds=elapsed,
                       collector_and_helper_cpu_seconds=cpu,
                       collector_and_helper_single_core_percent=cpu / elapsed * 100,
                       collector_max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       focus_is_continuously_proven=False)
        write_json(output / "session.json", session)
        manifest = []
        for path in sorted(output.rglob("*")):
            if path.is_file():
                manifest.append({"path": str(path.relative_to(output)), "bytes": path.stat().st_size,
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        write_json(output / "manifest.json", {"schema": "wps.periodic-manifest.v1", "files": manifest})
        return session


def main():
    parser = argparse.ArgumentParser(description="Linux WPS 主进程无标签定时采集；只需 Python 3.10+")
    parser.add_argument("--pid", type=int, required=True, help="明确指定本账号 WPS Writer 主进程 PID")
    parser.add_argument("--output", type=Path, required=True, help="父目录须存在；输出目录必须是新目录，置于仓库外")
    parser.add_argument("--interval", type=float, default=2, help="目标读取周期/秒，默认 2")
    parser.add_argument("--seconds", type=float, default=1800, help="总观察秒数，默认 1800")
    parser.add_argument("--wps-executable", type=Path, default=Path("/opt/kingsoft/wps-office/office6/wps"),
                        help="须与 /proc/PID/exe 对应的真实二进制一致")
    parser.add_argument("--app-version", default="unknown", help="建议填写实际 WPS 版本")
    parser.add_argument("--focus-x11", action="store_true", help="使用现有 xdotool 作前后焦点记录；不读取标题")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("真实 /proc 采集只能在 Linux 运行")
    def interrupt(*unused):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    os.umask(0o077)
    try:
        result = capture(args)
    except (OSError, CaptureError, ValueError) as error:
        print("采集未启动或保存失败：" + type(error).__name__ + "; 检查 PID、可执行文件、权限及新输出目录", file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "valid_frames": result["valid_frames"],
                      "attempted_slots": result["attempted_slots"], "scope": result["scope"],
                      "output": str(args.output.absolute())}, ensure_ascii=False))
    return 130 if result["status"] == "interrupted" else 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
