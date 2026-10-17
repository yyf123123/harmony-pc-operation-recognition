"""Synthetic orchestration tests; no WPS/UI, real collection or accuracy claim."""
from collections import Counter
import copy
import json
from pathlib import Path
import tempfile
import unittest

from collector.linux.batch import (OPS, ROLES, SCHEMA, atomic_json, digest,
                                  exclusive_run, make_schedule, run_batch, validate_config)
from collector.linux.procfs import CaptureError
from scripts.linux_batch import main, template
from unittest.mock import patch


def config():
    return {"schema_version": SCHEMA, "batch_id": "synthetic-batch", "app_version": "fixture",
            "device_id": "fixture", "pids": [321], "root_pid": 321, "seed": 7, "max_attempts": 3,
            "build_dir": "/no-real-build", "automation_files": ["/no-real-driver"],
            "documents": [{"document_id": f"doc-{n}", "family_id": f"family-{n}", "path": f"/synthetic/{n}.docx",
                           "split": "train" if n < 3 else "validation" if n == 3 else "test",
                           "test_copy": True} for n in range(5)],
            "automation": {role: ["synthetic-driver", role, "{operation_id}", "{document_path}"] for role in ROLES},
            "workflows": [{"workflow_id": f"flow-{n}", "operations": [OPS[(i+n) % 18] for i in range(20)]} for n in range(6)]}


class FixtureCapture:
    is_mock = True
    binding = {"source": "synthetic_unit_test", "processes": [{"pid": 321, "start_ticks": 123}]}

    def __init__(self, fault=None):
        self.fault = fault
        self.calls = []
        self.prepared = []
        self.failed_once = False

    def prepare(self, role, context):
        self.prepared.append((role, dict(context)))

    def capture(self, unit, context, output):
        self.calls.append((dict(unit), dict(context)))
        if self.fault and self.fault(unit, context, self):
            raise CaptureError("synthetic controlled failure")
        (output / "derived").mkdir(parents=True)
        (output / "raw").mkdir()
        sample = {"status": "success", "is_mock": True, "feature_schema": "wps.vma.feature.v1",
                  "sample_id": digest([context["session_id"], unit["unit_id"], context["sequence_index"]]),
                  "session_id": context["session_id"], "app_instance_id": "synthetic-instance",
                  "operation_id": context["operation_id"], "collection_stage": unit["stage"],
                  "capture_context": {"unit_id": unit["unit_id"]}}
        atomic_json(output / "derived/sample.json", sample)
        (output / "raw/synthetic.txt").write_text("explicit synthetic evidence")
        return sample


class BatchScheduleTests(unittest.TestCase):
    def test_fixed_quota_and_family_isolation_with_deterministic_schedule(self):
        schedule = make_schedule(config())
        self.assertEqual(schedule, make_schedule(config()))
        self.assertEqual(len(schedule), 966)
        self.assertEqual(sum(len(unit["operations"]) for unit in schedule), 1536)
        self.assertTrue(all(unit["stage"] == "preflight" for unit in schedule[:36]))
        self.assertEqual(Counter(op for unit in schedule[:36] for op in unit["operations"]), dict.fromkeys(OPS, 2))
        independent = [u for u in schedule if u["stage"] == "formal" and u["subset"] == "independent"]
        self.assertEqual(Counter(u["operations"][0] for u in independent), dict.fromkeys(OPS, 50))
        self.assertEqual(sum(u["subset"] == "continuous" for u in schedule), 30)
        splits = Counter()
        family = {}
        for unit in schedule[36:]:
            splits[unit["split"]] += len(unit["operations"])
            self.assertEqual(family.setdefault(unit["family_id"], unit["split"]), unit["split"])
        self.assertEqual(splits, {"train": 900, "validation": 300, "test": 300})

    def test_incomplete_templates_and_untrusted_argv_cannot_execute(self):
        with self.assertRaises(ValueError): validate_config(template())
        bad = config(); bad["automation"]["action"] = "a shell string"
        with self.assertRaises(ValueError): validate_config(bad)
        bad = config(); bad["automation"]["action"].append("{unknown_token}")
        with self.assertRaises(ValueError): validate_config(bad)
        bad = config(); bad["documents"][4]["family_id"] = bad["documents"][0]["family_id"]
        with self.assertRaises(ValueError): validate_config(bad)
        bad = config(); bad["workflows"][5]["operations"] = bad["workflows"][0]["operations"]
        with self.assertRaises(ValueError): validate_config(bad)


class BatchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "batch"
        self.config = config()

    def tearDown(self): self.temporary.cleanup()

    def run_fixture(self, engine, resume=False):
        return run_batch(self.config, self.root, resume=resume, capture=engine, fixture_mode=True)

    def test_full_batch_and_idempotent_resume_never_claim_real_dataset(self):
        engine = FixtureCapture()
        report = self.run_fixture(engine)
        self.assertEqual(report["status"], "fixture_passed")
        self.assertFalse(report["dataset_ready"])
        self.assertFalse(report["formal_collection_enabled"])
        self.assertEqual(report["valid_windows"], {"preflight": 36, "independent": 900, "continuous": 600})
        self.assertEqual(report["formal_split_counts"], {"train": 900, "validation": 300, "test": 300})
        self.assertEqual(len(engine.calls), 1536)
        self.assertEqual(Counter(role for role, _ in engine.prepared), {"prepare_independent": 936, "begin_session": 30})
        resumed = FixtureCapture()
        self.assertEqual(self.run_fixture(resumed, True)["completed_units"], 966)
        self.assertEqual(resumed.calls, [])
        self.assertEqual(resumed.prepared, [])

    def test_failed_pilot_cannot_open_formal_and_modified_input_cannot_resume(self):
        engine = FixtureCapture(lambda u, c, e: len(e.calls) == 8)
        report = self.run_fixture(engine)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["preflight_passed"])
        self.assertEqual(report["valid_windows"], {"preflight": 7})
        self.assertFalse(any(u["stage"] == "formal" for u, _ in engine.calls))
        changed = FixtureCapture(); changed.binding = {"source": "changed"}
        with self.assertRaisesRegex(ValueError, "changed"):
            self.run_fixture(changed, True)
        self.assertFalse(json.loads((self.root / "report.json").read_text())["dataset_ready"])

    def test_continuous_prefix_is_not_counted_and_retry_starts_new_session(self):
        def fail_once(unit, context, engine):
            if unit["subset"] == "continuous" and context["sequence_index"] == 4 and not engine.failed_once:
                engine.failed_once = True
                return True
            return False
        engine = FixtureCapture(fail_once)
        report = self.run_fixture(engine)
        self.assertEqual(report["status"], "fixture_passed")
        self.assertEqual(report["failed_attempts"], 1)
        sessions = [context for unit, context in engine.calls if unit["subset"] == "continuous"]
        self.assertNotEqual(sessions[0]["session_id"], sessions[5]["session_id"])
        self.assertEqual(sessions[5]["sequence_index"], 0)
        self.assertEqual(len(sessions), 605)
        self.assertEqual(report["valid_windows"]["continuous"], 600)

    def test_corrupt_or_unhashed_evidence_refuses_resume_and_invalidates_readiness(self):
        engine = FixtureCapture(lambda u, c, e: len(e.calls) == 2)
        self.run_fixture(engine)
        raw = next(self.root.glob("attempts/*/try-*/sample-*/raw/synthetic.txt"))
        raw.write_text("modified evidence")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.run_fixture(FixtureCapture(), True)
        self.assertEqual(json.loads((self.root / "report.json").read_text())["status"], "resume_validation_failed")

    def test_truncated_journal_and_concurrent_runner_are_explicit_failures(self):
        self.run_fixture(FixtureCapture(lambda u, c, e: True))
        with exclusive_run(self.root):
            with self.assertRaisesRegex(RuntimeError, "another runner"):
                self.run_fixture(FixtureCapture(), True)
        with (self.root / "journal.jsonl").open("ab") as handle: handle.write(b'{"truncated":')
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.run_fixture(FixtureCapture(), True)

    def test_injected_capture_cannot_claim_production_source(self):
        with self.assertRaisesRegex(ValueError, "fixture"):
            run_batch(self.config, self.root, capture=FixtureCapture())
        self.assertFalse(json.loads((self.root / "report.json").read_text())["dataset_ready"])

    def test_disk_estimate_blocks_formal_after_successful_pilot(self):
        class Disk: free = 0
        engine = FixtureCapture()
        with patch("collector.linux.batch.shutil.disk_usage", return_value=Disk()):
            report = self.run_fixture(engine)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(engine.calls), 36)
        self.assertTrue(report["preflight_passed"])
        self.assertFalse(report["disk_estimate"]["passed"])
        self.assertFalse(report["formal_collection_enabled"])

    def test_abandoned_attempt_resumes_with_fresh_directory_without_double_counting(self):
        self.run_fixture(FixtureCapture(lambda u,c,e: len(e.calls)==2))
        journal=self.root/"journal.jsonl"
        lines=journal.read_bytes().splitlines(keepends=True)
        self.assertEqual(json.loads(lines[-1])["event"],"failed")
        journal.write_bytes(b"".join(lines[:-1]))  # simulated crash after started, no terminal record
        resumed=FixtureCapture()
        report=self.run_fixture(resumed,True)
        self.assertEqual(report["status"],"fixture_passed")
        self.assertEqual(len(resumed.calls),1535)
        self.assertEqual(report["failed_attempts"],1)
        self.assertIn('"event": "abandoned"',journal.read_text())


if __name__ == "__main__": unittest.main()
