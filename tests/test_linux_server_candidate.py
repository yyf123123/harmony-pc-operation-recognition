"""Training isolation tests; synthetic fixtures never count as WPS evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.linux_server_candidate import load_rows, fit, evaluate


def fixture(root, instance="synthetic-instance", version="synthetic-version"):
    root.mkdir();token="a"*32;folder=root/token;folder.mkdir()
    vector=folder/"vector.json";vector.write_text(json.dumps([1.0]+[0.0]*2047)+"\n")
    result={"status":"replay_verified","event_id":token,"truth":"WRITE_TEXT","semantic_passed":True,
            "native_capture_valid":True,"vector_sha256":hashlib.sha256(vector.read_bytes()).hexdigest(),
            "raw_sha256":"synthetic-raw","action_method":"keyboard"}
    (root/"analysis.json").write_text(json.dumps({"schema":"linux.wps.raw-replay-audit.v1","app_version":version,
                                               "app_instance_id":instance,"results":[result]}))
    return token


class CandidateIsolationTests(unittest.TestCase):
    def test_tampered_vector_is_rejected_before_fitting(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);token=fixture(root/"audit")
            (root/"audit"/token/"vector.json").write_text("[]")
            with self.assertRaisesRegex(ValueError,"hash"):load_rows([root/"audit"])

    def test_repeated_sample_and_incomplete_catalog_cannot_fit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root/"audit")
            with self.assertRaisesRegex(ValueError,"duplicate"):load_rows([root/"audit",root/"audit"])
            with self.assertRaisesRegex(ValueError,"18 classes"):fit([root/"audit"],root/"model-output")
            self.assertFalse((root/"model-output").exists())

    def test_same_application_instance_cannot_be_called_heldout(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root/"audit")
            model=root/"candidate/model";model.mkdir(parents=True)
            (model/"model.json").write_text(json.dumps({"wps_version":"synthetic-version"}))
            (root/"candidate/training_manifest.json").write_text(json.dumps({"instances":["synthetic-instance"],"samples":[]}))
            with self.assertRaisesRegex(ValueError,"instance overlap"):
                evaluate(root/"candidate",[root/"audit"],root/"eval")
            self.assertFalse((root/"eval").exists())


if __name__=="__main__":unittest.main()
