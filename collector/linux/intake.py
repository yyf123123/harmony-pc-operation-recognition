"""Read-only intake of an external, selected WPS pilot. Never executes archive code.

This is deliberately separate from the complete 1500-window batch exporter.
It checks received evidence and replays frozen math; it does not train a model.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import zipfile

from apps.wps.adapter import WpsAdapter
from collector.linux.batch import atomic_json, digest, file_sha
from collector.linux.history import OperationHistory, event_from_prediction
from collector.linux.procfs import parse_compact
from features.wps import build_vector, l2_normalize, map_fixed_window_sequence

VECTOR_HEADER = ["sample_id"] + [f"v{i:04d}" for i in range(2048)]
PHASES = ("BASELINE_01", "BASELINE_02", "ACTION", "POST_ACTION")
CATALOG = tuple(WpsAdapter().catalog.operations)


def relative_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or "\0" in name
            or PurePosixPath(name).is_absolute() or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ValueError("unsafe archive or receipt path")
    return name


def strict_json(text):
    def reject(value):
        raise ValueError("nonfinite JSON")
    return json.loads(text, parse_constant=reject)


class EvidenceZip:
    def __init__(self, archive):
        self.zip = zipfile.ZipFile(archive)
        self.names = set()
        try:
            entries = self.zip.infolist()
            if len(entries) > 50000 or sum(x.file_size for x in entries) > 4 * 1024**3:
                raise ValueError("archive exceeds finite intake limits")
            for entry in entries:
                name = relative_name(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                if name in self.names or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("duplicate, encrypted or symlink archive entry")
                if entry.file_size > 64 * 1024**2:
                    raise ValueError("individual entry exceeds intake limit")
                self.names.add(name)
            self.files = {e.filename for e in entries if not e.is_dir()}
        except BaseException:
            self.zip.close()
            raise

    def close(self):
        self.zip.close()

    def read(self, name):
        return self.zip.read(relative_name(name))

    def json(self, name):
        return strict_json(self.read(name))

    def verify_manifest(self):
        self.hashes = self.json("SHA256SUMS.json")
        if not isinstance(self.hashes, dict) or set(self.hashes) != self.files - {"SHA256SUMS.json"}:
            raise ValueError("archive manifest does not cover exactly all received files")
        for name, expected in self.hashes.items():
            relative_name(name)
            hasher = hashlib.sha256()
            with self.zip.open(name) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.hexdigest() != expected:
                raise ValueError("archive file SHA-256 mismatch")
        return len(self.hashes)

    def csv(self, name, header=None):
        reader = csv.DictReader(io.StringIO(self.read(name).decode("utf-8")))
        rows = list(reader)
        if header is not None and reader.fieldnames != header:
            raise ValueError("unexpected CSV schema")
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError("incomplete CSV")
        return rows


def vector_row(rows, sample_id):
    if len(rows) != 1 or rows[0]["sample_id"] != sample_id:
        raise ValueError("vector identity mismatch")
    vector = [float(rows[0][f"v{i:04d}"]) for i in range(2048)]
    if not all(math.isfinite(v) for v in vector) or abs(math.sqrt(sum(v*v for v in vector))-1) > 1e-9:
        raise ValueError("invalid finite normalized vector")
    return vector


def verify_journal(evidence, batch):
    base = "raw/" + relative_name(batch) + "/"
    config, binding = evidence.json(base+"config.json"), evidence.json(base+"binding.json")
    schedule = evidence.json(base+"schedule.json")
    planned = {unit["unit_id"]: unit for unit in schedule}
    if len(planned) != len(schedule):
        raise ValueError("duplicate scheduled unit")
    if "config_sha256" in binding:
        if binding["config_sha256"] != digest(config) or binding.get("fixture_mode") is not False:
            raise ValueError("configuration binding or real-source flag mismatch")
    elif binding.get("config") != config or binding.get("schedule") != schedule or binding.get("is_mock") is not False:
        raise ValueError("pilot configuration/schedule binding mismatch")
    data = evidence.read(base+"journal.jsonl")
    if not data.endswith(b"\n"):
        raise ValueError("truncated evidence journal")
    receipts, samples, started, terminal = {}, {}, Counter(), set()
    events = Counter()
    for line in data.splitlines():
        record = strict_json(line)
        uid, attempt = record["unit_id"], record["attempt"]
        if uid not in planned or type(attempt) is not int or attempt <= 0:
            raise ValueError("journal identity is outside schedule")
        event = record["event"]
        events[event] += 1
        if event == "started":
            if uid in receipts or attempt != started[uid]+1 or (started[uid] and (uid, started[uid]) not in terminal):
                raise ValueError("inconsistent attempt sequence")
            started[uid] = attempt
            continue
        if event not in {"passed", "failed", "abandoned"} or attempt != started[uid] or (uid, attempt) in terminal:
            raise ValueError("inconsistent journal completion")
        terminal.add((uid, attempt))
        if event != "passed":
            continue
        unit = planned[uid]
        if record["stage"] != unit["stage"] or len(record["samples"]) != len(unit["operations"]) or not record["files"]:
            raise ValueError("committed receipt differs from scheduled unit")
        prefix = f"attempts/{uid}/try-{attempt:03d}/"
        for name, expected in record["files"].items():
            if not relative_name(name).startswith(prefix) or evidence.hashes.get(base+name) != expected:
                raise ValueError("receipt hash or attempt boundary mismatch")
        for name, operation in zip(record["samples"], unit["operations"]):
            if name in samples or name not in record["files"]:
                raise ValueError("duplicate or unhashed committed sample")
            sample = evidence.json(base+name)
            if sample["operation_id"] != operation:
                raise ValueError("receipt operation order mismatch")
            samples[name] = (unit, record)
        receipts[uid] = record
    return {"base": base, "config": config, "binding": binding, "schedule": schedule,
            "samples": samples, "receipts": receipts, "journal_events": dict(events),
            "uncommitted_attempts": sum((uid, count) not in terminal for uid,count in started.items())}


def checked_sample(evidence, batch, name):
    unit, receipt = batch["samples"][name]
    base = batch["base"]
    sample = evidence.json(base+name)
    context = sample["capture_context"]
    catalog = next(op for op in CATALOG if op.operation_id == sample["operation_id"])
    if (sample.get("status") != "success" or sample.get("is_mock") is not False
            or sample.get("source") != "linux-procfs" or sample.get("app_id") != "wps"
            or sample.get("feature_schema") != "wps.vma.feature.v1"
            or sample["operation_label"] != catalog.action_spec["legacy_label"] or sample["label_id"] != CATALOG.index(catalog)
            or sample["collection_stage"] != unit["stage"]
            or any(context[key] != unit[key] for key in ("unit_id", "document_id", "family_id", "split", "subset", "group_index"))):
        raise ValueError("sample provenance/catalog mismatch")
    profile = sample["capture_profile"]
    if profile != {"name":"linux.wps.fixed_windows.v1", "baseline_count":2, "baseline_s":5, "action_s":15, "post_s":5}:
        raise ValueError("unexpected capture profile")
    processes = sample["processes"]
    pids = {p["pid"] for p in processes}
    if len(pids) != len(processes) or any(type(p["pid"]) is not int or p["pid"] <= 0 or type(p["start_ticks"]) is not int or p["start_ticks"] <= 0 for p in processes):
        raise ValueError("invalid process start identity")
    bound = batch["binding"]["inputs"]
    if processes != bound["processes"] or sample["page_size_bytes"] != bound["page_size_bytes"]:
        raise ValueError("sample process/page size differs from input binding")
    root = next(p for p in processes if p["pid"] == sample["root_pid"])
    if sample["app_instance_id"] != f'wps:{root["pid"]}:{root["start_ticks"]}':
        raise ValueError("root process identity mismatch")
    sample_dir = str(PurePosixPath(name).parent.parent)
    def bound_json(relative):
        if relative not in receipt["files"]:
            raise ValueError("unbound observation evidence")
        return evidence.json(base+relative)
    windows = sample["baseline_windows"]+sample["operation_windows"]
    if [w["segment_label"] for w in windows] != list(PHASES):
        raise ValueError("missing or reordered raw windows")
    end = -1
    durations = []
    for phase, window, duration in zip(PHASES, windows, (5,5,15,5)):
        start, finish = window["start_monotonic_ns"], window["end_monotonic_ns"]
        elapsed = (finish-start)/1e9
        sampling = window["T_device_sampling_s"]
        if (window["status"] != "success" or type(start) is not int or type(finish) is not int or start <= end
                or not math.isfinite(sampling) or sampling < 0 or not duration-0.001 <= elapsed <= duration+sampling+1):
            raise ValueError("invalid/overlapping window timing")
        end = finish
        durations.append(elapsed)
        for focus in (window["focus_start"],window["focus_end"]):
            if focus["pid"] not in pids or not focus["source"] or not focus["window_id"]:
                raise ValueError("missing selected-process focus evidence")
        if bound_json(sample_dir+f"/raw/{phase}.json") != window:
            raise ValueError("raw window JSON differs from sample")
        tsv = sample_dir+f"/raw/{phase}.tsv"
        if tsv not in receipt["files"]:
            raise ValueError("unbound original TSV")
        raw = parse_compact(evidence.read(base+tsv).decode("utf-8"),pids,sample["page_size_bytes"])
        if raw["feature_pages"] != window["feature_pages"]:
            raise ValueError("raw TSV does not reconstruct the saved semantic features")
    action = windows[2]
    if not action["start_monotonic_ns"] <= action["action_start_monotonic_ns"] <= action["action_end_monotonic_ns"] <= action["verification_end_monotonic_ns"] <= action["start_monotonic_ns"]+15_000_000_000:
        raise ValueError("action/verification exceeded action interval")
    attempt = str(PurePosixPath(sample_dir).parent)
    step = context["sequence_index"]
    observations = [bound_json(attempt+f"/semantic-evidence/{step:03d}-{phase}.json") for phase in ("before","observed","after")]
    if (any(o["operation"] != sample["operation_id"].rsplit(".",1)[1] for o in observations)
            or observations[-1].get("semantic_success") is not True
            or not action["action_end_monotonic_ns"] <= observations[-1]["monotonic_ns"] <= action["verification_end_monotonic_ns"]):
        raise ValueError("semantic verifier evidence mismatch")
    sequence = map_fixed_window_sequence(operation_execution_id=sample["sample_id"],operation_id=sample["operation_label"],
        baseline_group_id=sample["sample_id"]+".baseline",baseline_windows=windows[:2],operation_windows=windows[2:])
    aggregated = {(row["feature_namespace"],row["feature_key"]):float(row["aggregated_excess_pages"]) for row in sequence["aggregated_features"]}
    export = sample_dir+"/export/"
    for required in ("aggregated_features.csv","vma_vectors_l2.csv","native_vectors.csv"):
        if export+required not in receipt["files"]:
            raise ValueError("unbound derived feature")
    rows = evidence.csv(base+export+"aggregated_features.csv",["namespace","feature_key","pages"])
    if len(rows) != len(aggregated) or {(r["namespace"],r["feature_key"]):float(r["pages"]) for r in rows} != aggregated:
        raise ValueError("saved aggregate differs from reconstructed baseline/excess")
    vector = l2_normalize(build_vector({key:v for (ns,key),v in aggregated.items() if ns=="FILE"},
                                     {key:v for (ns,key),v in aggregated.items() if ns=="ANON"}))
    maximum = 0.0
    for filename in ("vma_vectors_l2.csv","native_vectors.csv"):
        saved = vector_row(evidence.csv(base+export+filename,VECTOR_HEADER),sample["sample_id"])
        maximum = max(maximum,max(abs(a-b) for a,b in zip(saved,vector)))
    if maximum > 1e-9:
        raise ValueError("raw-to-saved vector parity failed")
    prediction_name = sample_dir+"/prediction.csv"
    if prediction_name not in receipt["files"]:
        raise ValueError("unbound prediction")
    predictions = evidence.csv(base+prediction_name)
    if len(predictions) != 1 or bound_json(sample_dir+"/recognition.json") != event_from_prediction(sample,predictions[0]):
        raise ValueError("prediction/recognition semantic join mismatch")
    return sample, aggregated, vector, predictions[0], durations, maximum


def native_command(argv):
    completed = subprocess.run([str(arg) for arg in argv],capture_output=True,timeout=60,check=False)
    if completed.returncode:
        raise ValueError("frozen Native replay failed")


def intake_pilot(archive: Path, output: Path, build: Path, repo: Path):
    output = output.expanduser().resolve()
    output.mkdir(parents=True,exist_ok=False)
    # Retain a private source copy; nothing is extracted or executed from it.
    shutil.copyfile(archive,output/"source.zip")
    report = {"schema_version":"linux.wps.pilot-intake.v1", "status":"checking", "archive_sha256":file_sha(output/"source.zip"),
              "full_batch_complete":False, "resume_full_collection":False, "new_model_trained":False,
              "page_mapping_status":"unknown", "verification_scope":"received_evidence_and_offline_frozen_math"}
    evidence = None
    try:
        evidence = EvidenceZip(output/"source.zip")
        report["archive_files_verified"] = evidence.verify_manifest()
        report["tool_inputs_sha256"] = {
            "intake.py":file_sha(Path(__file__)),
            "procfs.py":file_sha(repo/"collector/linux/procfs.py"),
            "wps_features.py":file_sha(repo/"features/wps.py"),
            "operation_feature":file_sha(build/"native/operation_feature"),
            "operation_predict":file_sha(build/"native/operation_predict"),
            "wps_model.json":file_sha(repo/"models/wps/model.json"),
            "wps_centers.csv":file_sha(repo/"models/wps/centers.csv"),
        }
        selected = evidence.json("reports/pilot-selected-evidence.json")
        if not isinstance(selected,list) or len(selected) != 190:
            raise ValueError("this pilot intake requires the explicitly selected 190 observations")
        batches = {name:verify_journal(evidence,name) for name in {e["source_batch"] for e in selected}}
        pilot = next(b for b in batches.values() if "source_binding_sha256" in b["binding"])
        source = next(b for b in batches.values() if "config_sha256" in b["binding"])
        if pilot["binding"]["source_binding_sha256"] != hashlib.sha256(evidence.read(source["base"]+"binding.json")).hexdigest():
            raise ValueError("reused source binding mismatch")
        accepted = evidence.json(pilot["base"]+"accepted.json")
        if accepted["new_units"] != list(pilot["receipts"].values()):
            raise ValueError("accepted index differs from committed journal")
        if accepted["reused_source_receipts"] != pilot["binding"]["source_receipts"]:
            raise ValueError("reused source receipts differ from binding")
        for reused in accepted["reused_source_receipts"]:
            # The producer stores an old absolute Linux path as provenance.
            # Only map its basename to a received, hash-bound archive batch;
            # never access that external path on this host.
            batch = batches[PurePosixPath(reused["source_batch"]).name]
            uid = reused["unit"]["unit_id"]
            if (reused["receipt"] != batch["receipts"].get(uid)
                    or reused["unit"] != next(u for u in batch["schedule"] if u["unit_id"]==uid)):
                raise ValueError("reused receipt differs from source journal")
        documents = {d["document_id"]:d for d in pilot["config"]["documents"]}
        if source["config"]["documents"] != pilot["config"]["documents"] or Counter(d["split"] for d in documents.values()) != {"train":3,"validation":1,"test":1} or len({d["family_id"] for d in documents.values()}) != 5:
            raise ValueError("document family split mismatch")
        preflight = [(name,path) for name,b in batches.items() for path,(u,_) in b["samples"].items() if u["stage"]=="preflight"]
        if len(preflight) != 36:
            raise ValueError("separate preflight quota differs")
        entries = selected + [{"source_batch":b,"sample_path":p,"stage":"preflight"} for b,p in preflight]
        rows, predictions, sequences, metadata, durations = [], {}, defaultdict(list), [], []
        counts, labels = Counter(), Counter()
        ids, raw_max, native_max = set(), 0.0, 0.0
        with (output/"native_vectors.csv").open("x",newline="",encoding="utf-8") as handle:
            writer = csv.writer(handle); writer.writerow(VECTOR_HEADER)
            for entry in entries:
                batch = batches[entry["source_batch"]]
                sample, aggregated, vector, prediction, timing, delta = checked_sample(evidence,batch,entry["sample_path"])
                sid = sample["sample_id"]
                if sid in ids:
                    raise ValueError("overlapping selected/preflight sample identity")
                ids.add(sid); durations.extend(timing); raw_max = max(raw_max,delta)
                context = sample["capture_context"]
                if entry["stage"] != sample["collection_stage"]:
                    raise ValueError("selected stage mismatch")
                if entry["stage"] == "formal":
                    for key,actual in {"sample_id":sid,"operation_id":sample["operation_id"],"label":sample["operation_label"],"subset":context["subset"],"document_id":context["document_id"],"split":context["split"],"unit_id":context["unit_id"],"session_id":sample["session_id"],"sequence_index":context["sequence_index"],"instance":sample["app_instance_id"]}.items():
                        if entry.get(key) != actual:
                            raise ValueError("selected evidence identity mismatch")
                    doc = documents[context["document_id"]]
                    if context["family_id"] != doc["family_id"] or context["split"] != doc["split"]:
                        raise ValueError("sample document family mismatch")
                    counts[(context["subset"],context["split"])] += 1
                    if context["subset"] == "independent": labels[(context["document_id"],sample["operation_id"])] += 1
                # Replay Native from the raw-rebuilt semantic excess, never from labels.
                aggregated_file = output/"aggregate.csv"
                with aggregated_file.open("w",newline="") as temporary:
                    table=csv.writer(temporary); table.writerow(["namespace","feature_key","pages"])
                    table.writerows((ns,key,value) for (ns,key),value in sorted(aggregated.items()))
                native_file = output/"one-vector.csv"
                native_command([build/"native/operation_feature","--schema","wps.vma.feature.v1","--sample-id",sid,"--input",aggregated_file,"--output",native_file])
                with native_file.open(newline="") as temporary: native_vector=vector_row(list(csv.DictReader(temporary)),sid)
                native_file.unlink(); aggregated_file.unlink()
                native_max=max(native_max,max(abs(a-b) for a,b in zip(vector,native_vector)))
                if native_max > 1e-9: raise ValueError("Python/Native raw vector replay differs")
                writer.writerow([sid,*native_vector])
                predictions[sid]=prediction
                item={"sample_id":sid,"source_batch":entry["source_batch"],"sample_path":entry["sample_path"],
                      "stage":sample["collection_stage"],"operation_id":sample["operation_id"],"label":sample["operation_label"],
                      "session_id":sample["session_id"],"app_instance_id":sample["app_instance_id"],**context,
                      "start_ns":sample["baseline_windows"][0]["start_monotonic_ns"],"end_ns":sample["operation_windows"][-1]["end_monotonic_ns"]}
                rows.append(item)
                if entry["stage"] == "formal" and context["subset"] == "continuous": sequences[(entry["source_batch"],context["unit_id"])].append((sample,prediction))
                metadata.append((sample["app_version"],sample["architecture"],sample["kernel"],sample["page_size_bytes"],sample["app_instance_id"]))
        expected={('independent','train'):54,('independent','validation'):18,('independent','test'):18,('continuous','train'):60,('continuous','validation'):20,('continuous','test'):20}
        if counts != expected or len(labels) != 90 or any(n!=1 for n in labels.values()) or len(sequences)!=5 or len(set(metadata)) != 1:
            raise ValueError("pilot quotas, class coverage, session coverage or environment mismatch")
        native_command([build/"native/operation_predict","--models",repo/"models","--app","wps","--schema","wps.vma.feature.v1","--vectors",output/"native_vectors.csv","--output",output/"predictions.csv"])
        with (output/"predictions.csv").open(newline="") as handle: fresh={p["sample_id"]:p for p in csv.DictReader(handle)}
        score_max=0.0
        if set(fresh) != ids: raise ValueError("Native prediction coverage mismatch")
        for sid,p in fresh.items():
            old=predictions[sid]
            score_max=max(score_max,abs(float(p["cosine_similarity"])-float(old["cosine_similarity"])))
            if p["operation_label"] != old["operation_label"] or score_max > 1e-9:
                raise ValueError("received predictions differ from frozen Native replay")
        for (_,uid),items in sequences.items():
            items.sort(key=lambda pair:pair[0]["capture_context"]["sequence_index"])
            if [s["capture_context"]["sequence_index"] for s,_ in items] != list(range(20)) or len({(s["session_id"],s["app_instance_id"]) for s,_ in items})!=1:
                raise ValueError("continuous session is incomplete or spliced")
            history=OperationHistory(); history.reset(items[0][0]["session_id"],items[0][0]["app_instance_id"])
            events,truth,gaps=[],[],[]; last=-1
            for sample,_ in items:
                start=sample["baseline_windows"][0]["start_monotonic_ns"]
                if start <= last: raise ValueError("continuous windows overlap or are unordered")
                if last>0:gaps.append((start-last)/1e9)
                last=sample["operation_windows"][-1]["end_monotonic_ns"]
                event=event_from_prediction(sample,fresh[sample["sample_id"]]); history.append(event); events.append(event)
                truth.append({"sample_id":sample["sample_id"],"operation_id":sample["operation_id"],"end_monotonic_ns":last})
            directory=output/"sequences"/uid; directory.mkdir(parents=True)
            atomic_json(directory/"truth.json",{"sequence_kind":"scripted_controlled_sequence","operations":truth,"gap_seconds":gaps})
            with (directory/"recognition.jsonl").open("x") as handle:
                for event in events:handle.write(json.dumps(event,ensure_ascii=False)+"\n")
        metrics={}
        for subset in ("independent","continuous"):
            observations=[r for r in rows if r["stage"]=="formal" and r["subset"]==subset]
            correct=sum(fresh[r["sample_id"]]["operation_label"]==r["label"] for r in observations)
            metrics[subset]={"windows":len(observations),"correct":correct,"accuracy":correct/len(observations),
                "predicted_labels":dict(Counter(fresh[r["sample_id"]]["operation_label"] for r in observations))}
        atomic_json(output/"sample_index.json",rows)
        app,architecture,kernel,page,instance=metadata[0]
        report.update(status="partial_pilot_verified",selected_formal_windows=190,separate_preflight_windows=36,
            extra_committed_excluded=sum(len(b["samples"]) for b in batches.values())-len(ids),raw_windows_verified=4*len(ids),
            source_batches={name:{"committed_samples":len(b["samples"]),"events":b["journal_events"],"uncommitted_attempts":b["uncommitted_attempts"]} for name,b in batches.items()},
            splits={f"{a}/{b}":n for (a,b),n in counts.items()},continuous_sessions=5,
            environment={"wps_version":app,"architecture":architecture,"kernel":kernel,"page_size_bytes":page,"unique_application_instances":1},
            parity={"raw_to_saved_max_abs":raw_max,"python_native_max_abs":native_max,"prediction_score_max_abs":score_max,"tolerance":1e-9},
            window_elapsed_seconds={"minimum":min(durations),"maximum":max(durations)},frozen_native_metrics=metrics,
            delivered_diagnostic_baseline={"verification":"reported_not_retrained_or_independently_reproduced", "metrics":evidence.json("reports/pilot-quality-results.json")["heldout_independent"]},
            limitations=["semantic observations and verifier success records checked; live UI and verifier implementation not independently certified",
                         "single machine/application instance; document holdout is not independent restart/session generalization",
                         "RPC verification overhead is part of observations; free-user segmentation and prediction lead time untested",
                         "VMA Referenced is not precise per-file-page access evidence"])
    except BaseException as error:
        report.update(status="rejected",error_type=type(error).__name__,reason=str(error))
        atomic_json(output/"intake_report.json",report)
        raise
    finally:
        if evidence is not None:evidence.close()
    atomic_json(output/"intake_report.json",report)
    return report
