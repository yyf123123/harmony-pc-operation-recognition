import unittest

from collector.linux.history import OperationHistory, event_from_prediction


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = OperationHistory(history_size=2, dedup_size=3)
        self.history.reset("session", "instance")

    def event(self, number, **kwargs):
        return {"session_id": "session", "app_instance_id": "instance", "event_id": str(number),
                "operation_id": "wps.writer.write_text", "end_monotonic_ns": number,
                "source_kind": "recognition", **kwargs}

    def test_retains_repeated_actions_but_deduplicates_ids(self):
        self.assertTrue(self.history.append(self.event(1)))
        self.assertFalse(self.history.append(self.event(1)))
        self.history.append(self.event(2))
        self.assertEqual(len(self.history.prediction_request()["history_ops"]), 2)
        self.history.append(self.event(3))
        self.assertEqual([event["event_id"] for event in self.history.events], ["2", "3"])

    def test_rejects_truth_conflicts_and_cross_session_or_out_of_order_events(self):
        self.history.append(self.event(2))
        for event in (self.event(1), self.event(3, source_kind="ground_truth"),
                      self.event(2, operation_id="wps.writer.save_document"), self.event(3, session_id="another")):
            with self.assertRaises(ValueError):
                self.history.append(event)
        self.history.reset("new-session", "new-instance")
        self.assertEqual(self.history.prediction_request()["history_ops"], [])

    def test_event_uses_native_prediction_instead_of_collection_truth(self):
        sample = {"status": "success", "sample_id": "sample", "session_id": "session",
                  "app_instance_id": "instance", "operation_label": "SAVE_DOCUMENT", "is_mock": True,
                  "operation_windows": [{"end_monotonic_ns": 1}, {"end_monotonic_ns": 2}]}
        prediction = {"sample_id": "sample", "operation_label": "WRITE_TEXT", "cosine_similarity": "0.75"}
        event = event_from_prediction(sample, prediction)
        self.assertEqual(event["operation_id"], "wps.writer.write_text")
        self.history.append(event)
        self.assertEqual(self.history.prediction_request()["history_ops"], ["wps.writer.write_text"])
        with self.assertRaises(ValueError):
            event_from_prediction(sample, {**prediction, "sample_id": "another"})
