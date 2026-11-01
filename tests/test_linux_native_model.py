"""Pinned opt-in, complete compact replay and timing contract, not UI accuracy."""
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from classification.predict import load_model,predict
from collector.linux.procfs import parse_compact
from features.wps import map_fixed_window_sequence,build_vector,l2_normalize
from scripts.linux_wps_report import capture_sample_timings,stats

ROOT=Path(__file__).resolve().parents[1]
BUILD=Path(os.environ.get("NATIVE_BUILD_DIR",ROOT/"build/native")).resolve()
PROFILE="linux-wps-writer-pilot-20261023-v1"


def compact_fixture(root):
    windows=[]
    header="pid\tprocess_name\tsegment\tperms\treferenced_kib\treferenced_pages\tpathname\n"
    with (root/"manifest.csv").open("w",newline="") as handle:
        writer=csv.writer(handle);writer.writerow(["phase","index","pid","page_size","compact_vma"])
        for n,(phase,index,label,pages) in enumerate((("baseline",0,"BASELINE_01",1),("baseline",1,"BASELINE_02",3),("action",0,"ACTION",8),("post",0,"POST_ACTION",4))):
            text=header+f'321\twps\tFilePage other\tr-xp\t{pages*64}\t{pages}\t/tmp/synthetic "quoted" file.so\n'+f'321\twps\tnative heap\trw-p\t{pages*pages*64}\t{pages*pages}\t[heap]\n'
            (root/f"{n}.tsv").write_text(text)
            writer.writerow([phase,index,321,65536,f"{n}.tsv"])
            windows.append({"segment_label":label,**parse_compact(text,{321},65536)})
    sequence=map_fixed_window_sequence(operation_execution_id="synthetic",operation_id="",baseline_group_id="synthetic",baseline_windows=windows[:2],operation_windows=windows[2:])
    maps={ns:{r["feature_key"]:r["aggregated_excess_pages"] for r in sequence["aggregated_features"] if r["feature_namespace"]==ns} for ns in ("FILE","ANON")}
    return l2_normalize(build_vector(maps["FILE"],maps["ANON"]))


@unittest.skipUnless((BUILD/"native/opsctl").is_file(),"build Native executables")
class LinuxNativeModelTests(unittest.TestCase):
    def test_compact_raw_to_linux_prediction_matches_python_and_64k_pages(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);vector=compact_fixture(root)
            result=subprocess.run([str(BUILD/"native/opsctl"),"replay","--models",str(ROOT/"models"),"--app","wps","--model-profile",PROFILE,"--format","compact-vma","--manifest",str(root/"manifest.csv"),"--output",str(root/"result.json"),"--vector-output",str(root/"vector.csv")],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            model,centers=load_model(ROOT/"models/linux/wps_writer_pilot_v1/model.json","wps.vma.feature.v1")
            label,score=predict([vector],model,centers)[0]
            event=json.loads((root/"result.json").read_text())
            self.assertEqual(event["model_profile"],PROFILE);self.assertEqual(event["operation_label"],label)
            self.assertAlmostEqual(event["cosine_similarity"],score,delta=1e-9)
            with (root/"vector.csv").open() as f:row=next(csv.DictReader(f))
            self.assertLessEqual(max(abs(vector[i]-float(row[f"v{i:04d}"])) for i in range(2048)),1e-9)

    def test_wrong_profile_missing_candidate_or_modified_parameters_never_fall_back(self):
        for app,profile in (("wps","unknown-profile"),("bilibili",PROFILE)):
            result=subprocess.run([str(BUILD/"native/opsctl"),"doctor","--models",str(ROOT/"models"),"--app",app,"--model-profile",profile],capture_output=True)
            self.assertNotEqual(result.returncode,0)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);shutil.copytree(ROOT/"models/wps",root/"wps")
            command=[str(BUILD/"native/opsctl"),"doctor","--models",str(root),"--app","wps","--model-profile",PROFILE]
            self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)
            shutil.copytree(ROOT/"models/linux/wps_writer_pilot_v1",root/"linux/wps_writer_pilot_v1")
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,0)
            with (root/"linux/wps_writer_pilot_v1/centers.csv").open("a") as f:f.write("\n")
            self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)

    def test_compact_pid_or_page_count_failure_cannot_write_a_prediction(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);compact_fixture(root)
            original=(root/"2.tsv").read_text()
            for number,text in enumerate((original.replace("321\twps","999\twps"),original.replace("512\t8\t","512\t7\t"),original.splitlines()[0]+"\n")):
                (root/"2.tsv").write_text(text)
                output=root/f"bad-{number}.json"
                result=subprocess.run([str(BUILD/"native/opsctl"),"replay","--models",str(ROOT/"models"),"--app","wps","--model-profile",PROFILE,"--format","compact-vma","--manifest",str(root/"manifest.csv"),"--output",str(output)],capture_output=True)
                self.assertNotEqual(result.returncode,0);self.assertFalse(output.exists())

    def test_benchmark_has_one_record_per_repeat_and_disjoint_stage_timers(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);compact_fixture(root)
            (root/"observations.csv").write_text("sample_id,window_manifest\nsynthetic,manifest.csv\n")
            result=subprocess.run([str(BUILD/"native/operation_benchmark"),"--models",str(ROOT/"models"),"--model-profile",PROFILE,"--manifest",str(root/"observations.csv"),"--output",str(root/"bench"),"--repeats","3"],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            meta=json.loads(result.stdout);self.assertEqual(meta["repeats"],3);self.assertEqual(meta["observations"],1)
            with (root/"bench/timings.csv").open() as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),3)
            for row in rows:
                components=sum(int(v) for k,v in row.items() if k.endswith("_ns") and k!="total_ns")
                self.assertLessEqual(components,int(row["total_ns"]))
                self.assertGreater(int(row["inference_ns"]),0)


class TimingStatisticsTests(unittest.TestCase):
    def test_percentiles_and_nested_capture_costs_do_not_double_count(self):
        self.assertEqual(stats([1,2,3,4])["p50"],2.5)
        sample={"created_at":"2026-01-01T00:00:00+00:00","sample_started_at":"2026-01-01T00:00:01+00:00","sample_ended_at":"2026-01-01T00:00:33+00:00"}
        windows=[];start=1_000_000_000
        for label,seconds in (("BASELINE_01",5),("BASELINE_02",5),("ACTION",15),("POST_ACTION",5)):
            w={"segment_label":label,"start_monotonic_ns":start,"end_monotonic_ns":start+int((seconds+.2)*1e9),"T_clear_refs_s":.1,"T_device_sampling_s":.2}
            if label=="ACTION":w.update(action_start_monotonic_ns=start+100_000_000,action_end_monotonic_ns=start+1_100_000_000,verification_end_monotonic_ns=start+1_600_000_000)
            windows.append(w);start=w["end_monotonic_ns"]+100_000_000
        sample.update(baseline_windows=windows[:2],operation_windows=windows[2:])
        values=capture_sample_timings(sample)
        self.assertAlmostEqual(values["action_window_s"],15.2)
        self.assertAlmostEqual(values["semantic_verification_nested_s"],.5)
        self.assertAlmostEqual(values["inter_phase_overhead_s"],.3)
        with self.assertRaises(ValueError):stats([float("nan")])


if __name__=="__main__":unittest.main()
