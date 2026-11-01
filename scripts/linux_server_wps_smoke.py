"""Three bounded real-WPS checks on an explicitly selected disposable X11 document.

Run inside the project's authenticated virtual desktop, after opening the blank
test DOCX. This is a calibrated smoke, not an 18-class batch gate. Document
verification happens after Native stops sampling, so verification saves cannot
enter the observed WRITE_TEXT / FORMAT_BOLD window. No labels enter Native.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

from collector.linux.procfs import descendant_pids, identity
from scripts.linux_desktop_check import NativeStream, PROFILE

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
OPERATIONS = (("wps.writer.write_text", "WRITE_TEXT"),
              ("wps.writer.save_document", "SAVE_DOCUMENT"),
              ("wps.writer.format_bold", "FORMAT_BOLD"))


def document_state(path):
    data = Path(path).read_bytes()
    with zipfile.ZipFile(path) as archive:
        tree = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = tree.findall(".//w:body/w:p", NS)
    text = "\n".join("".join(n.text or "" for n in p.findall(".//w:t", NS)) for p in paragraphs)
    bold = []
    for run in tree.findall(".//w:body/w:p/w:r", NS):
        if any(n.text for n in run.findall("w:t", NS)):
            flag = run.find("w:rPr/w:b", NS)
            bold.append(flag is not None and flag.get("{" + NS["w"] + "}val", "1") not in ("0", "false", "off"))
    return {"text": text, "bold": bold, "sha256": hashlib.sha256(data).hexdigest()}


def ui(*args):
    result = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise RuntimeError("X11 command failed")
    return result.stdout.strip()


def verify_saved(path, expected_text, *, must_bold=False, previous_sha=None):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            state = document_state(path)
            if (state["text"] == expected_text and
                    (not must_bold or (state["bold"] and all(state["bold"]))) and
                    (previous_sha is None or state["sha256"] != previous_sha)):
                # The report contains hashes and booleans, never document text.
                return {"saved_docx_sha256": state["sha256"], "exact_text_match": True,
                        "all_text_runs_explicitly_bold": bool(state["bold"]) and all(state["bold"])}
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            pass
        time.sleep(.1)
    raise RuntimeError("saved DOCX semantic verification failed")


def run(args):
    document = args.document.resolve(strict=True)
    if args.document.is_symlink() or not args.test_copy or document.suffix.lower() != ".docx":
        raise ValueError("explicit regular DOCX test copy required")
    if document_state(document)["text"] != "":
        raise ValueError("open blank test document required")
    pids = sorted(set(args.covered_pids))
    if args.root_pid not in pids or descendant_pids(args.root_pid) != set(pids):
        raise ValueError("explicit authorized PID set differs from current WPS tree")
    identities = {pid: identity(pid) for pid in pids}
    if any((Path("/proc") / str(pid)).stat().st_uid != os.getuid() for pid in pids):
        raise ValueError("only current user's test processes are allowed")
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "linux.wps.server-smoke.v1", "status": "running",
              "source": "server-lbz-authenticated-xvfb-real-wps", "app_version": args.app_version,
              "synthetic_document": True, "is_mock": False, "formal_collection_enabled": False,
              "dataset_release_eligible": False, "model_profile": PROFILE,
              "native_sha256": hashlib.sha256(args.native.read_bytes()).hexdigest(),
              "root_pid": args.root_pid, "processes": [vars(v) for v in identities.values()],
              "scope": "three calibrated actions in one application instance; not independent accuracy evaluation",
              "verification_protocol": "saved OOXML after sampling, no verification save in observed window",
              "trials": []}

    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def check_target():
        if (ui("getactivewindow") != args.window_id or
                int(ui("getwindowpid", args.window_id)) != args.root_pid):
            raise RuntimeError("selected WPS window is not actually focused")
        if descendant_pids(args.root_pid) != set(pids) or any(identity(pid) != original for pid, original in identities.items()):
            raise RuntimeError("authorized WPS process coverage changed")

    save()
    payload = "Sample document text"
    expected = ""
    try:
        for operation_id, label in OPERATIONS:
            check_target()
            if label == "SAVE_DOCUMENT":
                ui("key", "ctrl+End"); ui("type", "--", " additional text")
                expected += " additional text"
            elif label == "FORMAT_BOLD":
                if any(document_state(document)["bold"]):
                    raise RuntimeError("bold precondition is not unformatted text")
                ui("key", "ctrl+a")
            time.sleep(1)
            check_target()
            before = document_state(document)
            event_id = uuid.uuid4().hex
            trial_dir = output / event_id; trial_dir.mkdir()
            trial = {"event_id": event_id, "operation_id": operation_id, "truth_label": label,
                     "status": "running", "semantic_passed": False, "valid_native_window": False}
            report["trials"].append(trial); save()
            config = trial_dir / "device.conf"
            config.write_text(f"app_id=wps\nmodels_root={args.models.resolve()}\nmodel_profile={PROFILE}\n"
                              f"session_id={event_id}\nroot_pid={args.root_pid}\ncovered_pids={','.join(map(str,pids))}\n"
                              f"output_jsonl={trial_dir/'native.jsonl'}\nbaseline_count=2\nbaseline_ms=5000\n"
                              "action_ms=15000\npost_ms=5000\nmax_sample_ms=1000\nfocus_poll_ms=100\n")
            stream = NativeStream([args.native.resolve(), "serve", "--config", config])
            started = time.monotonic_ns(); acted = False; event = health = None
            try:
                stream.process.stdin.write((event_id + "\t" + event_id + "\n").encode()); stream.process.stdin.flush()
                deadline = time.monotonic() + 85
                while health is None:
                    record = stream.next(deadline)
                    if record.get("schema") == "harmony.ops.stage.v1":
                        trial.setdefault("stages", []).append(record)
                        if record.get("phase") == "action":
                            if acted: raise RuntimeError("duplicate action stage")
                            tick = time.monotonic_ns(); check_target()
                            if label == "WRITE_TEXT": ui("type", "--", payload)
                            else: ui("key", "ctrl+s" if label == "SAVE_DOCUMENT" else "ctrl+b")
                            acted = True; check_target()
                            trial["action_ns"] = time.monotonic_ns() - tick
                    elif record.get("schema") == "harmony.ops.recognized.v1":
                        if (not acted or event is not None or record.get("event_id") != event_id or
                                record.get("status") != "success" or record.get("source") != "controlled_live_window"):
                            raise RuntimeError("unexpected Native event")
                        event = record
                    elif record.get("schema") == "harmony.ops.health.v1":
                        if not event or record.get("last_code") != "ok" or not record.get("last_success_timing_ns"):
                            raise RuntimeError("Native successful completion not confirmed")
                        health = record
                stream.close(normal=True)
                trial.update(valid_native_window=True, predicted_label=event["operation_label"],
                             native_timing_ns=event["timing_ns"], post_output_timing_ns=health["last_success_timing_ns"])
            except Exception as error:
                trial.update(native_error=str(error))
            finally:
                stream.close()
            trial["native_wait_ns"] = time.monotonic_ns() - started
            # Sampling has fully stopped before any verification save.
            if not acted: raise RuntimeError("Native did not reach the operation stage")
            if label == "WRITE_TEXT": expected = payload
            tick = time.monotonic_ns()
            if label != "SAVE_DOCUMENT": ui("key", "ctrl+s")
            trial["semantic_evidence"] = verify_saved(document, expected, must_bold=label == "FORMAT_BOLD", previous_sha=before["sha256"])
            trial.update(semantic_passed=True, verification_after_capture_ns=time.monotonic_ns()-tick)
            trial["status"] = "success" if trial["valid_native_window"] else "invalid_capture"
            if trial["valid_native_window"]: trial["correct"] = trial["predicted_label"] == label
            with (output / "truth.jsonl").open("a") as handle:
                handle.write(json.dumps({"event_id": event_id, "operation_id": operation_id, "operation_label": label,
                    "verification_status": "passed", "truth_source": "saved-docx-after-sampling",
                    "capture_valid": trial["valid_native_window"], "source": report["source"]}) + "\n")
            save()
        report["status"] = "smoke_complete"
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", reason=str(error))
        for trial in report["trials"]:
            if trial["status"] == "running": trial["status"] = report["status"]
    finally:
        report["semantic_passed"] = sum(t["semantic_passed"] for t in report["trials"])
        report["valid_native_windows"] = sum(t["valid_native_window"] and t["semantic_passed"] for t in report["trials"])
        report["correct_requests"] = sum(t.get("correct", False) and t["semantic_passed"] for t in report["trials"])
        report["accuracy"] = None  # A three-action smoke is not an 18-class accuracy estimate.
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=Path, required=True); parser.add_argument("--test-copy", action="store_true")
    parser.add_argument("--root-pid", type=int, required=True); parser.add_argument("--covered-pids", type=int, nargs="+", required=True)
    parser.add_argument("--window-id", required=True); parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app-version", required=True)
    args = parser.parse_args(); report = run(args)
    print(json.dumps({k: report[k] for k in ("status", "semantic_passed", "valid_native_windows", "correct_requests", "accuracy")}))
    return 0 if report["status"] == "smoke_complete" and report["valid_native_windows"] == 3 else 2


if __name__ == "__main__":
    raise SystemExit(main())
