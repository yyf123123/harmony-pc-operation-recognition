"""Linux /proc failure-path integration using only self-owned test processes.

The PATH helper below deliberately synthesizes X11 focus output. These tests do
not connect to DISPLAY, use real WPS, or establish graphical focus correctness.
Actual procfs reads/clear_refs, child coverage changes and SIGINT are exercised.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get("NATIVE_BUILD_DIR", ROOT / "build/native"))
OPSCTL = (BUILD / "native/opsctl").resolve()

# Only this generated target may spawn a child, and its cleanup stops only that
# exact subprocess. No application launch, pkill, process-name search or sudo.
TARGET = r'''
import signal
import subprocess
import sys
children = []
def finish(*unused):
    for child in children:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, finish)
print("READY", flush=True)
try:
    for line in sys.stdin:
        if line.strip() == "spawn":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            children.append(child)
            print("CHILD " + str(child.pid), flush=True)
        elif line.strip() == "stop":
            break
finally:
    finish()
'''


def read_line_until(stream, deadline):
    """Byte-wise reads avoid TextIO read-ahead hiding a later complete line."""
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    result = bytearray()
    try:
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            value = os.read(stream.fileno(), 1)
            if not value:
                break
            result.extend(value)
            if value == b"\n":
                return result.decode("utf-8")
    finally:
        selector.close()
    if result:
        raise AssertionError("synthetic child emitted an incomplete line")
    raise AssertionError("timed out waiting for self-owned test process")


@unittest.skipUnless(sys.platform == "linux" and OPSCTL.is_file(), "requires built Linux opsctl")
class OpsctlLinuxFailures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="opsctl-synthetic-linux-")
        self.root = Path(self.directory.name)
        self.target = subprocess.Popen([sys.executable, "-u", "-c", TARGET],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.running = None
        self.addCleanup(self._cleanup)
        self.assertEqual(read_line_until(self.target.stdout, time.monotonic() + 5).strip(), "READY")
        helpers = self.root / "synthetic-focus-bin"
        helpers.mkdir()
        helper = helpers / "xdotool"
        helper.write_text("#!" + sys.executable + '\n' + r'''
# SYNTHETIC FOCUS HELPER FOR TESTS ONLY: never accesses a desktop or DISPLAY.
import os
import sys
import time
if os.environ.get("OPS_TEST_FOCUS_TIMEOUT") == "1":
    time.sleep(10)
if sys.argv[1:] == ["getactivewindow"]:
    print("424242")
elif sys.argv[1:] == ["getwindowpid", "424242"]:
    print(os.environ["OPS_TEST_TARGET_PID"])
else:
    raise SystemExit(3)
''', encoding="utf-8")
        helper.chmod(0o700)
        self.environment = {**os.environ, "PATH": str(helpers) + os.pathsep + os.environ.get("PATH", ""),
                            "OPS_TEST_TARGET_PID": str(self.target.pid)}

    def _cleanup(self):
        if self.running is not None and self.running.poll() is None:
            self.running.send_signal(signal.SIGINT)
            try:
                self.running.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                self.running.kill()
                self.running.communicate(timeout=4)
        if self.target.poll() is None:
            try:
                self.target.stdin.write(b"stop\n")
                self.target.stdin.flush()
                self.target.communicate(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                self.target.terminate()
                self.target.communicate(timeout=5)
        else:
            self.target.communicate(timeout=4)
        self.directory.cleanup()

    def start(self, *, baseline_ms=50, extra_pid=None, timeout_helper=False):
        output = self.root / "result.jsonl"
        config = self.root / "synthetic-device.conf"
        covered = str(self.target.pid) + ("," + str(extra_pid) if extra_pid else "")
        config.write_text("\n".join(("app_id=wps", "models_root=" + str(ROOT / "models"),
            "session_id=synthetic-linux-proc-test", "root_pid=" + str(self.target.pid),
            "covered_pids=" + covered, "output_jsonl=" + str(output),
            "baseline_ms=" + str(baseline_ms), "baseline_count=2", "action_ms=200", "post_ms=50",
            "focus_poll_ms=10", "max_sample_ms=1000")) + "\n", encoding="utf-8")
        env = {**self.environment}
        if timeout_helper:
            env["OPS_TEST_FOCUS_TIMEOUT"] = "1"
        self.running = subprocess.Popen([str(OPSCTL), "run", "--config", str(config),
            "--window-id", "synthetic-window", "--event-id", "synthetic-event"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        return output

    def wait_phase(self, phase):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            line = read_line_until(self.running.stdout, deadline)
            event = json.loads(line)
            if event.get("schema") == "harmony.ops.stage.v1" and event.get("phase") == phase:
                return
        self.fail("expected controlled phase was never armed")

    def assert_failed_without_recognition(self, output, code):
        stdout, stderr = self.running.communicate(timeout=8)
        self.assertNotEqual(self.running.returncode, 0, stderr.decode())
        records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(records, "runtime must write its structured failure event")
        self.assertTrue(any(record.get("code") == code for record in records), (records, stderr.decode()))
        self.assertFalse(any(record.get("status") == "success" for record in records), records)
        self.assertFalse(any(record.get("schema") == "harmony.ops.recognized.v1" for record in records), records)
        self.assertNotIn(b'"status":"success"', stdout)
        # CLI exits here; its internal empty-on-failure history is separately
        # asserted by runtime_test/service_c_test rather than inferred from a file.

    def test_new_child_during_action_invalidates_complete_window(self):
        output = self.start()
        self.wait_phase("action")
        self.target.stdin.write(b"spawn\n")
        self.target.stdin.flush()
        child = read_line_until(self.target.stdout, time.monotonic() + 3)
        self.assertTrue(child.startswith("CHILD "))
        self.assert_failed_without_recognition(output, "process_changed")

    def test_missing_selected_process_fails_before_clear_or_action(self):
        output = self.start(extra_pid=2147483647)
        self.assert_failed_without_recognition(output, "process_changed")

    def test_target_exit_during_baseline_is_not_valid_observation(self):
        output = self.start(baseline_ms=2000)
        self.wait_phase("baseline")
        self.target.terminate()
        self.target.wait(timeout=4)
        self.assert_failed_without_recognition(output, "process_changed")

    def test_sigint_cancels_long_baseline_without_prediction(self):
        output = self.start(baseline_ms=5000)
        self.wait_phase("baseline")
        started = time.monotonic()
        self.running.send_signal(signal.SIGINT)
        self.assert_failed_without_recognition(output, "cancelled")
        self.assertLess(time.monotonic() - started, 3, "SIGINT must cancel the wait, not finish 5 s baseline")

    def test_synthetic_focus_helper_timeout_is_bounded_and_fails(self):
        started = time.monotonic()
        output = self.start(timeout_helper=True)
        self.assert_failed_without_recognition(output, "focus_changed")
        self.assertLess(time.monotonic() - started, 4)


if __name__ == "__main__":
    unittest.main()
