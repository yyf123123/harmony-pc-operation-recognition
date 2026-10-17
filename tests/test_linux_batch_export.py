"""End-to-end fixture intake. Synthetic predictions are deliberately never scored."""
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.wps.adapter import WpsAdapter
from collector.linux.batch import OPS, RealCapture, atomic_json, file_sha, make_schedule, run_batch
from collector.linux.batch_export import export_batch
from collector.linux.history import event_from_prediction
from collector.linux.procfs import CaptureError
from tests.test_linux_batch import FixtureCapture, config
from tests.test_linux_collection import FakeProcfs


class RichFixture(FixtureCapture):
    def __init__(self):
        super().__init__()
        self.clock = 1

    def capture(self, unit, context, output):
        sample = super().capture(unit, context, output)
        op = WpsAdapter().catalog.get(sample["operation_id"])
        self.clock += 30_000_000_000
        sample.update(trial_id=context["trial_id"], operation_label=op.action_spec["legacy_label"], label_id=OPS.index(op.operation_id),
                      source="synthetic_batch_export_unit_test", app_version="fixture", page_size_bytes=4096,
                      capture_profile={"name": "linux.wps.fixed_windows.v1", "baseline_count": 2, "baseline_s": 5, "action_s": 15, "post_s": 5},
                      operation_windows=[{"end_monotonic_ns": self.clock}],
                      capture_context={"unit_id": unit["unit_id"], "sequence_index": context["sequence_index"]})
        atomic_json(output/"derived/sample.json", sample)
        export = output/"export"; export.mkdir()
        with (export/"native_vectors.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id"]+[f"v{i:04d}" for i in range(2048)])
            writer.writerow([sample["sample_id"],1]+[0]*2047)
        prediction = {"sample_id": sample["sample_id"], "operation_label": op.action_spec["legacy_label"], "cosine_similarity": "0.5"}
        with (output/"prediction.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(prediction)); writer.writeheader(); writer.writerow(prediction)
        atomic_json(output/"recognition.json", event_from_prediction(sample, prediction))
        return sample


class BatchExportTests(unittest.TestCase):
    def test_complete_fixture_exports_grouped_data_but_never_real_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root/"batch", root/"dataset"
            report = run_batch(config(), source, capture=RichFixture(), fixture_mode=True)
            self.assertEqual(report["status"], "fixture_passed")
            with self.assertRaisesRegex(ValueError, "synthetic"):
                export_batch(source, target)
            result = export_batch(source, target, allow_fixture=True)
            self.assertEqual(result["formal_windows"], 1500)
            self.assertEqual(result["continuous_sessions"], 30)
            self.assertEqual(result["metrics_scope"], "not_scored_synthetic")
            self.assertEqual(result["groups"]["independent/train"]["windows"], 540)
            self.assertNotIn("accuracy", result["groups"]["independent/train"])
            self.assertEqual(len(list((target/"sequences").glob("*/recognition.jsonl"))), 30)
            with (target/"independent/test/dataset_manifest.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["family_id"] for row in rows}, {"family-4"})
            self.assertTrue(all(row["is_mock"] == "true" and row["collection_stage"] == "formal" for row in rows))
            with self.assertRaises(FileExistsError): export_batch(source, target, allow_fixture=True)
            # Even if a recognition artifact is rehashed, using truth instead of
            # its Native label is rejected by the semantic join validator.
            index = json.loads((source/"accepted.json").read_text())
            receipt = next(r for r in index["units"] if r["stage"] == "formal")
            event_name = next(n for n in receipt["files"] if n.endswith("recognition.json"))
            event_file = source/event_name
            event = json.loads(event_file.read_text()); event["operation_id"] = "wps.invalid"
            atomic_json(event_file,event); receipt["files"][event_name] = file_sha(event_file)
            atomic_json(source/"accepted.json",index)
            journal=source/"journal.jsonl"
            records=[json.loads(line) for line in journal.read_text().splitlines()]
            for record in records:
                if record["event"]=="passed" and record["unit_id"]==receipt["unit_id"]:
                    record["files"][event_name]=receipt["files"][event_name]
            journal.write_text("".join(json.dumps(record)+"\n" for record in records))
            with self.assertRaisesRegex(ValueError,"Native prediction"):
                export_batch(source,root/"corrupt",allow_fixture=True)
            self.assertFalse((root/"corrupt").exists())

    def test_real_capture_adapter_calls_shared_native_math_with_explicit_mock_observations(self):
        repo = Path(__file__).resolve().parents[1]
        build = Path(os.environ.get("NATIVE_BUILD_DIR",repo/"build/native")).resolve()
        if not (build/"native/operation_predict").is_file(): self.skipTest("build Native targets first")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            engine=object.__new__(RealCapture)
            engine.config=config(); engine.repo=repo
            engine.backend=FakeProcfs(root,[])
            engine.native=build/"native/operation_predict"; engine.feature=build/"native/operation_feature"
            engine.inputs={}
            unit=next(u for u in make_schedule(config()) if u["operations"] == ["wps.writer.write_text"])
            context={"operation_id":"wps.writer.write_text","session_id":"fixture-session","trial_id":"fixture-trial",
                     "document_path":"/synthetic/document","document_id":unit["document_id"],"state_dir":str(root/"state"),"sequence_index":0}
            def command(argv, timeout, name, **kwargs):
                return json.dumps({"pid":321,"window_id":"77","source":"synthetic_focus"}) if name=="focus" else ""
            with patch("collector.linux.session.run_command",side_effect=command), patch("collector.linux.session.time.sleep"):
                sample=engine.capture(unit,context,root/"sample")
            self.assertTrue(sample["is_mock"])
            self.assertEqual(sample["collection_stage"],"preflight")
            self.assertTrue((root/"sample/export/native_vectors.csv").is_file())
            event=json.loads((root/"sample/recognition.json").read_text())
            self.assertTrue(event["is_mock"])
            self.assertEqual(event["source_kind"],"recognition")

    def test_two_batch_adapters_cannot_claim_the_same_process(self):
        engine=object.__new__(RealCapture)
        class Backend:
            identities=FakeProcfs.identities
            def check(self): pass
        engine.backend=Backend(); engine.inputs={}
        with tempfile.TemporaryDirectory() as directory:
            locks=Path(directory)/"locks"
            with engine.claim_targets(locks):
                with self.assertRaisesRegex(CaptureError,"another batch"):
                    with engine.claim_targets(locks): pass


if __name__ == "__main__": unittest.main()
