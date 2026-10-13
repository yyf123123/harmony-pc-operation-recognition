import unittest

from collector.snapshot import VmaRegion, VmaSnapshot
from features.feature_schema import (
    CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION,
    SIGNED_VECTOR_DIM,
)
from features.vma_feature import build_vma_feature


def snapshot(snapshot_id: str, *, referenced: float, rss: float) -> VmaSnapshot:
    return VmaSnapshot.now(
        snapshot_id,
        "browser",
        "TEST",
        [
            VmaRegion(
                0,
                1,
                "r-xp",
                "/system/lib/libgeneric.so",
                "FilePage code",
                referenced_pages=referenced,
                rss_pages=rss,
            ),
            VmaRegion(
                2,
                3,
                "rw-p",
                "(anonymous)",
                "AnonPage heap",
                referenced_pages=referenced,
                rss_pages=rss,
            ),
        ],
    )


class SignedFeatureSchemaTests(unittest.TestCase):
    def test_signed_schema_keeps_referenced_and_rss_growth_and_reclamation(self):
        feature = build_vma_feature(
            sample_id="trial-1",
            app_id="browser",
            operation_id="browser.refresh",
            baselines=[snapshot("baseline-1", referenced=10, rss=20), snapshot("baseline-2", referenced=14, rss=24)],
            action=snapshot("action", referenced=30, rss=40),
            post_action=snapshot("post-short", referenced=4, rss=8),
            post_actions=[snapshot("post-long", referenced=6, rss=10)],
            schema_version=CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION,
        )

        self.assertEqual(feature.schema_version, CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION)
        self.assertEqual(len(feature.raw_vector), SIGNED_VECTOR_DIM)
        self.assertGreater(feature.signal_strength, 0.0)
        self.assertGreater(feature.metadata["channel_signal_strength"]["referenced_positive_file"], 0.0)
        self.assertGreater(feature.metadata["channel_signal_strength"]["referenced_negative_file"], 0.0)
        self.assertGreater(feature.metadata["channel_signal_strength"]["rss_positive_anon"], 0.0)
        self.assertGreater(feature.metadata["channel_signal_strength"]["rss_negative_anon"], 0.0)


if __name__ == "__main__":
    unittest.main()
