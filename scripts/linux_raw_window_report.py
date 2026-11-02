"""Replay private Native raw sidecars with the frozen Python feature formula."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import bisect
import subprocess

from features.wps import _semantic_key, map_fixed_window_sequence, build_vector, l2_normalize


def reconstruct(records, phases, identities=None, feature_pids=None):
    expected = {(p["phase"], p["index"], v["pid"]): v for p in phases for v in p["processes"]}
    if len(expected) != sum(len(p["processes"]) for p in phases): raise ValueError("duplicate phase/process")
    if {(p["phase"],p["index"]) for p in phases} != {("baseline",0),("baseline",1),("action",0),("post",0)}:
        raise ValueError("incomplete controlled window phases")
    maps = {}; seen = set(); page_size = None
    ticks = {v["pid"]:v["start_ticks"] for v in (identities or [])}
    selected = set(feature_pids) if feature_pids is not None else {k[2] for k in expected}
    if not selected or not selected <= {k[2] for k in expected}: raise ValueError("invalid feature PID selection")
    for record in records:
        if record.get("schema") != "harmony.ops.raw-window.v1" or record.get("feature_schema") != "wps.vma.feature.v1":
            raise ValueError("unknown raw schema")
        key = (record["phase"], record["index"], record["pid"])
        if key not in expected or key in seen: raise ValueError("unexpected or repeated process snapshot")
        if record["start_ticks"] <= 0 or record["start_ticks"] != ticks.setdefault(record["pid"],record["start_ticks"]):
            raise ValueError("process start identity changed")
        seen.add(key); timing = expected[key]
        if not timing["sample_start_ns"] <= record["started_ns"] <= record["finished_ns"] <= timing["sample_end_ns"]:
            raise ValueError("snapshot outside its Native sampling interval")
        size = record["page_size"]
        if size < 1024 or size & (size-1) or (page_size is not None and size != page_size): raise ValueError("inconsistent page size")
        page_size = size
        if not record["vmas"]: raise ValueError("empty process snapshot")
        target = maps.setdefault(key[:2], {})
        for v in record["vmas"]:
            count = v["referenced_pages"]; nbytes = v["referenced_bytes"]
            if (type(count) is not int or type(nbytes) is not int or count < 0 or nbytes < 0 or
                    count != (nbytes+size-1)//size or not re.fullmatch(r"[r-][w-][x-][ps]",v["perms"])):
                raise ValueError("invalid VMA reference/perms")
            namespace = "FILE" if v["pathname"].startswith("/") or v["segment"].lower().startswith("filepage") else "ANON"
            feature = namespace+"\t"+_semantic_key(namespace,v["segment"],v["perms"],v["pathname"])
            if record["pid"] in selected: target[feature] = target.get(feature,0)+count
    if seen != set(expected): raise ValueError("partial PID/phase coverage")
    def window(key): return {"window_id":str(key),"segment_label":"ACTION" if key[0]=="action" else "POST_ACTION",
                             "feature_pages":maps[key],"feature_meta":{}}
    seq = map_fixed_window_sequence(operation_execution_id="opaque",operation_id="opaque",baseline_group_id="opaque",
        baseline_windows=[window(("baseline",0)),window(("baseline",1))],
        operation_windows=[window(("action",0)),window(("post",0))])
    aggregated = seq["aggregated_features"]
    values = {ns:{r["feature_key"]:r["aggregated_excess_pages"] for r in aggregated if r["feature_namespace"]==ns} for ns in ("FILE","ANON")}
    vector = l2_normalize(build_vector(values["FILE"],values["ANON"]))
    if not any(vector) or not all(math.isfinite(x) for x in vector): raise ValueError("zero or nonfinite feature")
    return vector, seq


def score(vector, model_dir):
    if len(vector)!=2048 or not all(math.isfinite(v) for v in vector) or not any(vector):
        raise ValueError("invalid model input vector")
    model = json.loads((model_dir/"model.json").read_text())
    centers = model_dir/model["center_csv"]
    if hashlib.sha256(centers.read_bytes()).hexdigest() != model["centers_sha256"]: raise ValueError("model hash mismatch")
    rows = list(csv.DictReader(centers.open()))
    result = []
    for index,row in enumerate(rows):
        if row["operation_label"] != model["center_to_operation"][str(index)]: raise ValueError("label mapping mismatch")
        values = [float(row[f"v{i:04d}"]) for i in range(2048)]
        norm = math.sqrt(sum(v*v for v in values))
        if not norm or not all(math.isfinite(v) for v in values): raise ValueError("invalid class center")
        result.append({"label":row["operation_label"],"score":sum(a*b for a,b in zip(vector,values))/norm})
    return sorted(result,key=lambda v:-v["score"])


def analyze(source, models, output, native_feature=None):
    report = json.loads((source/"report.json").read_text()); output.mkdir(parents=True,exist_ok=False)
    if report.get("is_mock") is not False: raise ValueError("real report required")
    scoped = {}
    if (source/"soak-raw.jsonl").exists():
        # One pass partitions a persistent service's raw stream by its actual
        # Native sampling clocks, never by automation labels or request times.
        intervals = {}
        for trial in report["trials"]:
            if not trial["semantic_passed"] or not trial["capture_valid"]: continue
            for phase in trial["native_phases"]:
                for process in phase["processes"]:
                    intervals.setdefault(process["pid"],[]).append((process["sample_start_ns"],process["sample_end_ns"],trial["event_id"]))
        starts = {}
        for pid,values in intervals.items(): values.sort(); starts[pid]=[v[0] for v in values]
        folder=output/"scoped-raw";folder.mkdir()
        for line in (source/"soak-raw.jsonl").open():
            record=json.loads(line);pid=record["pid"]
            index=bisect.bisect_right(starts.get(pid,[]),record["started_ns"])-1
            if index<0: continue
            start,end,token=intervals[pid][index]
            if record["finished_ns"]>end: continue
            path=folder/(token+".jsonl");scoped[token]=path
            with path.open("a") as handle: handle.write(line)
    results = []
    for trial in report["trials"]:
        row = {"event_id":trial["event_id"],"truth":trial["operation_label"],"semantic_passed":trial["semantic_passed"],
               "native_capture_valid":trial["capture_valid"],"payload_characters":trial.get("payload_characters"),"action_method":trial["action_method"]}
        results.append(row)
        if not trial["semantic_passed"] or not trial["capture_valid"]: row["status"]="not_eligible";continue
        folder = source/trial["event_id"]
        raw=scoped.get(trial["event_id"],folder/"raw.jsonl")
        records = [json.loads(line) for line in raw.read_text().splitlines()]
        vector, seq = reconstruct(records,trial["native_phases"],trial.get("processes"),trial.get("feature_pids"))
        directories={"linux-wps-writer-pilot-20261023-v1":"wps_writer_pilot_v1",
                     "linux-wps-writer-server-20261101-v2":"wps_writer_server_v2"}
        if report["model_profile"] not in directories:raise ValueError("unknown model profile")
        scores = score(vector,models/"linux"/directories[report["model_profile"]])
        if scores[0]["label"]!=trial["prediction"] or abs(scores[0]["score"]-trial["cosine_similarity"])>1e-9:
            raise ValueError("raw Python replay disagrees with Native prediction")
        row.update(status="replay_verified",prediction=scores[0]["label"],score=scores[0]["score"],scores=scores,
                   truth_rank=next(i+1 for i,v in enumerate(scores) if v["label"]==row["truth"]),
                   nonzero_slots=sum(v!=0 for v in vector),raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest())
        save_dir=output/trial["event_id"];save_dir.mkdir()
        (save_dir/"vector.json").write_text(json.dumps(vector)+"\n")
        row["vector_sha256"] = hashlib.sha256((save_dir/"vector.json").read_bytes()).hexdigest()
        (save_dir/"sequence.json").write_text(json.dumps(seq,ensure_ascii=False)+"\n")
        if native_feature:
            input_csv=save_dir/"excess.csv";native_csv=save_dir/"native-vector.csv"
            with input_csv.open("w",newline="") as handle:
                writer=csv.writer(handle);writer.writerow(["namespace","feature_key","pages"])
                for value in seq["aggregated_features"]:
                    writer.writerow([value["feature_namespace"],value["feature_key"],format(value["aggregated_excess_pages"],".17g")])
            subprocess.run([str(native_feature),"--schema","wps.vma.feature.v1","--sample-id",trial["event_id"],
                            "--input",str(input_csv),"--output",str(native_csv)],check=True,capture_output=True,timeout=10)
            native_row=next(csv.DictReader(native_csv.open()))
            error=max(abs(vector[i]-float(native_row[f"v{i:04d}"])) for i in range(2048))
            if error>1e-9:raise ValueError("Python/C++ feature comparison failed")
            row["max_native_feature_error"]=error
    result={"schema":"linux.wps.raw-replay-audit.v1","source_report_sha256":hashlib.sha256((source/"report.json").read_bytes()).hexdigest(),
            "model_profile":report["model_profile"],"app_version":report["app_version"],
            "app_instance_id":str(report["root_pid"])+":"+str(report["root_start_ticks"]),
            "results":results,"replayed":sum(r["status"]=="replay_verified" for r in results)}
    (output/"analysis.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--source",type=Path,required=True)
    p.add_argument("--models",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--native-feature",type=Path)
    a=p.parse_args();print(json.dumps({"replayed":analyze(a.source,a.models,a.output,a.native_feature)["replayed"]}))


if __name__=="__main__":main()
