"""Finite 18-operation, input-diagnosis and persistent controlled-window checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import random
import subprocess
import time
import uuid

from collector.linux.wps_acceptance import WpsTestApplication, LABELS, SHORT, MEDIUM
from scripts.linux_desktop_check import NativeStream, PROFILE


def save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n"); temp.replace(path)


def process_stats(pid):
    tail=(Path("/proc")/str(pid)/"stat").read_text().rpartition(") ")[2].split()
    status=(Path("/proc")/str(pid)/"status").read_text().splitlines()
    values={line.split(":",1)[0]:line.split(":",1)[1].strip() for line in status if ":" in line}
    return {"pid":pid,"cpu_ticks":int(tail[11])+int(tail[12]),
            "rss_kib":int(values.get("VmRSS","0 kB").split()[0]),
            "hwm_kib":int(values.get("VmHWM","0 kB").split()[0]),"monotonic_ns":time.monotonic_ns()}


def run_session(args, root):
    root = Path(root); output = root/"acceptance"; output.mkdir()
    wm = subprocess.Popen(["xfwm4", "--compositor=off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    app = None; stream = None
    report = {"schema": "linux.wps.acceptance.v1", "mode": args.acceptance, "status": "running",
              "source": "real-WPS-on-authenticated-isolated-X11", "is_mock": False,
              "app_version": args.app_version, "model_profile": args.model_profile,
              "capture_profile": "controlled-5+5+15+5-with-private-raw-sidecar.v1",
              "formal_collection_enabled": False, "production_deployment_accepted": False,
              "native_sha256": hashlib.sha256(args.native.read_bytes()).hexdigest(),
              "scope": "finite engineering check; not a final generalization estimate", "trials": []}
    started = time.monotonic_ns()
    try:
        app = WpsTestApplication(root/"fixtures")
        report["root_pid"] = app.pid; report["root_start_ticks"] = app.original.start_ticks
        if args.operations == "all": labels = list(LABELS)
        else:
            labels = args.operations.split(",")
            if not labels or any(label not in LABELS for label in labels): raise ValueError("unknown operation subset")
        if args.acceptance == "input":
            cases = [("WRITE_TEXT", SHORT, "keyboard"), ("WRITE_TEXT", MEDIUM, "keyboard"),
                     ("WRITE_TEXT", SHORT, "rpc"), ("WRITE_TEXT", MEDIUM, "rpc")]
        else: cases = [(label, SHORT, "keyboard") for label in labels]
        target_rounds = args.rounds
        if args.acceptance == "soak":
            target_rounds = 1000
            report["soak_scope"] = "persistent Native service with controlled resets; not uninterrupted user-history validation"
        report["order_seed"] = args.order_seed
        deadline = time.monotonic() + args.duration_minutes*60
        service_coverage = None
        for round_index in range(target_rounds):
            ordered_cases = list(cases)
            if args.order_seed is not None:
                random.Random(args.order_seed+round_index).shuffle(ordered_cases)
                # A held-out soak explicitly includes cold short text first.
                if args.acceptance == "soak" and round_index == 0:
                    ordered_cases.sort(key=lambda c:c[0]!="WRITE_TEXT")
            for label, payload, method in ordered_cases:
                if args.acceptance == "soak" and time.monotonic() >= deadline: break
                token = uuid.uuid4().hex; trial_dir = output/token; trial_dir.mkdir()
                trial = {"event_id": token, "round_index": round_index, "operation_label": label,
                         "action_method": app.method(label, method), "payload_characters": len(payload) if label == "WRITE_TEXT" else None,
                         "status": "running", "semantic_passed": False, "capture_valid": False}
                report["trials"].append(trial); save(output/"report.json", report)
                acted = False
                try:
                    tick = time.monotonic_ns(); state = app.prepare(label, payload=payload, input_method=method)
                    trial["prepare_ns"] = time.monotonic_ns()-tick
                    if args.acceptance == "semantic":
                        app.action(state); acted = True; time.sleep(.5)
                    else:
                        identities = app.freeze_coverage(); pids = tuple(v.pid for v in identities)
                        trial["processes"] = [vars(v) for v in identities]
                        if stream is None or args.acceptance != "soak":
                            config = trial_dir/"device.conf"
                            raw = output/"soak-raw.jsonl" if args.acceptance == "soak" else trial_dir/"raw.jsonl"
                            event_file = output/"soak-native.jsonl" if args.acceptance == "soak" else trial_dir/"native.jsonl"
                            config.write_text(f"app_id=wps\nmodels_root={args.models.resolve()}\nmodel_profile={args.model_profile}\n"
                                f"session_id={token}\nroot_pid={app.pid}\ncovered_pids={','.join(map(str,pids))}\n"
                                f"output_jsonl={event_file}\nraw_output_jsonl={raw}\nbaseline_count=2\nbaseline_ms=5000\n"
                                "action_ms=15000\npost_ms=5000\nmax_sample_ms=1000\nfocus_poll_ms=100\n")
                            stream = NativeStream([args.native.resolve(), "serve", "--config", config])
                            service_coverage = pids
                        elif pids != service_coverage:
                            raise RuntimeError("persistent service coverage changed; no automatic expansion")
                        stream.process.stdin.write((token+"\t"+token+"\n").encode()); stream.process.stdin.flush()
                        event = health = None; until = time.monotonic()+85
                        while health is None:
                            record = stream.next(until)
                            if record.get("schema") == "harmony.ops.stage.v1":
                                trial.setdefault("stages", []).append(record)
                                if record.get("phase") == "action":
                                    if acted: raise RuntimeError("duplicate action stage")
                                    tick = time.monotonic_ns(); trial["action_started_ns"] = tick
                                    app.action(state); trial["action_finished_ns"] = time.monotonic_ns()
                                    trial["action_ns"] = trial["action_finished_ns"]-tick; acted = True
                            elif record.get("schema") == "harmony.ops.recognized.v1":
                                if (not acted or event is not None or record.get("event_id") != token or
                                        record.get("status") != "success" or record.get("source") != "controlled_live_window"):
                                    raise RuntimeError("unexpected recognition record")
                                event = record
                            elif record.get("schema") == "harmony.ops.health.v1":
                                if not event or record.get("last_code") != "ok" or not record.get("last_success_timing_ns"):
                                    raise RuntimeError("Native successful completion not confirmed")
                                health = record
                        trial.update(capture_valid=True, prediction=event["operation_label"],
                                     feature_pids=event.get("feature_pids",list(pids)),
                                     cosine_similarity=event["cosine_similarity"], timing_ns=event["timing_ns"],
                                     post_output_timing_ns=health["last_success_timing_ns"],
                                     native_completed_windows=health["completed_windows"], native_phases=event["phases"])
                        trial["native_process_stats"] = process_stats(stream.process.pid)
                        trial["wps_root_process_stats"] = process_stats(app.pid)
                        if args.acceptance != "soak": stream.close(normal=True); stream = None
                    # The Native window is complete before any RPC observation/save.
                    if not acted: raise RuntimeError("operation was never executed")
                    tick = time.monotonic_ns(); trial["semantic_evidence"] = app.verify(state)
                    trial["verification_ns"] = time.monotonic_ns()-tick
                    trial.update(semantic_passed=True, status="passed")
                    if args.acceptance != "semantic": trial["correct"] = trial["prediction"] == label
                except Exception as error:
                    trial.update(status="failed", reason=str(error))
                    if hasattr(error, "evidence"): trial["semantic_evidence"] = error.evidence
                    if stream is not None:
                        if stream.error: trial["native_error"] = stream.error
                        stream.close(); stream = None
                    if acted and not trial["semantic_passed"] and not hasattr(error,"evidence"):
                        try:
                            trial["semantic_evidence"] = app.verify(state)
                            trial["semantic_passed"] = True
                            if not trial["capture_valid"]: trial["status"] = "invalid_capture"
                        except Exception as verification_error:
                            trial["verification_reason"] = str(verification_error)
                    if args.acceptance == "soak": raise
                # Reports and screenshots are private evidence, never features.
                if os.environ.get("HARMONY_ACCEPTANCE_SCREENSHOTS") == "1":
                    subprocess.run(["import", "-window", "root", str(trial_dir/"screen.png")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
                print(json.dumps({"operation": label, "status": trial["status"], "prediction": trial.get("prediction")}), flush=True)
                save(output/"report.json", report)
            if args.acceptance == "soak" and time.monotonic() >= deadline: break
        report["status"] = "completed"
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", reason=str(error))
    finally:
        if stream is not None: stream.close(normal=report["status"] == "completed")
        if app is not None:
            app.close(); report["cleanup"] = app.cleanup
        try: os.killpg(wm.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        wm.wait(timeout=5)
        report["elapsed_ns"] = time.monotonic_ns()-started
        report["semantic_passed"] = sum(t["semantic_passed"] for t in report["trials"])
        report["valid_windows"] = sum(t["semantic_passed"] and t["capture_valid"] for t in report["trials"])
        report["correct_requests"] = sum(t.get("correct",False) and t["semantic_passed"] for t in report["trials"])
        report["all_requested_semantics_passed"] = report["status"] == "completed" and bool(report["trials"]) and all(t["semantic_passed"] for t in report["trials"])
        report["all_requested_capture_passed"] = args.acceptance != "semantic" and report["all_requested_semantics_passed"] and all(t["capture_valid"] for t in report["trials"])
        report["all_requested_recognition_passed"] = args.acceptance != "semantic" and report["all_requested_semantics_passed"] and all(t.get("correct",False) for t in report["trials"])
        save(output/"report.json", report)
    complete = report["all_requested_semantics_passed"] if args.acceptance == "semantic" else report["all_requested_capture_passed"]
    return 0 if complete else 2
