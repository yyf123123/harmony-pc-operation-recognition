"""Explicit safe WPS desktop smoke commands used by the unified launcher."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from collector.linux.wps_automation import action, focus, load_config, operation_readiness, precondition, verify


def make_plan(config_path, state_path):
    config = load_config(config_path)
    prefix = [sys.executable, "-m", "scripts.linux_desktop"]
    return {"schema_version": "linux.wps.action-plan.v1", "operation_id": config["operation_id"],
            "precondition": [*prefix, "precondition", "--config", str(config_path), "--state", str(state_path)],
            "action": [*prefix, "action", "--config", str(config_path)],
            "verify": [*prefix, "verify", "--config", str(config_path), "--state", str(state_path)],
            "focus": [*prefix, "focus", "--config", str(config_path)],
            "baseline_s": 5, "action_s": 15, "post_s": 5, "command_timeout_s": 15,
            "automation_status": "desktop_validation_pending"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("readiness")
    for name in ("focus", "precondition", "action", "verify", "make-plan"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--config", type=Path, required=True)
        if name in ("precondition", "verify", "make-plan"):
            cmd.add_argument("--state", type=Path, required=True)
        if name == "make-plan":
            cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "readiness":
        print(json.dumps({"schema_version": "linux.wps.automation-readiness.v1", "operations": operation_readiness()}, indent=2))
        return 0
    config = load_config(args.config)
    if args.command == "focus":
        print(json.dumps(focus(config)))
    elif args.command == "precondition":
        precondition(config, args.state)
    elif args.command == "action":
        action(config)
    elif args.command == "verify":
        verify(config, args.state)
    else:
        plan = make_plan(args.config.resolve(), args.state.resolve())
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(plan, handle, indent=2)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, ImportError) as error:
        raise SystemExit(f"WPS desktop smoke: {error}")
