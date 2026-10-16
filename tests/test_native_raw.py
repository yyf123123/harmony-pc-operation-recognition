"""Raw smaps → semantic map → median/excess → vector → label parity, not accuracy."""
import csv
import json
import os
import statistics
import subprocess
import tempfile
import unittest
from pathlib import Path

from classification.predict import load_model, predict
from collector.smaps_collector import collect_snapshot_from_smaps
from collector.snapshot import aggregate_snapshot_features
from features import wps
from features.vma_feature import build_vma_feature

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "native/fixtures/raw-window"
OPSCTL = Path(os.environ.get("NATIVE_BUILD_DIR", ROOT / "build/native")) / "native/opsctl"


def reference(app):
    snapshots = {name: collect_snapshot_from_smaps((FIXTURES / (name + ".smaps")).read_text(),
                 snapshot_id=name, app_id=app, phase=name) for name in ("baseline0", "baseline1", "action", "post")}
    if app == "wps":
        # Frozen collect.c classifications for these six deliberately different VMA kinds.
        segments = (".so", "native heap", "AnonPage other", "FilePage other", "stack", "FilePage other")
        maps = {}
        for name, snapshot in snapshots.items():
            values = {}
            for region, segment in zip(snapshot.regions, segments):
                namespace = wps._feature_namespace(segment, region.pathname)
                key = (namespace, wps._semantic_key(namespace, segment, region.permissions, region.pathname))
                values[key] = values.get(key, 0) + region.referenced_pages
            maps[name] = values
        def window(name, phase):
            return {"feature_pages": {"\t".join(key): value for key, value in maps[name].items()}, "segment_label": phase}
        mapped = wps.map_fixed_window_sequence(operation_execution_id="synthetic", operation_id="",
            baseline_group_id="synthetic", baseline_windows=[window("baseline0", "BASELINE"), window("baseline1", "BASELINE")],
            operation_windows=[window("action", "ACTION"), window("post", "POST_ACTION")])
        excess = {(row["feature_namespace"], row["feature_key"]): row["aggregated_excess_pages"] for row in mapped["aggregated_features"]}
        vector = wps.l2_normalize(wps.build_vector({k: v for (ns, k), v in excess.items() if ns == "FILE"},
                                                {k: v for (ns, k), v in excess.items() if ns == "ANON"}))
        schema = "wps.vma.feature.v1"
    else:
        maps = {name: aggregate_snapshot_features(snapshot) for name, snapshot in snapshots.items()}
        feature = build_vma_feature(sample_id="synthetic", app_id=app, operation_id="",
            baselines=[snapshots["baseline0"], snapshots["baseline1"]], action=snapshots["action"], post_action=snapshots["post"])
        vector = feature.l2_vector
        excess = {("FILE", k): v for k, v in feature.file_features.items()}
        excess.update({("ANON", k): v for k, v in feature.anon_features.items()})
        schema = "cross_app.vma.feature.v2"
    maps["median"] = {key: statistics.median([maps["baseline0"].get(key, 0), maps["baseline1"].get(key, 0)])
                      for key in set(maps["baseline0"]) | set(maps["baseline1"])}
    maps["excess"] = excess
    model, centers = load_model(ROOT / "models" / app / "model.json", schema)
    return maps, vector, predict([vector], model, centers)[0]


@unittest.skipUnless(OPSCTL.is_file(), "build opsctl first")
class RawNativeParity(unittest.TestCase):
    def test_every_stage_and_all_models(self):
        for app in ("wps", "bilibili", "huawei_browser"):
            with self.subTest(app=app), tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                result = subprocess.run([str(OPSCTL), "replay", "--models", str(ROOT / "models"), "--app", app,
                    "--manifest", str(FIXTURES / "manifest.csv"), "--output", str(out / "result.json"),
                    "--vector-output", str(out / "vector.csv"), "--stages", str(out / "stages")], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                maps, vector, (label, score) = reference(app)
                for phase, expected in maps.items():
                    with (out / "stages" / (phase + ".csv")).open(newline="") as file:
                        actual = {(row["namespace"], row["feature_key"]): float(row["pages"]) for row in csv.DictReader(file)}
                    self.assertEqual(actual, expected, phase)
                with (out / "vector.csv").open(newline="") as file:
                    row = next(csv.DictReader(file))
                self.assertLessEqual(max(abs(vector[i] - float(row[f"v{i:04d}"])) for i in range(2048)), 1e-9)
                result = json.loads((out / "result.json").read_text())
                self.assertEqual(result["operation_label"], label)
                self.assertAlmostEqual(result["cosine_similarity"], score, delta=1e-9)

    def test_missing_referenced_never_writes_success(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            for source in FIXTURES.glob("*.smaps"):
                (out / source.name).write_text(source.read_text().replace("Referenced: 8 kB\n", ""))
            (out / "manifest.csv").write_text((FIXTURES / "manifest.csv").read_text())
            result = subprocess.run([str(OPSCTL), "replay", "--models", str(ROOT / "models"), "--app", "wps",
                "--manifest", str(out / "manifest.csv"), "--output", str(out / "result.json")], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing_referenced", result.stderr)
            self.assertFalse((out / "result.json").exists())

    def test_offline_selftest_needs_no_python_child(self):
        result = subprocess.run([str(OPSCTL), "selftest", "--models", str(ROOT / "models"), "--fixtures", str(FIXTURES)],
                                capture_output=True, text=True, env={**os.environ, "PATH": "/no-programs-here"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
