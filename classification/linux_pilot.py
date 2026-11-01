"""Train a separate Linux WPS pilot centroid; preserve every frozen model.

Only formal independent train documents fit parameters. All other observations
are diagnostic evaluation, never model selection or a final deployment claim.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import platform

import numpy as np

from apps.wps.adapter import WpsAdapter
from collector.linux.batch import atomic_json, digest, file_sha
from collector.linux.intake import EvidenceZip, VECTOR_HEADER
from classification import algorithms
from classification.predict import load_model, predict

CATALOG = tuple(WpsAdapter().catalog.operations)
LABELS = [str(op.action_spec["legacy_label"]) for op in CATALOG]
SCHEMA = "wps.vma.feature.v1"


def load_intake(directory):
    directory = Path(directory).resolve(strict=True)
    report = json.loads((directory/"intake_report.json").read_text())
    if (report.get("status") != "partial_pilot_verified" or report.get("selected_formal_windows") != 190
            or file_sha(directory/"source.zip") != report.get("archive_sha256")):
        raise ValueError("a verified, unchanged 190-observation real pilot is required")
    index = json.loads((directory/"sample_index.json").read_text())
    ids = [r["sample_id"] for r in index]
    if len(index) != 226 or len(set(ids)) != 226 or sum(r["stage"]=="preflight" for r in index) != 36:
        raise ValueError("selected/preflight intake identities differ")
    with (directory/"native_vectors.csv").open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != VECTOR_HEADER:
            raise ValueError("expected 2048-dimensional Native vectors")
        rows = list(reader)
    if (len(rows) != len(ids) or {r["sample_id"] for r in rows} != set(ids)
            or any(None in r or any(v is None for v in r.values()) for r in rows)):
        raise ValueError("vector/sample identity join differs")
    vectors = {r["sample_id"]:np.asarray([float(r[f"v{i:04d}"]) for i in range(2048)]) for r in rows}
    if any(not np.isfinite(v).all() or abs(np.linalg.norm(v)-1)>1e-9 for v in vectors.values()):
        raise ValueError("nonfinite, zero or unnormalized input vector")
    by_id = {r["sample_id"]:r for r in index}
    evidence = EvidenceZip(directory/"source.zip")
    records = []
    try:
        selected = evidence.json("reports/pilot-selected-evidence.json")
        if len(selected) != 190 or len({r["sample_id"] for r in selected}) != 190:
            raise ValueError("invalid archived pilot selection")
        if {r["sample_id"] for r in selected} != {r["sample_id"] for r in index if r["stage"]=="formal"}:
            raise ValueError("intake selection differs from archived evidence")
        for entry in selected:
            item = by_id[entry["sample_id"]]
            base = "raw/"+entry["source_batch"]+"/"
            sample = evidence.json(base+entry["sample_path"])
            context = sample["capture_context"]
            operation = next(op for op in CATALOG if op.operation_id==sample["operation_id"])
            if (sample.get("is_mock") is not False or sample.get("source") != "linux-procfs"
                    or sample.get("status") != "success" or sample.get("feature_schema") != SCHEMA
                    or sample.get("collection_stage") != "formal" or sample["sample_id"] != entry["sample_id"]
                    or sample["operation_label"] != operation.action_spec["legacy_label"]
                    or sample["label_id"] != CATALOG.index(operation)):
                raise ValueError("archived sample source or frozen label differs")
            expected = {"sample_id":sample["sample_id"],"source_batch":entry["source_batch"],"sample_path":entry["sample_path"],
                        "stage":"formal","label":sample["operation_label"],"operation_id":sample["operation_id"],
                        "session_id":sample["session_id"],"app_instance_id":sample["app_instance_id"],**context,
                        "start_ns":sample["baseline_windows"][0]["start_monotonic_ns"],
                        "end_ns":sample["operation_windows"][-1]["end_monotonic_ns"]}
            if item != expected:
                raise ValueError("mutable intake index differs from archived truth")
            sample_directory = str(Path(entry["sample_path"]).parent.parent).replace("\\","/")
            archived = evidence.csv(base+sample_directory+"/export/native_vectors.csv",VECTOR_HEADER)
            if len(archived)!=1 or archived[0]["sample_id"]!=sample["sample_id"]:
                raise ValueError("archived vector identity mismatch")
            reference = np.asarray([float(archived[0][f"v{i:04d}"]) for i in range(2048)])
            if not np.isfinite(reference).all() or np.max(np.abs(reference-vectors[sample["sample_id"]]))>1e-9:
                raise ValueError("intake vector differs from previously verified archived vector")
            records.append({**item,"app_version":sample["app_version"],"capture_profile":sample["capture_profile"],
                            "page_size_bytes":sample["page_size_bytes"]})
    finally:
        evidence.close()
    records.sort(key=lambda r:r["sample_id"])
    inputs = {"archive_sha256":report["archive_sha256"],"intake_report_sha256":file_sha(directory/"intake_report.json"),
              "sample_index_sha256":file_sha(directory/"sample_index.json"),"native_vectors_sha256":file_sha(directory/"native_vectors.csv")}
    return records, np.asarray([vectors[r["sample_id"]] for r in records]), inputs


def fit_candidate(records, vectors):
    vectors = np.asarray(vectors,dtype=float)
    if vectors.shape != (190,2048) or not np.isfinite(vectors).all() or np.any(np.abs(np.linalg.norm(vectors,axis=1)-1)>1e-9):
        raise ValueError("190 finite normalized 2048-dimensional observations required")
    if len(records)!=190 or len({r["sample_id"] for r in records})!=190 or any(r.get("stage")!="formal" for r in records):
        raise ValueError("unique formal observations required; preflight cannot fit a model")
    expected={('independent','train'):54,('independent','validation'):18,('independent','test'):18,
              ('continuous','train'):60,('continuous','validation'):20,('continuous','test'):20}
    if Counter((r["subset"],r["split"]) for r in records)!=expected:
        raise ValueError("frozen document split/quota differs")
    family_splits, documents, sessions = {}, {}, {}
    for r in records:
        if family_splits.setdefault(r["family_id"],r["split"])!=r["split"]:
            raise ValueError("document family crosses splits")
        if documents.setdefault(r["document_id"],(r["family_id"],r["split"]))!=(r["family_id"],r["split"]):
            raise ValueError("document identity crosses families/splits")
        if sessions.setdefault(r["session_id"],r["split"])!=r["split"]:
            raise ValueError("session crosses splits")
        if r["label"] not in LABELS or CATALOG[LABELS.index(r["label"])].operation_id!=r["operation_id"]:
            raise ValueError("catalog label mapping differs")
    if len(documents)!=5 or len(family_splits)!=5 or Counter(split for _,split in documents.values())!={"train":3,"validation":1,"test":1}:
        raise ValueError("five distinct document families with 3/1/1 split required")
    independent=[r for r in records if r["subset"]=="independent"]
    if len(Counter((r["document_id"],r["label"]) for r in independent))!=90:
        raise ValueError("each document must cover all eighteen independent operations once")
    flows=defaultdict(list)
    for r in records:
        if r["subset"]=="continuous":flows[r["session_id"]].append(r)
    if len(flows)!=5:
        raise ValueError("five complete continuous sessions required")
    for flow in flows.values():
        flow.sort(key=lambda r:r["sequence_index"])
        if ([r["sequence_index"] for r in flow]!=list(range(20))
                or len({(r["document_id"],r["family_id"],r["app_instance_id"],r["unit_id"]) for r in flow})!=1
                or any(a["end_ns"]>=b["start_ns"] for a,b in zip(flow,flow[1:]))):
            raise ValueError("continuous session is incomplete, spliced or unordered")
    train=np.asarray([r["subset"]=="independent" and r["split"]=="train" for r in records])
    y=np.asarray([LABELS.index(r["label"]) for r in records])
    if Counter(y[train])!=dict.fromkeys(range(18),3):
        raise ValueError("three training observations per frozen class required")
    # No validation/test/continuous observation reaches fit_model.
    centers,mapping=algorithms.fit_model(vectors[train],y[train],LABELS,"class_centroid_cosine",0)
    if not np.array_equal(mapping,np.arange(18)):
        raise ValueError("unexpected class center mapping")
    return centers,train,y


def verify_candidate(directory):
    directory=Path(directory)
    manifest=json.loads((directory/"SHA256SUMS.json").read_text())
    if set(manifest)!={"model.json","centers.csv","evaluation.json","README.md"}:
        raise ValueError("candidate manifest must cover exactly the four public artifacts")
    for name,expected in manifest.items():
        if file_sha(directory/name)!=expected:raise ValueError("candidate file SHA-256 mismatch")
    model,centers=load_model(directory/"model.json",SCHEMA)
    if (model.get("platform")!="linux" or model.get("model_state")!="pilot_candidate_not_promoted"
            or model.get("training_sample_count")!=54 or model.get("center_count")!=18
            or model.get("vector_dim")!=2048 or model.get("threshold") is not None
            or model.get("centers_sha256")!=manifest["centers.csv"]
            or model["center_to_operation"]!={str(i):label for i,label in enumerate(LABELS)}):
        raise ValueError("candidate schema, label or parameter policy differs")
    if centers.shape!=(18,2048) or not np.isfinite(centers).all():
        raise ValueError("invalid saved class center dimensions")
    return model,centers


def train_pilot(intake, output):
    records,vectors,inputs=load_intake(intake)
    centers,train,y=fit_candidate(records,vectors)
    output=Path(output).expanduser().resolve()
    output.mkdir(parents=True,exist_ok=False)
    target=output/"model"; target.mkdir()
    with (target/"centers.csv").open("x",newline="",encoding="utf-8") as handle:
        writer=csv.writer(handle);writer.writerow(["center_id","operation_label"]+[f"v{i:04d}" for i in range(2048)])
        writer.writerows([i,LABELS[i],*[format(float(x),".17g") for x in center]] for i,center in enumerate(centers))
    train_ids=[r["sample_id"] for r,selected in zip(records,train) if selected]
    model={"schema_version":"cross_app.vma-cluster-model.v1","model_id":"linux-wps-writer-pilot-20261023-v1",
           "app_id":"wps","platform":"linux","method":"class_centroid_cosine","feature_schema":SCHEMA,
           "vector_dim":2048,"center_count":18,"center_csv":"centers.csv",
           "centers_sha256":file_sha(target/"centers.csv"),"center_to_operation":{str(i):label for i,label in enumerate(LABELS)},
           "operation_ids":{label:op.operation_id for label,op in zip(LABELS,CATALOG)},
           "threshold":None,"smoothing":None,"model_state":"pilot_candidate_not_promoted",
           "native_runtime_ready":False,"training_date":"2026-10-23","training_sample_count":54,
           "training_counts":dict(Counter(r["label"] for r,selected in zip(records,train) if selected)),
           "fit_scope":"formal independent train documents doc1-3 only; no preflight or continuous data; no all-data refit",
           "training_documents":sorted({r["document_id"] for r,selected in zip(records,train) if selected}),
           "training_families":sorted({r["family_id"] for r,selected in zip(records,train) if selected}),
           "training_ids_sha256":digest(sorted(train_ids)),"source_inputs_sha256":inputs,
           "trainer_sha256":file_sha(Path(__file__)),"python_version":platform.python_version(),"numpy_version":np.__version__,
           "capture_profile":records[0]["capture_profile"],"wps_version":records[0]["app_version"],
           "page_size_bytes":records[0]["page_size_bytes"],
           "evaluation_scope":"exploratory controlled windows; doc4/5 outcomes previously observed; no cross-restart or online claim"}
    atomic_json(target/"model.json",model)
    loaded,saved=load_model(target/"model.json",SCHEMA)
    actual=predict(vectors,loaded,saved)
    reference=algorithms.norm_rows(vectors)@centers.T
    chosen=np.argmax(reference,axis=1)
    score_error=max(abs(score-float(reference[i,chosen[i]])) for i,(_,score) in enumerate(actual))
    if [label for label,_ in actual]!=[LABELS[i] for i in chosen] or score_error>1e-9:
        raise ValueError("saved/reloaded model differs from fitted parameters")
    pred=np.asarray([LABELS.index(label) for label,_ in actual])
    groups={}
    for subset in ("independent","continuous"):
        for split in ("train","validation","test"):
            mask=np.asarray([r["subset"]==subset and r["split"]==split for r in records])
            result=algorithms.metrics(y[mask],pred[mask],LABELS)
            result.update(windows=int(mask.sum()),correct=int((y[mask]==pred[mask]).sum()),
                          used_for_fit=subset=="independent" and split=="train")
            groups[f"{subset}/{split}"]=result
    continuous=np.asarray([r["subset"]=="continuous" for r in records])
    per_document={}
    for document in sorted({r["document_id"] for r in records}):
        mask=np.asarray([r["subset"]=="continuous" and r["document_id"]==document for r in records])
        per_document[document]={"windows":int(mask.sum()),"correct":int((y[mask]==pred[mask]).sum()),
                                "accuracy":float(np.mean(y[mask]==pred[mask]))}
    evaluation={"schema_version":"linux.wps.pilot-evaluation.v1","model_id":model["model_id"],"training_windows":54,
                "evaluated_selected_windows":190,"excluded_preflight":36,"included_extra_source_samples":0,
                "no_tuning_or_candidate_search":True,"source_archive_sha256":inputs["archive_sha256"],
                "saved_reload_max_score_difference":score_error,"groups":groups,"continuous_by_document":per_document,
                "continuous_overall":{"windows":100,"correct":int((y[continuous]==pred[continuous]).sum()),
                                      "accuracy":float(np.mean(y[continuous]==pred[continuous]))},
                "scope":model["evaluation_scope"],"final_generalization_estimate":False}
    atomic_json(target/"evaluation.json",evaluation)
    (target/"README.md").write_text("# Linux WPS Writer pilot v1\n\n"
        "18 class centroids × 2048 dimensions; L2 normalization and cosine argmax.\n"
        "Fitted only on 54 independent train observations (doc1-3), three per class.\n"
        "36 separate preflight and 100 continuous observations never fit parameters.\n"
        "doc4/5 and continuous scores are exploratory; their outcomes were previously viewed.\n"
        "No threshold, smoothing, all-data refit or online/cross-restart validation.\n\n"
        "Load explicitly using classification.predict with --schema wps.vma.feature.v1.\n"
        "Verify SHA256SUMS.json using scripts.linux_train verify before inference.\n"
        "Native's frozen registry does not load this candidate; no service model was replaced.\n",encoding="utf-8")
    atomic_json(target/"SHA256SUMS.json",{name:file_sha(target/name) for name in ("model.json","centers.csv","evaluation.json","README.md")})
    verify_candidate(target)
    atomic_json(output/"training_manifest.json",{"source_inputs_sha256":inputs,"training_sample_ids":train_ids,
                "records":[{**r,"used_for_fit":bool(selected)} for r,selected in zip(records,train)]})
    with (output/"predictions.csv").open("x",newline="",encoding="utf-8") as handle:
        writer=csv.writer(handle);writer.writerow(["sample_id","subset","split","truth","predicted","cosine_similarity","used_for_fit"])
        writer.writerows([r["sample_id"],r["subset"],r["split"],r["label"],label,format(score,".17g"),bool(selected)]
                         for r,(label,score),selected in zip(records,actual,train))
    return evaluation
