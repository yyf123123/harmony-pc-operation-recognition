"""One Linux command: diagnose, build, self-test, optional smoke, unique report.

No dependency installer, sudo, application launch, process kill or cache clearing.
Configuration selects selftest, an unlabeled Native controlled window, or one
verified desktop smoke action, or an external-automation batch gated by a full
36-window preflight. Desktop validation is still performed on the target host.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import uuid

from collector.linux.procfs import LinuxProcfs
from collector.linux.session import capture_sample
from collector.linux.wps_automation import operation_readiness
from scripts.linux_desktop import make_plan
from scripts.linux import export_sample

ROOT = Path(__file__).resolve().parents[1]


def _new_run(output_root):
    parent = Path(output_root).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
    output.mkdir(exist_ok=False)
    return output


def run(config: dict, *, repo: Path = ROOT) -> dict:
    if config.get("schema_version") != "linux.launcher.v1":
        raise ValueError("unsupported launcher configuration")
    mode = config.get("mode", "selftest")
    if mode not in ("selftest", "controlled", "desktop-smoke", "batch"):
        raise ValueError("only selftest, controlled, desktop-smoke and batch are available")
    output = _new_run(config["output_root"])
    report = {"schema_version": "linux.run-report.v1", "mode": mode, "status": "running",
              "output": str(output), "architecture": platform.machine(), "platform": platform.system(),
              "desktop_validation": "pending", "formal_collection_enabled": False, "stages": []}

    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    def command(name, argv, timeout=300, *, passthrough=False):
        started = time.monotonic_ns()
        record = {"name": name, "status": "running"}
        report["stages"].append(record)
        save()
        try:
            # Action stdout/stderr can contain document content. Never persist it.
            result = subprocess.run([str(arg) for arg in argv], cwd=repo, timeout=timeout,
                                    capture_output=not passthrough, check=False)
            record.update(returncode=result.returncode, elapsed_ns=time.monotonic_ns() - started,
                          status="passed" if result.returncode == 0 else "failed")
            if result.returncode:
                raise RuntimeError(f"{name} failed (exit {result.returncode}); see direct tool diagnostics")
        except (OSError, subprocess.TimeoutExpired):
            record.update(status="failed", elapsed_ns=time.monotonic_ns() - started)
            raise RuntimeError(f"{name} unavailable or timed out") from None
        finally:
            save()

    try:
        if platform.system() != "Linux":
            raise ValueError("unified launcher targets Linux; use opsctl directly for host offline tests")
        missing = [tool for tool in ("cmake", "c++") if not shutil.which(tool)]
        if missing:
            raise ValueError("required build tools unavailable: " + ", ".join(missing))
        page_size = os.sysconf("SC_PAGE_SIZE")
        if type(page_size) is not int or page_size <= 0:
            raise ValueError("system page size is unavailable")
        report["page_size_bytes"] = page_size
        build = output / "build"
        command("configure", ["cmake", "-S", repo, "-B", build, "-DCMAKE_BUILD_TYPE=Release"])
        command("build", ["cmake", "--build", build, "--parallel", str(config.get("build_jobs", 2))])
        opsctl = build / "native" / "opsctl"
        fixtures = repo / config.get("fixtures", "native/fixtures/raw-window")
        batch_config = None
        if mode == "batch":
            from collector.linux.batch import validate_config
            batch_config = validate_config(json.loads(Path(config["batch_config"]).expanduser().read_text()))
        doctor = [opsctl, "doctor", "--models", repo / "models", "--app", "wps"]
        for pid in batch_config["pids"] if batch_config else config.get("pids", []):
            if type(pid) is not int or pid <= 0:
                raise ValueError("pids must be explicit positive integers")
            doctor.extend(("--pid", str(pid)))
        command("doctor", doctor)
        command("native-selftest", [opsctl, "selftest", "--models", repo / "models", "--fixtures", fixtures])
        command("native-tests", ["ctest", "--test-dir", build, "--output-on-failure"])
        if mode == "batch":
            from collector.linux.batch import run_batch
            from collector.linux.batch_export import export_batch
            batch_config = {**batch_config, "build_dir": str(build)}
            batch = run_batch(batch_config, output / "batch", repo=repo)
            report.update(formal_collection_enabled=batch["formal_collection_enabled"],
                          validation_scope=batch["validation_scope"], batch_report=str(output/"batch/report.json"),
                          valid_windows=batch["valid_windows"])
            if batch["status"] != "passed":
                raise RuntimeError("batch did not complete; inspect batch/report.json, then use linux_batch run --resume")
            exported = export_batch(output / "batch", output / "dataset")
            report.update(desktop_validation="configured_full_class_verifiers_passed",
                          dataset_report=str(output/"dataset/quality.json"), formal_windows=exported["formal_windows"])
        elif mode == "controlled":
            # The core receives only opaque window/event IDs, never operation labels.
            if not config.get("device_config"):
                raise ValueError("controlled mode requires a Native device_config")
            command("controlled-window", [opsctl, "run", "--config", Path(config["device_config"]).expanduser().resolve(),
                    "--window-id", uuid.uuid4().hex, "--event-id", uuid.uuid4().hex,
                    "--output", output / "recognition.jsonl"],
                    timeout=float(config.get("controlled_timeout_s", 120)), passthrough=True)
            report["validation_scope"] = "one_controlled_window; inspect diagnostics and evidence"
        elif mode == "desktop-smoke":
            state = output / "precondition-state.json"
            plan = make_plan(Path(config["desktop_config"]).expanduser().resolve(), state)
            (output / "action-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            backend = LinuxProcfs(config["pids"], build / "collector/linux/linux_vma_collect")
            sample = capture_sample(plan=plan, backend=backend, output=output / "capture",
                session_id=config["session_id"], trial_id=config["trial_id"],
                app_version=config["app_version"], device_id=config["device_id"])
            export_sample(sample, output / "replay")
            command("smoke-native-prediction", [build / "native/operation_predict", "--models", repo / "models",
                    "--app", "wps", "--schema", "wps.vma.feature.v1", "--vectors",
                    output / "replay/vma_vectors_l2.csv", "--output", output / "predictions.csv"])
            report.update(sample_id=sample["sample_id"], operation_id=plan["operation_id"],
                          desktop_validation="one_operation_smoke_passed", validation_scope="one_smoke_only")
        else:
            report["validation_scope"] = "offline_build_selftest_only"
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
    finally:
        save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run(json.loads(args.config.read_text(encoding="utf-8")))
    print(json.dumps({"status": report["status"], "report": str(Path(report["output"]) / "report.json"),
                      "formal_collection_enabled": report.get("formal_collection_enabled", False)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(f"Linux launcher: {error}")
