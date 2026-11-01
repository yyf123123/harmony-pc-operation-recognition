"""Training leakage boundaries and saved candidate integrity, not fixture accuracy."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np

from classification.linux_pilot import CATALOG, LABELS, fit_candidate, load_intake, verify_candidate

ROOT=Path(__file__).resolve().parents[1]
MODEL=ROOT/"models/linux/wps_writer_pilot_v1"


def fixture():
    records=[];vectors=[]
    for doc in range(1,6):
        split="train" if doc<4 else "validation" if doc==4 else "test"
        for subset,size in (("independent",18),("continuous",20)):
            for i in range(size):
                label=i%18
                records.append({"sample_id":f"synthetic-{doc}-{subset}-{i}","stage":"formal","subset":subset,"split":split,
                    "document_id":f"doc-{doc}","family_id":f"family-{doc}","label":LABELS[label],"operation_id":CATALOG[label].operation_id,
                    "session_id":f"synthetic-session-{doc}-{subset}","app_instance_id":"synthetic-instance",
                    "unit_id":f"synthetic-unit-{doc}-{subset}","sequence_index":i if subset=="continuous" else 0,
                    "start_ns":i*100+1,"end_ns":i*100+50})
                vector=np.zeros(2048);vector[label]=1;vectors.append(vector)
    return records,np.asarray(vectors)


class LinuxPilotModelTests(unittest.TestCase):
    def test_holdout_and_continuous_vectors_cannot_change_fitted_parameters(self):
        records,vectors=fixture()
        centers,train,_=fit_candidate(records,vectors)
        self.assertEqual(int(train.sum()),54)
        changed=vectors.copy();changed[~train]=0;changed[~train,100]=1
        alternative,_,_=fit_candidate(records,changed)
        np.testing.assert_array_equal(centers,alternative)
        np.testing.assert_array_equal(centers[:,:18],np.eye(18))
        self.assertTrue(np.all(centers[:,18:]==0))

    def test_document_or_session_crossing_splits_is_rejected(self):
        records,vectors=fixture()
        heldout=next(i for i,r in enumerate(records) if r["split"]=="test")
        wrong=copy.deepcopy(records);wrong[heldout]["family_id"]="family-1"
        with self.assertRaisesRegex(ValueError,"family crosses"):fit_candidate(wrong,vectors)
        wrong=copy.deepcopy(records);wrong[heldout]["session_id"]=records[0]["session_id"]
        with self.assertRaisesRegex(ValueError,"session crosses"):fit_candidate(wrong,vectors)

    def test_preflight_duplicate_and_partial_continuous_session_are_rejected(self):
        records,vectors=fixture()
        wrong=copy.deepcopy(records);wrong[0]["stage"]="preflight"
        with self.assertRaisesRegex(ValueError,"preflight"):fit_candidate(wrong,vectors)
        wrong=copy.deepcopy(records);wrong[1]["sample_id"]=wrong[0]["sample_id"]
        with self.assertRaisesRegex(ValueError,"unique"):fit_candidate(wrong,vectors)
        wrong=copy.deepcopy(records);next(r for r in wrong if r["subset"]=="continuous")["sequence_index"]=19
        with self.assertRaisesRegex(ValueError,"continuous session"):fit_candidate(wrong,vectors)

    def test_intake_must_already_have_real_evidence_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"intake_report.json").write_text(json.dumps({"status":"fixture_passed","selected_formal_windows":190}))
            with self.assertRaisesRegex(ValueError,"verified"):load_intake(root)

    def test_saved_candidate_is_explicit_and_tampered_parameters_fail(self):
        model,centers=verify_candidate(MODEL)
        self.assertEqual(centers.shape,(18,2048))
        self.assertEqual(model["training_sample_count"],54)
        self.assertFalse(model["native_runtime_ready"])
        self.assertEqual(model["training_documents"],["doc-1","doc-2","doc-3"])
        with tempfile.TemporaryDirectory() as directory:
            copied=Path(directory)/"model";shutil.copytree(MODEL,copied)
            with (copied/"centers.csv").open("a") as handle:handle.write("\n")
            with self.assertRaisesRegex(ValueError,"SHA-256"):verify_candidate(copied)


if __name__=="__main__":unittest.main()
