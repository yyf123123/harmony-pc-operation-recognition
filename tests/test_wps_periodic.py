"""Periodic capture contracts; synthetic parser fixtures are not WPS results."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.collect_wps_periodic import CaptureError, capture, identity, parse_smaps, target_lock

SMAPS = b"""1000-3000 rw-p 00000000 00:00 0 [heap]
Size: 8 kB
Rss: 4 kB
Pss: 2 kB
Referenced: 4 kB
Anonymous: 4 kB
VmFlags: rd wr
"""


class ParserTests(unittest.TestCase):
    def test_retains_structure_units_and_missing_fields(self):
        row, = parse_smaps(SMAPS)
        self.assertEqual(row["pathname"], "[heap]")
        self.assertEqual(row["metrics_kib"]["Referenced"], 4)
        self.assertNotIn("Swap", row["metrics_kib"])
        self.assertIn("Swap", row["missing_fields"])
        self.assertEqual(row["vm_flags"], ["rd", "wr"])

    def test_missing_reference_bad_unit_duplicate_and_truncation_are_errors(self):
        for value in (SMAPS.replace(b"Referenced: 4 kB\n", b""), SMAPS[:-1],
                      SMAPS.replace(b"Referenced: 4 kB", b"Referenced: 4 MB"),
                      SMAPS + b"Referenced: 4 kB\n", SMAPS.replace(b"Rss: 4", b"Rss: nan")):
            with self.subTest(value=value), self.assertRaises(CaptureError):
                parse_smaps(value)

    def test_negative_and_reordered_vmas_are_errors(self):
        for value in (SMAPS.replace(b"Rss: 4", b"Rss: -4"), SMAPS + SMAPS):
            with self.assertRaises(CaptureError):
                parse_smaps(value)

    def test_valid_zero_activity_is_preserved(self):
        row, = parse_smaps(SMAPS.replace(b"Referenced: 4", b"Referenced: 0"))
        self.assertEqual(row["metrics_kib"]["Referenced"], 0)


@unittest.skipUnless(sys.platform == "linux", "real self-owned Linux /proc required")
class LiveProcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # The target creates a child with a different executable. Its presence
        # must not expand this collector's explicit main-process-only scope.
        self.target = subprocess.Popen([sys.executable, "-c",
            "import subprocess,time; p=subprocess.Popen(['/bin/sleep','60']); "
            "print(p.pid,flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
        self.child_pid = int(self.target.stdout.readline())
        self.args = SimpleNamespace(pid=self.target.pid, wps_executable=Path(sys.executable),
            output=self.root / "capture", interval=.1, seconds=.3,
            app_version="self-owned-Python-process-test-not-WPS", focus_x11=False)

    def tearDown(self):
        import os
        import signal
        try: os.kill(self.child_pid, signal.SIGTERM)
        except ProcessLookupError: pass
        self.target.terminate()
        self.target.wait(timeout=3)
        self.target.stdout.close()
        self.temp.cleanup()

    def test_raw_roundtrip_main_only_and_absolute_schedule(self):
        result = capture(self.args)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["valid_frames"], 3)
        frames = [json.loads(line) for line in (self.args.output / "frames.jsonl").read_text().splitlines()]
        self.assertEqual([f["scheduled_read_ns"] - frames[0]["scheduled_read_ns"] for f in frames],
                         [0, 100000000, 200000000])
        for frame in frames:
            data = gzip.decompress((self.args.output / frame["raw_path"]).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(), frame["raw_sha256"])
            self.assertEqual(frame["vma_count"], len(parse_smaps(data)))
            self.assertLess(frame["clear_end_ns"], frame["read_start_ns"])
            self.assertEqual(frame["foreground_evidence"], "unknown")
        with gzip.open(self.args.output / "vma.jsonl.gz", "rt") as handle:
            self.assertEqual({json.loads(line)["pid"] for line in handle}, {self.target.pid})
        for item in json.loads((self.args.output / "manifest.json").read_text())["files"]:
            self.assertEqual(hashlib.sha256((self.args.output / item["path"]).read_bytes()).hexdigest(), item["sha256"])
        with self.assertRaises(FileExistsError):
            capture(self.args)

    def test_same_target_lock_refuses_second_writer(self):
        info = identity(self.target.pid, Path(sys.executable).resolve())
        with target_lock(self.target.pid, info["start_ticks"]):
            with self.assertRaises(CaptureError):
                capture(self.args)
        self.assertFalse(self.args.output.exists())

    def test_identity_mismatch_stops_without_rebinding(self):
        from scripts import collect_wps_periodic as module
        original = module.check_target
        calls = []
        def changed(*args):
            calls.append(1)
            if len(calls) > 2:
                raise module.IdentityChanged("synthetic identity mismatch")
            original(*args)
        with patch.object(module, "check_target", side_effect=changed):
            result = capture(self.args)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["valid_frames"], 0)
        self.assertEqual(result["terminal_error"], "target_exited_or_changed")

    def test_parser_failure_preserves_raw_and_stops_after_three_failures(self):
        with patch("scripts.collect_wps_periodic.parse_smaps", side_effect=CaptureError("bad smaps")):
            result = capture(self.args)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_frames"], 3)
        self.assertEqual(result["valid_frames"], 0)
        self.assertEqual(len(list((self.args.output / "raw").glob("*.gz"))), 3)

    def test_interrupted_frame_is_retained_and_counted(self):
        with patch("scripts.collect_wps_periodic.time.sleep", side_effect=KeyboardInterrupt):
            result = capture(self.args)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["interrupted_frames"], 1)
        self.assertEqual(result["valid_frames"], 0)
        self.assertEqual(result["attempted_slots"], 1)


if __name__ == "__main__":
    unittest.main()
