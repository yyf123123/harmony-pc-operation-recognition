"""Fit a separately named server candidate; never overwrites frozen parameters.

Consumes audited real raw replays only. Test sessions are loaded only by the
separate evaluate command, with instance/sample overlap rejection. Formula stays
2048-D L2 and cosine class centers, no tuning, smoothing or rejection threshold.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import re

from collector.linux.wps_acceptance import LABELS


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def normalize(values):
    if len(values)!=2048 or not all(math.isfinite(v) for v in values): raise ValueError("invalid vector")
    norm=math.sqrt(sum(v*v for v in values))
    if not norm: raise ValueError("zero vector")
    return [v/norm for v in values]


def load_rows(directories):
    rows=[]; events=set(); versions=set(); sources=[]
    for directory in directories:
        path=directory/"analysis.json"; report=json.loads(path.read_text())
        if report.get("schema")!="linux.wps.raw-replay-audit.v1": raise ValueError("audited raw replay required")
        versions.add(report["app_version"]); sources.append(sha(path))
        for result in report["results"]:
            if result["status"]!="replay_verified": continue
            token=result["event_id"]
            if not re.fullmatch(r"[0-9a-f]{32}",token) or token in events: raise ValueError("duplicate or invalid sample ID")
            if result["truth"] not in LABELS or not result["semantic_passed"] or not result["native_capture_valid"]:
                raise ValueError("ineligible sample")
            vector_path=directory/token/"vector.json"
            if sha(vector_path)!=result["vector_sha256"]: raise ValueError("vector hash mismatch")
            vector=normalize(json.loads(vector_path.read_text()))
            events.add(token); rows.append({"sample_id":token,"label":result["truth"],"vector":vector,
                "app_instance_id":report["app_instance_id"],"raw_sha256":result["raw_sha256"],
                "vector_sha256":result["vector_sha256"],"action_method":result["action_method"]})
    if len(versions)!=1: raise ValueError("do not mix WPS versions")
    return rows,sources,next(iter(versions))


def fit(directories, output):
    rows,sources,version=load_rows(directories)
    counts=Counter(r["label"] for r in rows); instances={r["app_instance_id"] for r in rows}
    if set(counts)!=set(LABELS) or min(counts.values())<2 or len(instances)<2:
        raise ValueError("all 18 classes need at least two samples from at least two application instances")
    # Every class, including WRITE_TEXT, must span two app instances.
    if any(len({r["app_instance_id"] for r in rows if r["label"]==label})<2 for label in LABELS):
        raise ValueError("a class lacks independent restart coverage")
    output.mkdir(parents=True,exist_ok=False); model_dir=output/"model";model_dir.mkdir()
    centers=[]
    for label in LABELS:
        selected=[r["vector"] for r in rows if r["label"]==label]
        centers.append(normalize([sum(v[i] for v in selected) for i in range(2048)]))
    center_file=model_dir/"centers.csv"
    with center_file.open("w",newline="") as handle:
        writer=csv.writer(handle);writer.writerow(["center_id","operation_label"]+[f"v{i:04d}" for i in range(2048)])
        for i,label in enumerate(LABELS):writer.writerow([i,label]+[format(v,".17g") for v in centers[i]])
    model={"schema_version":"cross_app.vma-cluster-model.v1","app_id":"wps","platform":"linux",
        "model_id":"linux-wps-writer-server-20261101-v2","model_state":"server_candidate_not_promoted",
        "feature_schema":"wps.vma.feature.v1","vector_dim":2048,"center_count":18,"center_csv":"centers.csv",
        "centers_sha256":sha(center_file),"center_to_operation":{str(i):label for i,label in enumerate(LABELS)},
        "method":"class_centroid_cosine","smoothing":None,"threshold":None,"wps_version":version,
        "training_sample_count":len(rows),"training_counts":dict(counts),"training_application_instances":len(instances),
        "source_analysis_sha256":sources,"fit_scope":"calibration sessions only; separate held-out restart evaluation required",
        "capture_profile":{"name":"controlled-5+5+15+5-with-private-raw-sidecar.v1","baseline_count":2,"baseline_s":5,"action_s":15,"post_s":5},
        "automation_profile":"15 keyboard operations; find/replace/table RPC; extra input-method calibration",
        "native_runtime_ready":False,"production_ready":False}
    (model_dir/"model.json").write_text(json.dumps(model,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    manifest={"schema":"linux.wps.server-training-manifest.v1","instances":sorted(instances),
              "samples":[{k:v for k,v in row.items() if k!="vector"} for row in rows]}
    (output/"training_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n")
    (model_dir/"README.md").write_text("# Server WPS candidate\n\nExplicit candidate only. Frozen models are unchanged. Requires independent restart evaluation; controlled synthetic documents in a real WPS process, not free user actions.\n")
    (model_dir/"SHA256SUMS.json").write_text(json.dumps({p.name:sha(p) for p in model_dir.iterdir() if p.is_file()},indent=2)+"\n")
    return model


def evaluate(candidate, directories, output):
    rows,sources,version=load_rows(directories)
    model=json.loads((candidate/"model/model.json").read_text())
    manifest=json.loads((candidate/"training_manifest.json").read_text())
    if version!=model["wps_version"]: raise ValueError("heldout WPS version mismatch")
    if {r["app_instance_id"] for r in rows}&set(manifest["instances"]): raise ValueError("train/test application instance overlap")
    if {r["sample_id"] for r in rows}&{r["sample_id"] for r in manifest["samples"]}: raise ValueError("train/test sample overlap")
    center_file=candidate/"model/centers.csv"
    if sha(center_file)!=model["centers_sha256"]: raise ValueError("candidate parameter hash mismatch")
    centers=[normalize([float(r[f"v{i:04d}"]) for i in range(2048)]) for r in csv.DictReader(center_file.open())]
    predictions=[]
    for row in rows:
        scores=[sum(a*b for a,b in zip(row["vector"],center)) for center in centers]
        best=max(range(18),key=lambda i:scores[i])
        predictions.append({"sample_id":row["sample_id"],"truth":row["label"],"prediction":LABELS[best],
                            "score":scores[best],"correct":LABELS[best]==row["label"]})
    counts=Counter(p["truth"] for p in predictions)
    result={"schema":"linux.wps.server-heldout-evaluation.v1","model_sha256":sha(candidate/"model/model.json"),
        "source_analysis_sha256":sources,"scope":"held-out app restart; synthetic controlled documents on one server",
        "sample_count":len(rows),"correct":sum(p["correct"] for p in predictions),
        "accuracy":sum(p["correct"] for p in predictions)/len(rows) if rows else None,
        "full_catalog_covered":set(counts)==set(LABELS),"per_label":{label:{"support":counts[label],
            "correct":sum(p["correct"] for p in predictions if p["truth"]==label)} for label in LABELS},
        "predictions":predictions,"production_ready":False}
    output.mkdir(parents=True,exist_ok=False);(output/"evaluation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("mode",choices=("fit","evaluate"))
    p.add_argument("--analysis",type=Path,action="append",required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--candidate",type=Path);a=p.parse_args()
    result=fit(a.analysis,a.output) if a.mode=="fit" else evaluate(a.candidate,a.analysis,a.output)
    print(json.dumps({k:v for k,v in result.items() if k in ("model_id","training_sample_count","sample_count","correct","accuracy")}))


if __name__=="__main__":main()
