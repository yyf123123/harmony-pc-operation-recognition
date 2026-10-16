import copy
import json
from pathlib import Path
import tempfile
import unittest

from collector.linux.history import event_from_native_runtime
from collector.linux.next_operation import MarkovArtifact, OpMarkovModel, predict_or_not_ready
from collector.linux.page_mapping import PageMapping, candidates_or_unknown
from scripts.linux_followup import replay_events


class MarkovTests(unittest.TestCase):
    def setUp(self):
        self.vocab = {"wps": {"<PAD>": 0, "<UNK>": 1, "wps.writer.write_text": 2,
                              "wps.writer.save_document": 3, "wps.writer.format_bold": 4}}
        self.samples = [{"app": "wps", "history_ops": [0, 2, 2], "label_next_op": 3},
                        {"app": "wps", "history_ops": [2, 3], "label_next_op": 4},
                        {"app": "wps", "history_ops": [2, 3], "label_next_op": 3}]
        self.model = OpMarkovModel(2, self.vocab)
        self.model.fit(self.samples)
        self.payload = self.model.export(model_id="fixture", vocab_version="fixture.v1",
            source_kind="synthetic_fixture", source_sessions=["fixture-session"])
        self.artifact = MarkovArtifact.from_dict(self.payload)
        self.request = {"schema_version": "linux.next-operation-request.v1", "app_id": "wps",
                        "session_id": "session", "app_instance_id": "instance",
                        "history_ops": ["wps.writer.write_text", "wps.writer.save_document"]}

    def test_roundtrip_keeps_counts_vocab_and_backoff_tie_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            self.artifact.save(path)
            loaded = MarkovArtifact.load(path)
            self.assertEqual(loaded.sha256, self.artifact.sha256)
            self.assertEqual(loaded.model.predict_topk("wps", [2, 3]), [3, 4])
            self.assertEqual(loaded.model.predict_topk("wps", [4]), [2, 3, 4])
            self.assertEqual(loaded.model.global_counts, self.model.global_counts)
            self.assertEqual(loaded.model.transitions, self.model.transitions)
        result = self.artifact.predict(self.request, allow_fixture=True)
        self.assertEqual(result["context_order"], 2)
        self.assertEqual([item["operation_id"] for item in result["candidates"]],
                         ["wps.writer.save_document", "wps.writer.format_bold"])
        self.assertEqual(sum(item["score"] for item in result["candidates"]), 1)

    def test_missing_synthetic_unknown_or_empty_models_are_never_fake_ready(self):
        self.assertEqual(predict_or_not_ready(None, self.request)["reason"], "model_unavailable")
        self.assertEqual(self.artifact.predict(self.request)["reason"], "synthetic_model_disabled")
        self.assertEqual(self.artifact.predict({**self.request, "history_ops": ["unknown"]},
                                              allow_fixture=True)["reason"], "vocabulary_mismatch")
        self.assertEqual(self.artifact.predict({**self.request, "history_ops": []},
                                              allow_fixture=True)["reason"], "empty_history")
        for mutate in (
                lambda p: p.update(schema_version="unsupported"),
                lambda p: p["op_vocab"]["wps"].update({"duplicate": 2}),
                lambda p: p["global_counts"]["wps"].append([3, 1]),
                lambda p: p["global_counts"]["wps"][0].__setitem__(1, -1),
                lambda p: p["training_provenance"].update(split_unit="window")):
            payload = json.loads(json.dumps(self.payload))
            mutate(payload)
            with self.assertRaises((ValueError, KeyError)):
                MarkovArtifact.from_dict(payload)

    def test_fit_formula_matches_frozen_legacy_counts(self):
        # Hand-calculated legacy semantics: each history occurrence and target
        # contributes globally; transitions within history are counted as well.
        self.assertEqual(self.model.global_counts["wps"], {2: 4, 3: 4, 4: 1})
        self.assertEqual(self.model.transitions["wps"][(2,)], {2: 1, 3: 3})
        self.assertEqual(self.model.transitions["wps"][(2, 3)], {4: 1, 3: 1})

    def test_replay_deduplicates_recognition_and_never_writes_future_into_history(self):
        event = {"schema_version": "linux.operation-event.v1", "app_id": "wps", "session_id": "session",
                 "app_instance_id": "instance", "method": "native_class_centroid_cosine", "source_kind": "recognition",
                 "operation_id": "wps.writer.write_text", "end_monotonic_ns": 1, "event_id": "e1", "is_mock": True}
        rows = replay_events([event, event, {**event, "event_id": "e2", "end_monotonic_ns": 2}],
                             model=self.artifact, allow_fixture=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["request"]["history_ops"], ["wps.writer.write_text"] * 2)
        self.assertEqual(rows[-1]["pages"]["reason"], "mapping_unavailable")
        with self.assertRaises(ValueError):
            replay_events([{**event, "source_kind": "ground_truth"}], allow_fixture=True)
        with self.assertRaises(ValueError):
            replay_events([event], model=self.artifact)

    def test_native_runtime_adapter_uses_only_predicted_legacy_label(self):
        native = {"schema": "harmony.ops.recognized.v1", "status": "success", "method": "vma-centroid",
                  "source": "controlled_live_window", "app_id": "wps", "app_instance_id": "instance",
                  "session_id": "session", "event_id": "native-event", "window_id": "window",
                  "clock": "monotonic_ns", "start_ns": 1, "end_ns": 2,
                  "operation_label": "WRITE_TEXT", "cosine_similarity": 0.75,
                  "feature_schema": "wps.vma.feature.v1", "ground_truth": "SAVE_DOCUMENT"}
        event = event_from_native_runtime(native)
        self.assertEqual(event["operation_id"], "wps.writer.write_text")
        rows = replay_events([native])
        self.assertEqual(rows[0]["prediction"]["reason"], "model_unavailable")
        for changes in ({"status": "failed"}, {"source": "replay_fixture"}, {"end_ns": 0},
                        {"operation_label": "unknown"}, {"cosine_similarity": float("nan")}):
            with self.assertRaises(ValueError):
                event_from_native_runtime({**native, **changes})


class PageMappingTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"sha256": "a" * 64, "size_bytes": 8192}
        self.payload = {"schema_version": "linux.operation-file-pages.v1", "mapping_id": "fixture",
                        "mapping_version": "fixture.v1", "app_id": "wps", "app_version": "test",
                        "label_schema_version": "catalog.v1", "source_kind": "synthetic_fixture",
                        "source_sessions": ["fixture-session"], "created_unix_ns": 100,
                        "expires_unix_ns": 1000, "candidate_ttl_ns": 500,
                        "entries": [{"operation_id": "wps.writer.save_document", "file_identity": self.identity,
                                     "file_page_index": 1, "page_size_bytes": 4096, "support_count": 5,
                                     "access_frequency": 0.8, "evidence_kind": "observed_file_page_access"}]}
        self.mapping = PageMapping(self.payload)
        self.prediction = {"source_kind": "prediction", "app_id": "wps", "status": "ready", "candidates": [
            {"operation_id": "wps.writer.save_document", "score": 0.6, "score_kind": "markov_empirical_frequency"}]}
        self.args = dict(app_version="test", label_schema_version="catalog.v1", observed_file_identities=[self.identity],
                         page_size_bytes=4096, clock_domain="fixture-boot", now_monotonic_ns=10,
                         now_unix_ns=800, allow_fixture=True)

    def test_verified_match_has_bounded_ttl_and_never_marks_others_cold(self):
        result = self.mapping.query(self.prediction, **self.args)
        self.assertEqual(result["status"], "candidates")
        self.assertEqual(result["candidates"][0]["expires_monotonic_ns"], 210)
        self.assertEqual(result["candidates"][0]["label"], "hot_candidate")
        self.assertEqual(result["scope"], "user_space_candidates_only")

    def test_identity_app_time_page_or_missing_prediction_remain_unknown(self):
        for change in ({"observed_file_identities": [{"sha256": "b" * 64, "size_bytes": 8192}]},
                       {"app_version": "other"}, {"now_unix_ns": 1000}, {"page_size_bytes": 16384},
                       {"allow_fixture": False}):
            self.assertEqual(self.mapping.query(self.prediction, **{**self.args, **change})["status"], "unknown")
        self.assertEqual(self.mapping.query({**self.prediction, "status": "not_ready"}, **self.args)["status"], "unknown")
        self.assertEqual(candidates_or_unknown(None, self.prediction)["reason"], "mapping_unavailable")

    def test_vma_aggregate_bad_page_and_unstable_identities_are_rejected(self):
        for change in ({"evidence_kind": "vma_aggregate"}, {"file_page_index": 2},
                       {"file_identity": {"pfn": 123}}, {"access_frequency": float("nan")}):
            payload = copy.deepcopy(self.payload)
            payload["entries"][0].update(change)
            with self.assertRaises(ValueError):
                PageMapping(payload)


if __name__ == "__main__":
    unittest.main()
