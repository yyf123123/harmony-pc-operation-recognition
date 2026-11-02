"""Manual UI protocol faults; synthetic records never count as WPS accuracy."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.linux_manual_demo import PHASES, annotate, consume_window, feedback, save_report


class Stream:
    def __init__(self, records):self.records=iter(records)
    def next(self, deadline):return next(self.records)


def records():
    return [{"schema":"harmony.ops.stage.v1","phase":p,"index":i} for p,i in PHASES]+[
        {"schema":"harmony.ops.recognized.v1","event_id":"synthetic","status":"success",
         "source":"controlled_live_window","operation_label":"WRITE_TEXT"},
        {"schema":"harmony.ops.health.v1","last_code":"ok","completed_windows":1,
         "last_success_timing_ns":{"trigger_to_history_updated":30_000_000_000}}]


class ManualProtocolTests(unittest.TestCase):
    def test_truth_is_not_requested_during_capture(self):
        trial={}; seen=[]
        event,health=consume_window(Stream(records()),"synthetic",trial,seen.append)
        self.assertEqual(len(seen),4)
        self.assertNotIn("reported_label",trial)
        self.assertEqual(event["operation_label"],"WRITE_TEXT")

    def test_old_event_or_wrong_phase_cannot_be_reused(self):
        bad=records();bad[-2]["event_id"]="previous"
        with self.assertRaises(RuntimeError):consume_window(Stream(bad),"synthetic",{},lambda r:None)
        bad=records();bad[1]["index"]=0
        with self.assertRaises(RuntimeError):consume_window(Stream(bad),"synthetic",{},lambda r:None)

    def test_result_without_successful_history_is_not_a_valid_capture(self):
        bad=records();bad[-1]["last_success_timing_ns"]=None
        with self.assertRaises(RuntimeError):consume_window(Stream(bad),"synthetic",{},lambda r:None)

    def test_wrong_prediction_is_kept_as_a_wrong_reported_sample(self):
        trial={"capture_valid":True,"predicted_label":"SAVE_DOCUMENT"}
        annotate(trial,"2")
        self.assertFalse(trial["self_reported_correct"])
        self.assertFalse(trial["semantic_verified"])

    def test_multiple_noop_unknown_and_failed_capture_do_not_invent_accuracy(self):
        for answer in ("m","n","f","o",""):
            trial={"capture_valid":True,"predicted_label":"WRITE_TEXT"};annotate(trial,answer)
            self.assertIsNone(trial["self_reported_correct"])
        trial={"capture_valid":False};annotate(trial,"2")
        self.assertIsNone(trial["self_reported_correct"])

    def test_feedback_includes_failed_attempts_but_excludes_documents_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            report={"trials":[{"capture_valid":False,"reason":"synthetic failure"}]}
            save_report(root,report)
            (root/"test.docx").write_text("never return document content")
            (root/"private.conf").write_text("local paths")
            archive=feedback(root)
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(set(z.namelist()),{"report.json","comparison.csv"})
                returned=json.loads(z.read("report.json"))
                self.assertEqual(returned["total_attempts"],1)
                self.assertEqual(returned["valid_windows"],0)
                self.assertIsNone(returned["self_reported_agreement"])


if __name__=="__main__":unittest.main()
