import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from collector.linux.wps_automation import _docx_text, load_config, operation_readiness
from scripts.linux_desktop import make_plan
from scripts.linux_run import run


class DesktopSmokeContractTests(unittest.TestCase):
    def test_all_catalog_actions_are_listed_but_only_verified_primitives_enabled(self):
        readiness = operation_readiness()
        self.assertEqual(len(readiness), 18)
        self.assertEqual(sum(item["implementation_status"] == "smoke_primitive_ready" for item in readiness), 3)
        self.assertFalse(any(item["formal_collection_enabled"] for item in readiness))

    def test_plan_is_generated_without_shell_or_ui_access_and_unsupported_action_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desktop.json"
            config = {"schema_version": "linux.wps.desktop-smoke.v1", "test_copy": True,
                      "pid": 123, "start_ticks": 456, "window_id": "789",
                      "operation_id": "wps.writer.write_text", "expected_before": "",
                      "expected_after": "controlled", "payload": "controlled"}
            path.write_text(json.dumps(config))
            plan = make_plan(path, Path(directory) / "state.json")
            self.assertEqual(plan["operation_id"], "wps.writer.write_text")
            for field in ("precondition", "action", "verify", "focus"):
                self.assertIsInstance(plan[field], list)
                self.assertIn("scripts.linux_desktop", plan[field])
            for change in ({"test_copy": False}, {"operation_id": "wps.writer.close_document"},
                           {"payload": "different"}, {"expected_before": "not-blank", "expected_after": "not-blankcontrolled"}):
                path.write_text(json.dumps({**config, **change}))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_saved_docx_verification_joins_runs_within_paragraph(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.docx"
            with zipfile.ZipFile(path, "w") as handle:
                handle.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>abc</w:t></w:r><w:r><w:t>def</w:t></w:r></w:p><w:p><w:r><w:t>ghi</w:t></w:r></w:p></w:body></w:document>')
            self.assertEqual(_docx_text(path), "abcdef\nghi")


class UnifiedLauncherTests(unittest.TestCase):
    def test_build_selftest_unique_reports_and_failure_never_claims_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"schema_version": "linux.launcher.v1", "mode": "selftest", "output_root": directory}
            class Result:
                returncode = 0
            with patch("scripts.linux_run.platform.system", return_value="Linux"), \
                    patch("scripts.linux_run.shutil.which", return_value="/tool"), \
                    patch("scripts.linux_run.os.sysconf", return_value=16384), \
                    patch("scripts.linux_run.subprocess.run", return_value=Result()) as commands:
                report = run(config)
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["page_size_bytes"], 16384)
                argv = [list(call.args[0]) for call in commands.call_args_list]
                self.assertTrue(any("selftest" in command for command in argv))
                self.assertTrue(any("native/fixtures/raw-window" in " ".join(command) for command in argv))
            with patch("scripts.linux_run.platform.system", return_value="Darwin"):
                failed = run(config)
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["desktop_validation"], "pending")
                self.assertNotEqual(failed["output"], report["output"])
                self.assertTrue((Path(failed["output"]) / "report.json").is_file())
            self.assertFalse(report["formal_collection_enabled"])


if __name__ == "__main__":
    unittest.main()
