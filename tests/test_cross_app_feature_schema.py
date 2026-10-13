import unittest

from collector.smaps_collector import collect_snapshot_from_smaps
from features.feature_schema import CROSS_APP_FEATURE_SCHEMA_VERSION, LEGACY_WPS_FEATURE_SCHEMA_VERSION
from features.vma_feature import build_vma_feature


SMAPS = """00400000-00402000 r-xp 00000000 00:00 1 /system/lib/libbrowser.so
Rss: 8 kB
Referenced: 8 kB

00600000-00602000 rw-p 00000000 00:00 0
Rss: 4 kB
Referenced: 4 kB
"""


class CrossAppFeatureSchemaTests(unittest.TestCase):
    def test_browser_and_bilibili_share_the_same_app_agnostic_schema(self):
        features = []
        for app_id in ("huawei_browser", "bilibili"):
            baseline = collect_snapshot_from_smaps(SMAPS, snapshot_id=f"{app_id}-b", app_id=app_id, phase="BASELINE")
            action = collect_snapshot_from_smaps(SMAPS.replace("Referenced: 8", "Referenced: 12"), snapshot_id=f"{app_id}-a", app_id=app_id, phase="ACTION")
            post = collect_snapshot_from_smaps(SMAPS.replace("Referenced: 4", "Referenced: 12"), snapshot_id=f"{app_id}-p", app_id=app_id, phase="POST_ACTION")
            features.append(build_vma_feature(sample_id=app_id, app_id=app_id, operation_id=f"{app_id}.op", baselines=[baseline], action=action, post_action=post))
        self.assertEqual({feature.schema_version for feature in features}, {CROSS_APP_FEATURE_SCHEMA_VERSION})
        self.assertEqual({len(feature.raw_vector) for feature in features}, {2048})

    def test_frozen_wps_schema_can_still_be_requested(self):
        self.assertNotEqual(CROSS_APP_FEATURE_SCHEMA_VERSION, LEGACY_WPS_FEATURE_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
