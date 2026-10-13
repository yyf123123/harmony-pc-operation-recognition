import unittest

from collector.smaps_collector import SmapsEmptyError, collect_snapshot_from_smaps


class EmptySmapsTests(unittest.TestCase):
    def test_empty_capture_is_not_a_valid_snapshot(self):
        with self.assertRaises(SmapsEmptyError):
            collect_snapshot_from_smaps("", snapshot_id="s", app_id="app", phase="BASELINE")


if __name__ == "__main__":
    unittest.main()
