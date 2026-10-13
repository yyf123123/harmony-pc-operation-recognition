import subprocess
import unittest

from collector.automation.hdc_backend import HdcBackend, HdcError


class HdcErrorHandlingTests(unittest.TestCase):
    def test_timeout_is_retried_and_classified(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append(args)
            raise subprocess.TimeoutExpired(kwargs.get("args", args[0]), kwargs["timeout"])

        backend = HdcBackend(hdc="hdc", retries=1, runner=runner, timeout_s=0.01)
        with self.assertRaises(HdcError) as context:
            backend.discover_targets()
        self.assertEqual(context.exception.code, "HDC_TIMEOUT")
        self.assertEqual(len(calls), 2)

    def test_multiple_targets_require_explicit_selection(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"device-a\n device-b\n", b"")

        with self.assertRaises(HdcError) as context:
            HdcBackend(runner=runner).discover_targets()
        self.assertEqual(context.exception.code, "MULTIPLE_TARGETS")

    def test_multiple_process_pids_are_not_silently_collapsed_to_the_first_pid(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"101 202\n", b"")

        backend = HdcBackend(target="device-a", runner=runner)
        backend.register_validated_app("browser", bundle="com.example.browser", process="com.example.browser")
        with self.assertRaises(HdcError) as context:
            backend.resolve_pid("browser")
        self.assertEqual(context.exception.code, "PROCESS_AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
