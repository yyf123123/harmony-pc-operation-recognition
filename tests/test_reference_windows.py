import unittest

from collector.reference_windows import CaptureProfile, ReferenceWindowCapture


class ReferenceWindowTests(unittest.TestCase):
    def test_action_window_is_armed_before_action_and_never_cleared_before_capture(self):
        calls: list[str] = []
        capture = ReferenceWindowCapture(
            clear_refs=lambda: calls.append("clear_refs"),
            sleep=lambda _: calls.append("sleep"),
        )

        capture.arm("ACTION_WINDOW")
        calls.append("action")
        calls.append("confirmation")
        captured = capture.capture("ACTION_WINDOW", lambda: calls.append("capture") or "snapshot")

        self.assertEqual(captured, "snapshot")
        self.assertEqual(calls, ["clear_refs", "action", "confirmation", "capture"])

    def test_profile_has_two_baselines_and_short_and_long_post_windows(self):
        profile = CaptureProfile()
        self.assertEqual(profile.baseline_phases, ("BASELINE_1", "BASELINE_2"))
        self.assertEqual(profile.baseline_window_ms, 750)
        self.assertEqual(profile.post_windows, (("POST_0750", 750), ("POST_3000", 3000)))
        self.assertEqual(profile.to_metadata()["action_phase"], "ACTION_WINDOW")

    def test_profile_identity_includes_all_timing_semantics(self):
        settled = CaptureProfile(name="cross_app.reference_windows.v2_settled", prepare_settle_ms=5000)
        immediate = CaptureProfile(name="cross_app.reference_windows.v2_settled", prepare_settle_ms=0)
        self.assertNotEqual(settled.identity, immediate.identity)
        self.assertEqual(settled.to_metadata()["identity"], settled.identity)


if __name__ == "__main__":
    unittest.main()
