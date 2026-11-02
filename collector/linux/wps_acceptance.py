"""Isolated WPS acceptance driver; truth is never passed to Native recognition.

RPC is used outside observation windows to prepare and inspect real documents.
The operation method is recorded explicitly: keyboard actions stay keyboard;
replace/table use the real WPS RPC API and are evaluated as that driver profile.
This module requires a separately authorized private Xvfb test session.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import uuid

from collector.linux.procfs import descendant_pids, identity
from scripts.linux_server_wps_smoke import ui

BASE = "Alpha text with target marker. Beta text."
SHORT = "Sample document text"
MEDIUM = "First paragraph contains controlled text for the test.\nSecond paragraph contains different text for the test.\nThird paragraph contains more text for the document."
LABELS = ("NEW_DOCUMENT", "WRITE_TEXT", "SAVE_DOCUMENT", "CLOSE_DOCUMENT", "SELECT_ALL",
          "COPY_SELECTION", "PASTE_SELECTION", "CUT_SELECTION", "UNDO_EDIT", "REDO_EDIT",
          "FIND_TEXT", "REPLACE_TEXT", "INSERT_PAGE_BREAK", "INSERT_TABLE", "FORMAT_BOLD",
          "FORMAT_ITALIC", "FORMAT_UNDERLINE", "ALIGN_CENTER")


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def text(document):
    value = document.Content.Text
    if not isinstance(value, str): raise RuntimeError("document text interface temporarily unavailable")
    return value.replace("\r", "\n").removesuffix("\n")


class SemanticFailure(RuntimeError):
    def __init__(self, label, evidence):
        super().__init__("operation semantic postcondition failed: " + label)
        self.evidence = evidence


class WpsTestApplication:
    def __init__(self, root):
        if os.environ.get("HARMONY_OPS_ISOLATED_TEST") != "1":
            raise ValueError("an authenticated isolated test desktop is required")
        from pywpsrpc.common import QtApp
        from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
        from pywpsrpc import RpcProxy
        self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self.api = wpsapi; self.app = None; self.owned = False; self.closed = False
        self.run_id = uuid.uuid4().hex
        os.environ["HARMONY_WPS_RUN_ID"] = self.run_id
        self.qt = QtApp(["wps-acceptance"])
        hr, self.rpc = createWpsRpcInstance()
        if hr != 0: raise RuntimeError("WPS RPC instance failed")
        before = time.monotonic_ns()
        self.rpc.setProcessPath("/opt/kingsoft/wps-office/office6/wps")
        hr, app = self.rpc.getWpsApplication()
        if hr != 0: raise RuntimeError(f"WPS RPC startup failed: {hr}")
        hr, self.pid = self.rpc.getProcessPid()
        if hr != 0: raise RuntimeError("WPS RPC did not return its process identity")
        entry = Path("/proc") / str(self.pid)
        self.original = identity(self.pid)
        ticks_ns = self.original.start_ticks * 1_000_000_000 // os.sysconf("SC_CLK_TCK")
        # RPC may daemonize/reparent. A unique inherited marker + UID + creation
        # time binds the returned PID to this launch without reading credentials.
        marker = ("HARMONY_WPS_RUN_ID=" + self.run_id).encode()
        environment = (entry / "environ").read_bytes().split(b"\0")
        if entry.stat().st_uid != os.getuid() or marker not in environment or ticks_ns + 1_000_000_000 < before:
            raise RuntimeError("RPC process is outside this newly created test scope")
        self.owned = True; self.app = RpcProxy(app, use_exception=True)
        self.app.Visible = True
        self.docs = []; self.current = None
        self.anchor = self.app.Documents.Add(); self.docs.append(self.anchor)
        self.anchor.Content.Text = "Anchor fixture for the isolated test."
        self.anchor_path = self.root / (uuid.uuid4().hex + ".docx")
        self.anchor.SaveAs2(str(self.anchor_path), FileFormat=self.api.wdFormatXMLDocument)
        self.dismiss()

    def dismiss(self):
        for _ in range(3):
            ui("key", "Escape"); ui("close-system-check", str(self.pid)); time.sleep(.15)

    def close(self):
        if self.closed: return
        self.closed = True
        self.cleanup = {"forced_signals": [], "remaining_pids": []}
        if self.owned and self.app is not None:
            selected = []
            for pid in descendant_pids(self.pid):
                try:
                    if (Path("/proc")/str(pid)).stat().st_uid == os.getuid(): selected.append(identity(pid))
                except Exception: pass
            try:
                if identity(self.pid) == self.original: self.app.Quit(self.api.wdDoNotSaveChanges)
            except Exception: pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try: identity(self.pid)
                except Exception: break
                time.sleep(.1)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for original in selected:
                    try:
                        if identity(original.pid) == original:
                            os.kill(original.pid,sig);self.cleanup["forced_signals"].append({"pid":original.pid,"signal":int(sig)})
                    except Exception: pass
                time.sleep(.2)
            for original in selected:
                try:
                    if identity(original.pid) == original: self.cleanup["remaining_pids"].append(original.pid)
                except Exception: pass

    def __del__(self):
        if getattr(self,"owned",False) and getattr(self,"app",None) is not None:
            try: self.close()
            except Exception: pass

    def focus(self):
        window = ui("getactivewindow")
        if int(ui("getwindowpid", window)) != self.pid:
            raise RuntimeError("the owned WPS application is not focused")
        return window

    def freeze_coverage(self):
        previous = None; since = time.monotonic(); until = since + 12
        while time.monotonic() < until:
            current = tuple(identity(p) for p in sorted(descendant_pids(self.pid)))
            if any((Path("/proc")/str(v.pid)).stat().st_uid != os.getuid() for v in current):
                raise RuntimeError("test tree contains an unexpected process owner")
            if current != previous: previous = current; since = time.monotonic()
            if time.monotonic() - since >= 1: return current
            time.sleep(.15)
        raise RuntimeError("test process coverage did not stabilize")

    def prepare(self, label, *, payload=SHORT, input_method="keyboard"):
        if label not in LABELS: raise ValueError("unknown catalog operation")
        self.dismiss()
        # Only documents created by this object are ever closed.
        for doc in reversed(self.docs[1:]):
            try: doc.Close(self.api.wdDoNotSaveChanges)
            except Exception: pass  # A close test may have already closed it.
        self.docs = [self.anchor]
        doc = self.app.Documents.Add(); self.docs.append(doc); self.current = doc
        doc.Content.Text = "" if label == "WRITE_TEXT" else BASE
        doc.Content.Font.Bold = 0; doc.Content.Font.Italic = 0; doc.Content.Font.Underline = 0
        doc.Content.ParagraphFormat.Alignment = self.api.wdAlignParagraphLeft
        path = self.root / (uuid.uuid4().hex + ".docx")
        doc.SaveAs2(str(path), FileFormat=self.api.wdFormatXMLDocument)
        doc.Activate(); self.dismiss()
        self.app.Selection.SetRange(len(text(doc)), len(text(doc)))
        state = {"label": label, "path": str(path), "before": text(doc), "payload": payload,
                 "input_method": input_method, "document_count": self.app.Documents.Count,
                 "before_saved_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if label in {"SELECT_ALL", "COPY_SELECTION", "CUT_SELECTION", "FORMAT_BOLD", "FORMAT_ITALIC", "FORMAT_UNDERLINE"}:
            self.app.Selection.SetRange(0, 0 if label == "SELECT_ALL" else len(BASE))
        if label in {"COPY_SELECTION", "CUT_SELECTION", "SELECT_ALL", "FIND_TEXT"}:
            ui("clear-clipboard", str(self.pid))
        if label == "PASTE_SELECTION":
            self.app.Selection.SetRange(0, 5); self.app.Selection.Copy()
            self.app.Selection.SetRange(len(BASE), len(BASE))
        if label in {"SAVE_DOCUMENT", "UNDO_EDIT", "REDO_EDIT"}:
            ui("type", "--", " delta"); time.sleep(.3)
            if label == "REDO_EDIT": ui("key", "ctrl+z"); time.sleep(.3)
        if label in {"FIND_TEXT", "REPLACE_TEXT"}: self.app.Selection.SetRange(0, 0)
        state["before"] = text(doc)
        state["selection_before"] = [self.app.Selection.Start, self.app.Selection.End]
        state["saved_before"] = bool(doc.Saved)
        if label == "WRITE_TEXT" and state["before"]: raise RuntimeError("write precondition is not blank")
        if label == "SAVE_DOCUMENT" and state["saved_before"]: raise RuntimeError("save precondition is not dirty")
        if label.startswith("FORMAT_"):
            prop = {"FORMAT_BOLD":"Bold", "FORMAT_ITALIC":"Italic", "FORMAT_UNDERLINE":"Underline"}[label]
            if getattr(self.app.Selection.Font, prop) != 0: raise RuntimeError("formatting precondition is not plain")
        self.focus()
        return state

    def action(self, state):
        label = state["label"]; self.focus()
        if label == "WRITE_TEXT":
            if state["input_method"] == "keyboard": ui("type", "--", state["payload"])
            elif state["input_method"] == "rpc": self.app.Selection.TypeText(state["payload"].replace("\n", "\r"))
            else: raise ValueError("unsupported input method")
        elif label == "REPLACE_TEXT":
            self.app.Selection.Find.Execute(FindText="target", ReplaceWith="result", Replace=self.api.wdReplaceAll, Wrap=self.api.wdFindContinue)
        elif label == "INSERT_TABLE":
            self.current.Tables.Add(self.app.Selection.Range.rpc_object, 2, 2)
        elif label == "FIND_TEXT":
            # This explicit RPC driver selects the found range. The separate
            # keyboard search-dialog driver is not silently treated as passed.
            self.app.Selection.Find.Execute(FindText="target", Forward=True, Wrap=self.api.wdFindContinue)
        else:
            keys = {"NEW_DOCUMENT":"ctrl+n", "SAVE_DOCUMENT":"ctrl+s", "CLOSE_DOCUMENT":"ctrl+w",
                    "SELECT_ALL":"ctrl+a", "COPY_SELECTION":"ctrl+c", "PASTE_SELECTION":"ctrl+v",
                    "CUT_SELECTION":"ctrl+x", "UNDO_EDIT":"ctrl+z", "REDO_EDIT":"ctrl+y",
                    "INSERT_PAGE_BREAK":"ctrl+Return", "FORMAT_BOLD":"ctrl+b", "FORMAT_ITALIC":"ctrl+i",
                    "FORMAT_UNDERLINE":"ctrl+u", "ALIGN_CENTER":"ctrl+e"}
            ui("key", keys[label])

    def verify(self, state):
        label = state["label"]; self.focus()
        doc = self.app.ActiveDocument; actual = text(doc)
        selection = self.app.Selection
        evidence = {"verification_method": "WPS-RPC-after-observation",
                    "content_sha256": digest(actual), "document_count": self.app.Documents.Count,
                    "content_characters": len(actual), "control_characters": [ord(c) for c in actual if ord(c)<32],
                    "selection": [selection.Start, selection.End], "passed": False}
        expected = state["before"]
        if label == "NEW_DOCUMENT":
            passed = self.app.Documents.Count == state["document_count"] + 1 and actual == ""
            self.docs.append(doc)
        elif label == "CLOSE_DOCUMENT":
            passed = self.app.Documents.Count == state["document_count"] - 1 and doc.FullName == str(self.anchor_path)
        elif label == "SELECT_ALL":
            passed = actual == expected and selection.Start == 0 and selection.End >= len(actual) and selection.Text.replace("\r", "\n").rstrip("\n") == actual
        elif label == "COPY_SELECTION":
            clip = ui("getclipboard", str(self.pid)).replace("\r", "\n").rstrip("\n")
            passed = actual == expected and clip == BASE
            evidence["clipboard_sha256"] = digest(clip)
        elif label == "FIND_TEXT":
            passed = actual == expected and selection.Text == "target"
        elif label == "SAVE_DOCUMENT":
            passed = actual == expected and bool(doc.Saved) and hashlib.sha256(Path(state["path"]).read_bytes()).hexdigest() != state["before_saved_sha256"]
        elif label.startswith("FORMAT_"):
            prop = {"FORMAT_BOLD":"Bold", "FORMAT_ITALIC":"Italic", "FORMAT_UNDERLINE":"Underline"}[label]
            value = getattr(selection.Font, prop)
            passed = (actual == expected and [selection.Start,selection.End] == state["selection_before"] and
                      selection.Text.replace("\r","\n").rstrip("\n") == expected and
                      (value in (-1, 1) if prop != "Underline" else value == self.api.wdUnderlineSingle))
            evidence["format_value"] = value
        elif label == "ALIGN_CENTER":
            value = selection.ParagraphFormat.Alignment
            passed = actual == expected and value == self.api.wdAlignParagraphCenter
            evidence["alignment"] = value
        elif label == "INSERT_TABLE":
            passed = doc.Tables.Count == 1 and doc.Tables.Item(1).Rows.Count == 2 and doc.Tables.Item(1).Columns.Count == 2
            evidence["tables"] = doc.Tables.Count
        elif label == "INSERT_PAGE_BREAK":
            passed = actual.count("\x0c") == 1 and actual.replace("\n", "").replace("\x0c", "") == expected.replace("\n", "")
            evidence["explicit_page_break_characters"] = actual.count("\x0c")
        else:
            desired = {"WRITE_TEXT": state["payload"], "PASTE_SELECTION": BASE+"Alpha", "CUT_SELECTION":"",
                       "UNDO_EDIT":BASE, "REDO_EDIT":BASE+" delta", "REPLACE_TEXT":BASE.replace("target","result")}[label]
            passed = actual == desired
            if label == "CUT_SELECTION":
                clip = ui("getclipboard", str(self.pid)).replace("\r", "\n").rstrip("\n")
                passed = passed and clip == BASE; evidence["clipboard_sha256"] = digest(clip)
        if not passed:
            raise SemanticFailure(label, evidence)
        evidence["passed"] = True
        # Persist only after observation; snapshots are private synthetic docs.
        if label != "CLOSE_DOCUMENT":
            destination = self.root/(uuid.uuid4().hex+".docx")
            doc.SaveAs2(str(destination), FileFormat=self.api.wdFormatXMLDocument)
            evidence["saved_docx_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        return evidence

    @staticmethod
    def method(label, input_method):
        if label in {"FIND_TEXT", "REPLACE_TEXT", "INSERT_TABLE"}: return "wps-rpc"
        if label == "WRITE_TEXT": return input_method
        return "x11-keyboard"
