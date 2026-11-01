"""Synthetic file observer checks. No Harmony service or device is used."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/"scripts/onsite/show_wps_result.sh"


class HarmonyDisplayTests(unittest.TestCase):
    def write_status(self,root,state,completed):
        path=root/"status.json"
        path.with_suffix(".tmp").write_text(json.dumps({"state":state,"health":{"completed_windows":completed}}))
        path.with_suffix(".tmp").replace(path)

    def begin(self,root,expected="SAVE_DOCUMENT",seconds="4"):
        p=subprocess.Popen(["sh",str(SCRIPT),str(root),expected,seconds],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        first=p.stdout.readline()
        self.assertIn("等待本次新窗口",first)
        return p,first

    def add_result(self,root,label):
        (root/"results.jsonl").write_text(json.dumps({"status":"success","app_id":"wps","event_id":"synthetic-one","operation_label":label,"timing_ns":{"trigger_to_result_ready":31234567890}})+"\n")

    def test_new_complete_result_has_label_match_and_timing(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.write_status(root,"model_ready_live_pending",0)
            p,first=self.begin(root)
            (root/"stage.jsonl").write_text('{"phase":"action","monotonic_ns":1}\n')
            stage=p.stdout.readline();self.assertIn("现在执行",stage)
            self.add_result(root,"SAVE_DOCUMENT");self.write_status(root,"window_success",1)
            out,err=p.communicate(timeout=6)
            self.assertEqual(p.returncode,0,err);self.assertIn("本次标签命中：是",out)
            self.assertIn("31.235",out)

    def test_wrong_prediction_is_kept_as_a_completed_wrong_sample(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.write_status(root,"model_ready_live_pending",0)
            p,first=self.begin(root);self.add_result(root,"WRITE_TEXT");self.write_status(root,"window_success",1)
            out,err=p.communicate(timeout=6)
            self.assertEqual(p.returncode,0,err);self.assertIn("否（保留为错误样本）",out)

    def test_old_success_and_old_action_stage_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.add_result(root,"SAVE_DOCUMENT");self.write_status(root,"window_success",1)
            (root/"stage.jsonl").write_text('{"phase":"action","monotonic_ns":1}\n')
            p,first=self.begin(root,seconds="1");out,err=p.communicate(timeout=4)
            self.assertEqual(p.returncode,2,err);self.assertIn("等待超时",out)
            self.assertNotIn("本次标签命中",out);self.assertNotIn("现在执行",out)

    def test_published_record_without_successful_completion_is_not_a_hit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.write_status(root,"model_ready_live_pending",0)
            p,first=self.begin(root);self.add_result(root,"SAVE_DOCUMENT");self.write_status(root,"window_failed",0)
            out,err=p.communicate(timeout=6)
            self.assertEqual(p.returncode,2,err);self.assertNotIn("本次标签命中",out)


if __name__=="__main__":unittest.main()
