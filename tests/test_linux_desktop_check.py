"""Finite desktop wrapper safety; fixtures never count as real WPS validation."""
import json
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

from scripts.linux_desktop_check import NativeStream,action_stage,create_feedback,check,PROFILE,QUICK
from tests.test_linux_batch import config


class DesktopCheckTests(unittest.TestCase):
    def test_finite_flow_isolates_truth_sessions_and_counts_wrong_predictions(self):
        streams=[]
        class FixtureStream:
            error=None
            def __init__(self,argv):
                self.process=type("Process",(),{"stdin":io.BytesIO()})()
                self.index=0;self.closed=False;streams.append(self)
                self.device=Path(argv[-1]).read_text()
            def next(self,deadline):
                event_id=self.process.stdin.getvalue().decode().split("\t")[0]
                self.index+=1
                if self.index==1:return {"schema":"harmony.ops.stage.v1","phase":"action","monotonic_ns":time.monotonic_ns()}
                if self.index==2:return {"schema":"harmony.ops.recognized.v1","event_id":event_id,"status":"success","model_profile":PROFILE,
                    "source":"controlled_live_window","is_mock":False,"operation_label":"WRITE_TEXT","phases":[],
                    "timing_ns":{"feature":100,"inference":50}}
                return {"schema":"harmony.ops.health.v1","last_code":"ok","model_load_ns":30,
                        "last_success_timing_ns":{"output":20,"history":10,"trigger_to_history_updated":1000}}
            def close(self,normal=False):self.closed=True
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);native=root/"fixture-native";native.write_text("synthetic")
            document=root/"fixture.docx";document.write_text("synthetic")
            cfg=config();cfg["documents"][0]["path"]=str(document)
            cfg["automation"]["prepare_independent"].append("{state_dir}")
            with patch("scripts.linux_desktop_check.sys.platform","linux"),patch("scripts.linux_desktop_check.NativeStream",FixtureStream),patch("scripts.linux_desktop_check.run_command") as command:
                report=check(cfg,native,root,root/"out",operations=QUICK[:2])
            self.assertEqual(report["status"],"passed")
            self.assertEqual(report["completed_valid_windows"],2)
            self.assertEqual(report["accuracy"],.5)
            self.assertFalse(report["formal_collection_enabled"])
            self.assertTrue(all(s.closed for s in streams))
            self.assertNotEqual(streams[0].process.stdin.getvalue(),streams[1].process.stdin.getvalue())
            self.assertTrue(all(PROFILE in s.device and not any(op in s.device for op in QUICK) for s in streams))
            for call in command.call_args_list:
                if call.args[2]=="prepare_independent":self.assertTrue(any("automation-state" in str(arg) for arg in call.args[0]))
            with zipfile.ZipFile(report["feedback_zip"]) as archive:
                self.assertEqual(len(archive.read("truth.jsonl").splitlines()),2)

    def test_feedback_does_not_include_documents_semantic_text_or_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);operation=root/"operation-00";operation.mkdir()
            (root/"report.json").write_text('{"status":"fixture_failed"}')
            (operation/"working.docx").write_text("synthetic document body")
            (operation/"semantic.json").write_text("synthetic semantic body")
            (operation/"native.jsonl").write_text('{"status":"error","code":"fixture"}\n')
            (root/"truth.jsonl").symlink_to(operation/"working.docx")
            create_feedback(root)
            with zipfile.ZipFile(root/"feedback.zip") as archive:
                self.assertEqual(set(archive.namelist()),{"report.json","native-details/operation-00.jsonl"})
    def test_expired_action_window_executes_no_automation(self):
        with patch("scripts.linux_desktop_check.run_command") as command:
            with self.assertRaises(TimeoutError):action_stage(config(),{}, {"monotonic_ns":time.monotonic_ns()-20_000_000_000})
            command.assert_not_called()

    def test_action_failure_never_runs_a_success_verifier(self):
        context={"operation_id":"wps.writer.write_text","document_id":"synthetic","document_path":"/synthetic","state_dir":"/synthetic","session_id":"s","trial_id":"t"}
        with patch("scripts.linux_desktop_check.run_command",side_effect=RuntimeError("synthetic failure")) as command:
            with self.assertRaisesRegex(RuntimeError,"synthetic"):action_stage(config(),context,{"monotonic_ns":time.monotonic_ns()})
            self.assertEqual(command.call_count,1)

    def test_native_error_and_incomplete_stream_cannot_be_success(self):
        program='import sys,json;print(json.dumps({"status":"error","code":"synthetic_error"}),flush=True)'
        stream=NativeStream([sys.executable,"-u","-c",program])
        try:
            with self.assertRaisesRegex(RuntimeError,"synthetic_error"):stream.next(time.monotonic()+3)
            self.assertEqual(stream.error["code"],"synthetic_error")
        finally:stream.close()
        stream=NativeStream([sys.executable,"-u","-c","pass"])
        try:
            with self.assertRaisesRegex(RuntimeError,"before a complete"):stream.next(time.monotonic()+3)
        finally:stream.close()

    def test_normal_stop_drains_history_larger_than_a_linux_pipe(self):
        program='import sys;line=sys.stdin.readline();print("x"*200000,flush=True)'
        stream=NativeStream([sys.executable,"-u","-c",program])
        stream.close(normal=True)
        self.assertEqual(stream.process.returncode,0)


if __name__=="__main__":unittest.main()
