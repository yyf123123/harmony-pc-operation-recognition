"""Plan and execute the frozen 18-class Linux WPS batch using external automation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from collector.linux.batch import OPS, ROLES, SCHEMA, make_schedule, run_batch


def template():
    """Explicitly incomplete template: no successful UI checks are invented."""
    return {"schema_version": SCHEMA, "batch_id": "wps-first-batch", "app_version": "FILL_ACTUAL_VERSION",
            "device_id": "FILL_ANONYMOUS_DEVICE_ID", "pids": [], "root_pid": 0, "build_dir": "/absolute/build/native",
            "seed": 20261017, "max_attempts": 3,
            "documents": [{"document_id": f"doc-{n}", "family_id": f"family-{n}",
                           "split": "train" if n < 3 else "validation" if n == 3 else "test",
                           "path": f"/absolute/test-templates/{n}.docx", "test_copy": False} for n in range(5)],
            "automation_files": ["/absolute/automation/driver.py"],
            "automation": {role: [sys.executable, "/absolute/automation/driver.py", role,
                            "--operation", "{operation_id}", "--document", "{document_path}",
                            "--state-dir", "{state_dir}"] for role in sorted(ROLES)},
            "workflows": [{"workflow_id": f"flow-{n}", "operations": []} for n in range(6)],
            "instructions": "Fill explicit PIDs, actual test copies and six verified legal 20-operation workflows. "
                            "Driver accepts the catalog operation_id; focus prints pid/window_id/source JSON. "
                            "begin_session prepares once; continuous preconditions only check, never reset the document."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    example = commands.add_parser("template", help="write an intentionally incomplete config, no UI")
    example.add_argument("--output", type=Path, required=True)
    plan = commands.add_parser("plan", help="validate quotas/schedule only, no commands or /proc")
    plan.add_argument("--config", type=Path, required=True)
    run = commands.add_parser("run", help="actual external automation and /proc capture; Linux only")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--resume", action="store_true")
    export = commands.add_parser("export", help="verify receipts and export six split datasets; no UI or /proc")
    export.add_argument("--batch", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "export":
        from collector.linux.batch_export import export_batch
        report = export_batch(args.batch, args.output)
        print(json.dumps({"status": report["status"], "formal_windows": report["formal_windows"], "quality_report": str(args.output.resolve()/"quality.json")}))
        return 0
    if args.command == "template":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(template(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print("Template written; incomplete and cannot run until calibrated.")
        return 0
    config = json.loads(args.config.read_text(encoding="utf-8"))
    schedule = make_schedule(config)
    if args.command == "plan":
        print(json.dumps({"status": "plan_valid", "units": len(schedule), "windows": sum(len(u["operations"]) for u in schedule),
                          "preflight": 36, "formal_independent": 900, "formal_continuous": 600,
                          "desktop_validated": False, "formal_collection_enabled": False}))
        return 0
    if sys.platform != "linux":
        raise ValueError("actual batch execution requires Linux")
    report = run_batch(config, args.output, resume=args.resume)
    print(json.dumps({"status": report["status"], "report": str(args.output.resolve() / "report.json"),
                      "valid_windows": report["valid_windows"], "dataset_ready": report["dataset_ready"]}))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        raise SystemExit(f"Linux batch: {type(error).__name__}: {error}")
