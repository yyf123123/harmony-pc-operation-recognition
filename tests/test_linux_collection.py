"""Linux adapter contracts. Synthetic fixtures are unit tests, not accuracy data."""

import copy
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector.linux.procfs import CaptureError, ProcessIdentity, LinuxProcfs, identity, parse_compact, descendant_pids
from collector.linux.session import capture_sample, validate_plan
from collector.runtime.trial_state import TrialState
from scripts.linux import export_sample


HEADER = "pid\tprocess_name\tsegment\tperms\treferenced_kib\treferenced_pages\tpathname\n"


class FakeProcfs:
    identities = (ProcessIdentity(321, 987),)
    pids = {321}
    source = "mock-linux-procfs-unit-test"
    is_mock = True

    def __init__(self, root, events):
        self.collector = root / "fixture-collector"
        self.collector.write_text("not an executable; unit-test hash fixture")
        self.events = events
        self.index = 0

    def check(self):
        self.events.append("check")

    def clear(self):
        self.events.append("clear")

    def sample(self, destination, page_size):
        self.events.append("sample")
        pages = (1, 3, 8, 4)[self.index]
        self.index += 1
        destination.write_text("synthetic raw evidence")
        return {"report_paths": [str(destination)], "feature_meta": {},
                "feature_pages": {"FILE\t/usr/lib/fixture.so|segment=.so|perms=r-xp": pages,
                                  "ANON\t[heap]|segment=native heap|perms=rw-p": pages * pages}}


class LinuxCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = []
        self.backend = FakeProcfs(self.root, self.events)
        self.plan = {"schema_version": "linux.wps.action-plan.v1", "operation_id": "wps.writer.write_text",
                     "precondition": ["fixture-precondition"], "action": ["fixture-action"],
                     "verify": ["fixture-verify"], "focus": ["fixture-focus"],
                     "baseline_s": 1, "action_s": 1, "post_s": 1}

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, argv, timeout, name, **kwargs):
        self.events.append(name)
        return json.dumps({"pid": 321, "window_id": "77", "source": "mock-focus-unit-test"}) if name == "focus" else ""

    def capture(self, output=None):
        with patch("collector.linux.session.run_command", side_effect=self.command), patch("collector.linux.session.time.sleep"):
            return capture_sample(plan=self.plan, backend=self.backend, output=output or self.root / "capture",
                                  session_id="test-session", trial_id="test-trial", app_version="unit-test", device_id="unit-test")

    def test_full_protocol_keeps_action_evidence_and_replays_frozen_features(self):
        sample = self.capture()
        self.assertTrue(sample["is_mock"])
        self.assertEqual(sample["operation_label"], "WRITE_TEXT")
        self.assertEqual(self.events.count("clear"), 4)
        start = self.events.index("action")
        end = self.events.index("sample", start)
        self.assertNotIn("clear", self.events[start:end])
        self.assertIn("action verification", self.events[start:end])
        status = json.loads((self.root / "capture/validation/status.json").read_text())
        self.assertEqual(status["states"][-1], "SAVE")
        self.assertEqual(str(TrialState.ACTION_CONFIRM), "ACTION_CONFIRM")
        export_sample(sample, self.root / "export")
        with (self.root / "export/vma_vectors_l2.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 2049)
        with (self.root / "export/aggregated_features.csv").open() as handle:
            features = list(csv.DictReader(handle))
        file_row = next(row for row in features if row["namespace"] == "FILE")
        self.assertEqual(float(file_row["pages"]), 6)
        with (self.root / "export/dataset_manifest.csv").open() as handle:
            manifest = next(csv.DictReader(handle))
        self.assertEqual(manifest["is_mock"], "true")
        self.assertIn("linux.wps.fixed_windows.v1", manifest["capture_profile"])
        with self.assertRaises(FileExistsError):
            export_sample(sample, self.root / "export")

    def test_batch_root_instance_and_stage_are_metadata_only(self):
        self.backend.root_pid=321
        with patch("collector.linux.session.run_command",side_effect=self.command), patch("collector.linux.session.time.sleep"):
            sample=capture_sample(plan=self.plan,backend=self.backend,output=self.root/"batch-sample",
                session_id="s",trial_id="t",app_version="fixture",device_id="fixture",
                collection_stage="preflight",capture_context={"unit_id":"synthetic-unit"})
        self.assertEqual(sample["app_instance_id"],"wps:321:987")
        self.assertEqual(sample["collection_stage"],"preflight")
        export_sample(sample,self.root/"batch-export")
        with (self.root/"batch-export/dataset_manifest.csv").open() as handle:
            self.assertEqual(next(csv.DictReader(handle))["collection_stage"],"preflight")

    def test_verification_failure_is_retained_without_a_success_sample(self):
        original = self.command
        def fail(argv, timeout, name, **kwargs):
            if name == "action verification":
                raise CaptureError("verification failed")
            return original(argv, timeout, name, **kwargs)
        self.command = fail
        with self.assertRaises(CaptureError):
            self.capture()
        status = json.loads((self.root / "capture/validation/status.json").read_text())
        self.assertEqual(status["states"][-1], "FAILED")
        self.assertEqual(len(status["windows"]), 3)
        self.assertFalse((self.root / "capture/operation_window_sequences.jsonl").exists())

    def test_other_application_focus_rejects_the_sample(self):
        self.command = lambda *args, **kwargs: json.dumps({"pid": 999, "window_id": "77", "source": "mock"})
        with self.assertRaisesRegex(CaptureError, "focus"):
            self.capture()

    def test_replay_rebuilds_windows_and_rejects_missing_or_nonfinite_data(self):
        sample = self.capture()
        original = copy.deepcopy(sample)
        sample["sequence"]["aggregated_features"] = []
        export_sample(sample, self.root / "rebuild")
        for bad in (float("nan"), -1):
            item = copy.deepcopy(original)
            key = next(iter(item["baseline_windows"][0]["feature_pages"]))
            item["baseline_windows"][0]["feature_pages"][key] = bad
            with self.assertRaises(ValueError):
                export_sample(item, self.root / "invalid")
        original["operation_windows"].pop()
        with self.assertRaises(ValueError):
            export_sample(original, self.root / "missing")

    def test_plan_requires_independent_checks_and_canonical_operation(self):
        for key in ("focus", "verify", "precondition"):
            plan = dict(self.plan)
            plan.pop(key)
            with self.assertRaises(ValueError):
                validate_plan(plan)
        for duration in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                validate_plan({**self.plan, "action_s": duration})
        with self.assertRaises(KeyError):
            validate_plan({**self.plan, "operation_id": "wps.unknown"})

    def test_native_feature_cli_matches_the_replayed_vector(self):
        native = Path(os.environ.get("NATIVE_BUILD_DIR", "build/native")) / "native/operation_feature"
        if not native.is_file():
            self.skipTest("build Native targets to run numerical integration")
        sample = self.capture()
        output = self.root / "export"
        export_sample(sample, output)
        subprocess.run([str(native.resolve()), "--schema", "wps.vma.feature.v1", "--sample-id", sample["sample_id"],
                        "--input", str(output / "aggregated_features.csv"), "--output", str(self.root / "native.csv")],
                       check=True, capture_output=True, text=True)
        with (output / "vma_vectors_l2.csv").open() as handle:
            python = next(csv.DictReader(handle))
        with (self.root / "native.csv").open() as handle:
            cpp = next(csv.DictReader(handle))
        difference = max(abs(float(python[f"v{i:04d}"]) - float(cpp[f"v{i:04d}"])) for i in range(2048))
        self.assertLessEqual(difference, 1e-9)


class ProcfsTests(unittest.TestCase):
    def test_visible_tree_detects_new_child_and_ignores_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for pid,parent in ((100,1),(101,100),(102,101),(200,1)):
                (root/str(pid)).mkdir()
                (root/str(pid)/"stat").write_text(f"{pid} (name with ) bracket) S {parent} 0\n")
            self.assertEqual(descendant_pids(100,root),{100,101,102})
            (root/"103").mkdir(); (root/"103/stat").write_text("103 (new child) S 100 0\n")
            self.assertEqual(descendant_pids(100,root),{100,101,102,103})

    def test_stat_identity_handles_spaces_and_parentheses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "321").mkdir()
            (root / "321/stat").write_text("321 (app ) unusual name) S " + "0 " * 18 + "987 0\n")
            self.assertEqual(identity(321, root), ProcessIdentity(321, 987))
            (root / "321/stat").write_text("321 (app) Z " + "0 " * 18 + "987 0\n")
            with self.assertRaises(CaptureError):
                identity(321, root)

    def test_compact_uses_actual_64k_pages_and_full_pid_coverage(self):
        values = parse_compact(HEADER + "321\twps\t.so\tr-xp\t64\t1\t/usr/lib/liba.so\n", {321}, 65536)
        self.assertEqual(next(iter(values["feature_pages"].values())), 1)
        for text in (HEADER, HEADER + "321\twps\t.so\tr-xp\t64\t16\t/usr/lib/liba.so\n",
                     HEADER + "999\twps\t.so\tr-xp\t64\t1\t/usr/lib/liba.so\n"):
            with self.assertRaises(CaptureError):
                parse_compact(text, {321}, 65536)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("LINUX_VMA_COLLECTOR"), "requires built Linux C collector")
    def test_real_linux_child_process_clear_and_capture(self):
        # A controlled test process, not a WPS or accuracy experiment.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            backend = LinuxProcfs([child.pid], Path(os.environ["LINUX_VMA_COLLECTOR"]))
            backend.clear()
            with tempfile.TemporaryDirectory() as directory:
                values = backend.sample(Path(directory) / "sample.tsv", os.sysconf("SC_PAGE_SIZE"))
                self.assertTrue(values["feature_pages"])
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
