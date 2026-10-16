"""Small verified WPS/X11 smoke primitives; all other catalog actions stay gated.

This module never launches/kills an application, switches the active window,
replaces clipboard content, or deletes a file. Run it on a disposable test copy.
AT-SPI document text and selection are required where the action needs them;
keyboard delivery alone is never considered operation success.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile

from apps.wps.adapter import WpsAdapter
from collector.linux.procfs import identity as process_identity

SUPPORTED = {"wps.writer.write_text", "wps.writer.save_document", "wps.writer.format_bold"}


def operation_readiness():
    return [{"operation_id": operation.operation_id,
             "implementation_status": "smoke_primitive_ready" if operation.operation_id in SUPPORTED else "desktop_adaptation_pending",
             "desktop_validation_status": "pending", "formal_collection_enabled": False,
             "precondition": operation.precondition,
             "success_evidence": {"wps.writer.write_text": "AT-SPI exact document text and caret at end",
                                  "wps.writer.save_document": "changed target DOCX fingerprint and exact saved document text",
                                  "wps.writer.format_bold": "AT-SPI selected text and every selected run has bold weight"}.get(
                                      operation.operation_id, "target desktop semantic verifier required")}
            for operation in WpsAdapter().catalog.operations]


def _run(argv):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=5, check=False)
    if result.returncode:
        raise ValueError("desktop command failed")
    return result.stdout.strip()


def focus(config):
    if platform.system() != "Linux" or not os.environ.get("DISPLAY") or not shutil.which("xdotool"):
        raise ValueError("Linux X11 DISPLAY and xdotool are required")
    identity = process_identity(config["pid"])
    if identity.start_ticks != config["start_ticks"]:
        raise ValueError("process start identity changed")
    window = _run(["xdotool", "getactivewindow"])
    pid = int(_run(["xdotool", "getwindowpid", window]))
    if pid != config["pid"] or window != config["window_id"]:
        raise ValueError("selected WPS test window is not focused")
    return {"pid": pid, "window_id": window, "source": "xdotool-active-window-pid"}


def _text_node(config):
    try:
        import pyatspi
    except ImportError as error:
        raise ValueError("AT-SPI Python bindings unavailable; semantic verification cannot run") from error
    desktop = pyatspi.Registry.getDesktop(0)
    applications = [node for node in desktop if int(node.get_process_id()) == config["pid"]]
    if len(applications) != 1:
        raise ValueError("AT-SPI cannot uniquely identify selected WPS process")
    queue = [(applications[0], 0, False)]
    visited = 0
    while queue:
        node, depth, in_document = queue.pop(0)
        visited += 1
        if visited > 5000 or depth > 30:
            raise ValueError("accessibility tree exceeds bounded traversal")
        role = node.getRoleName().lower()
        in_document = in_document or role.startswith("document")
        if in_document and node.getState().contains(pyatspi.STATE_FOCUSED):
            try:
                text = node.queryText()
                # Require a real editable document text interface, not toolbar or search text.
                if node.getState().contains(pyatspi.STATE_EDITABLE):
                    return text
            except NotImplementedError:
                pass
        for child in node:
            if child is not None:
                queue.append((child, depth + 1, in_document))
    raise ValueError("focused editable AT-SPI document text is unavailable")


def _selection(text):
    if text.getNSelections() != 1:
        raise ValueError("one controlled text selection is required")
    start, end = text.getSelection(0)
    if not 0 <= start < end <= text.characterCount:
        raise ValueError("invalid document selection")
    return start, end


def _bold_states(text, start, end):
    states = []
    offset = start
    while offset < end:
        attributes, run_start, run_end = text.getAttributes(offset)
        if isinstance(attributes, str):
            attributes = attributes.split(";")
        parsed = dict(item.split(":", 1) for item in attributes if ":" in item)
        weight = parsed.get("weight", parsed.get("font-weight", ""))
        if not (weight.lower() in ("bold", "normal") or weight.isdigit()):
            raise ValueError("accessibility text weight is unavailable")
        states.append(weight.lower() == "bold" or (weight.isdigit() and int(weight) >= 700))
        if run_end <= offset:
            raise ValueError("invalid accessibility attribute run")
        offset = min(run_end, end)
    return states


def _all_bold(text, start, end):
    return all(_bold_states(text, start, end))


def _fingerprint(path):
    stat = path.stat()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns}


def _docx_text(path):
    with zipfile.ZipFile(path) as document:
        tree = ET.fromstring(document.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    return "\n".join("".join(node.text or "" for node in paragraph.findall(".//w:t", ns))
                     for paragraph in tree.findall(".//w:body/w:p", ns))


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("schema_version") != "linux.wps.desktop-smoke.v1" or config.get("test_copy") is not True:
        raise ValueError("explicit disposable test-copy config is required")
    for field in ("pid", "start_ticks"):
        if type(config.get(field)) is not int or config[field] <= 0:
            raise ValueError("explicit PID/start identity required")
    if not isinstance(config.get("window_id"), str) or not config["window_id"].isdigit():
        raise ValueError("explicit X11 window ID required")
    operation = config["operation_id"]
    WpsAdapter().catalog.get(operation)
    if operation not in SUPPORTED:
        raise ValueError("operation has no Linux semantic verifier yet; formal batch is disabled")
    for field in ("expected_before", "expected_after"):
        if not isinstance(config.get(field), str):
            raise ValueError("known test-copy document expectations required")
    if operation in ("wps.writer.save_document", "wps.writer.format_bold") and config["expected_after"] != config["expected_before"]:
        raise ValueError("save/format smoke must preserve the controlled document text")
    if operation == "wps.writer.write_text":
        if config["expected_before"] != "":
            raise ValueError("frozen write_text precondition requires a blank document")
        payload = config.get("payload")
        if (not isinstance(payload, str) or not payload or len(payload) > 512 or "\n" in payload
                or config["expected_after"] != config["expected_before"] + payload):
            raise ValueError("write smoke needs a bounded single-line payload appended at document end")
    if operation == "wps.writer.save_document":
        original = Path(config["document_copy"]).expanduser()
        if original.is_symlink():
            raise ValueError("save smoke does not accept a symlink target")
        path_ = original.resolve(strict=True)
        if path_.suffix.lower() != ".docx" or not path_.is_file():
            raise ValueError("save smoke requires an existing regular DOCX test copy")
        config["document_copy"] = str(path_)
    return config


def precondition(config, state_path):
    focus(config)
    text = _text_node(config)
    if text.getText(0, -1) != config["expected_before"]:
        raise ValueError("document text differs from controlled precondition")
    operation = config["operation_id"]
    state = {"schema_version": "linux.wps.smoke-state.v1", "operation_id": operation,
             "pid": config["pid"], "start_ticks": config["start_ticks"],
             "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()}
    if operation == "wps.writer.write_text":
        if text.getNSelections() or text.caretOffset != text.characterCount:
            raise ValueError("write smoke requires no selection and caret at document end")
    elif operation == "wps.writer.format_bold":
        start, end = _selection(text)
        if any(_bold_states(text, start, end)):
            raise ValueError("bold smoke requires a selected non-bold controlled text range")
        state.update(selection_start=start, selection_end=end)
    else:
        state["document_fingerprint"] = _fingerprint(Path(config["document_copy"]))
        if _docx_text(Path(config["document_copy"])) == config["expected_after"]:
            raise ValueError("save smoke requires controlled unsaved document changes")
    with Path(state_path).open("x", encoding="utf-8") as handle:
        json.dump(state, handle)


def action(config):
    focus(config)
    if config["operation_id"] == "wps.writer.write_text":
        _run(["xdotool", "type", "--clearmodifiers", "--delay", "10", "--", config["payload"]])
    else:
        key = "ctrl+s" if config["operation_id"] == "wps.writer.save_document" else "ctrl+b"
        _run(["xdotool", "key", "--clearmodifiers", key])
    focus(config)


def verify(config, state_path):
    focus(config)
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    if state.get("config_sha256") != hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest():
        raise ValueError("smoke precondition state belongs to another config")
    deadline = time.monotonic() + 4
    while True:
        text = _text_node(config)
        success = text.getText(0, -1) == config["expected_after"]
        if config["operation_id"] == "wps.writer.format_bold":
            start, end = _selection(text)
            success = success and (start, end) == (state["selection_start"], state["selection_end"]) and _all_bold(text, start, end)
        elif config["operation_id"] == "wps.writer.save_document":
            path_ = Path(config["document_copy"])
            success = success and _fingerprint(path_) != state["document_fingerprint"] and _docx_text(path_) == config["expected_after"]
        if success:
            return
        if time.monotonic() >= deadline:
            raise ValueError("semantic success check failed")
        time.sleep(0.1)
