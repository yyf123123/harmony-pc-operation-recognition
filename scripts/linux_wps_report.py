"""Prepare label-free compact replay, run Native profiling, and report evidence.

Only prepare/report need NumPy (already used by the offline evaluator). The
benchmark worker uses stdlib and the compiled C++ executable on Linux.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import statistics
import subprocess
import time
import zipfile

PROFILE="linux-wps-writer-pilot-20261023-v1"


def sha(path):
    result=hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda:handle.read(1024*1024),b""):result.update(block)
    return result.hexdigest()


def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def stats(values):
    values=list(values)
    if not values or any(not math.isfinite(v) or v<0 for v in values):
        raise ValueError("timings must be finite nonnegative observations")
    ordered=sorted(values)
    def quantile(q):
        index=(len(ordered)-1)*q;lo=math.floor(index);hi=math.ceil(index)
        return ordered[lo]+(ordered[hi]-ordered[lo])*(index-lo)
    return {"n":len(values),"mean":statistics.fmean(values),"p50":quantile(.5),"p95":quantile(.95),"min":ordered[0],"max":ordered[-1]}


def capture_sample_timings(sample):
    windows=sample["baseline_windows"]+sample["operation_windows"]
    if [w["segment_label"] for w in windows]!=["BASELINE_01","BASELINE_02","ACTION","POST_ACTION"]:
        raise ValueError("complete four-phase sample required for timing")
    elapsed=[(w["end_monotonic_ns"]-w["start_monotonic_ns"])/1e9 for w in windows]
    if any(a["end_monotonic_ns"]>=b["start_monotonic_ns"] for a,b in zip(windows,windows[1:])):
        raise ValueError("overlapping phases")
    action=windows[2]
    action_s=(action["action_end_monotonic_ns"]-action["action_start_monotonic_ns"])/1e9
    verify_s=(action["verification_end_monotonic_ns"]-action["action_end_monotonic_ns"])/1e9
    span=(windows[-1]["end_monotonic_ns"]-windows[0]["start_monotonic_ns"])/1e9
    # Wall span has different boundaries: before first focus -> after last
    # focus, not model inference/output. Do not add nested command durations.
    wall=(datetime.fromisoformat(sample["sample_ended_at"])-datetime.fromisoformat(sample["sample_started_at"])).total_seconds()
    precheck=(datetime.fromisoformat(sample["sample_started_at"])-datetime.fromisoformat(sample["created_at"])).total_seconds()
    result=dict(zip(("baseline_01_s","baseline_02_s","action_window_s","post_window_s"),elapsed))
    result.update(clear_refs_nested_s=sum(w["T_clear_refs_s"] for w in windows),
                  collector_read_parse_save_nested_s=sum(w["T_device_sampling_s"] for w in windows),
                  action_command_nested_s=action_s,semantic_verification_nested_s=verify_s,
                  inter_phase_overhead_s=span-sum(elapsed),capture_monotonic_span_s=span,
                  capture_wall_span_s=wall,created_to_first_window_s=precheck,
                  action_start_to_last_sample_s=(windows[-1]["end_monotonic_ns"]-action["action_start_monotonic_ns"])/1e9)
    if any(not math.isfinite(v) or v<0 for v in result.values()):raise ValueError("invalid timing evidence")
    return result


def prepare(intake,output,model):
    from classification.linux_pilot import load_intake,verify_candidate
    from collector.linux.intake import EvidenceZip
    records,_,inputs=load_intake(intake)
    model_info,_=verify_candidate(model)
    if model_info["model_id"]!=PROFILE:raise ValueError("pinned pilot model required")
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    payload=output/"payload";payload.mkdir()
    metrics=defaultdict(list);samples=[];units=defaultdict(list);expected=[]
    evidence=EvidenceZip(Path(intake)/"source.zip")
    try:
        journal={}
        for batch in {r["source_batch"] for r in records}:
            journal[batch]={r["unit_id"]:r for r in map(json.loads,evidence.read("raw/"+batch+"/journal.jsonl").splitlines()) if r["event"]=="passed"}
        with (payload/"observations.csv").open("x",newline="",encoding="utf-8") as master:
            table=csv.writer(master);table.writerow(["sample_id","window_manifest"])
            for record in records:
                sid=record["sample_id"]
                if not re.fullmatch("[0-9a-f]{24}",sid):raise ValueError("unsafe sample identity")
                base="raw/"+record["source_batch"]+"/"
                sample=evidence.json(base+record["sample_path"])
                directory=payload/"windows"/sid;directory.mkdir(parents=True)
                raw_directory=str(Path(record["sample_path"]).parent.parent/"raw").replace("\\","/")
                pids=[p["pid"] for p in sample["processes"]]
                # This received pilot has one selected WPS process. Never drop
                # another PID silently to make an unsupported manifest fit.
                if len(pids)!=1:raise ValueError("split multi-PID compact files explicitly before this pilot profiler")
                with (directory/"manifest.csv").open("x",newline="",encoding="utf-8") as window:
                    writer=csv.writer(window);writer.writerow(["phase","index","pid","page_size","compact_vma"])
                    for phase,index,name in (("baseline",0,"BASELINE_01"),("baseline",1,"BASELINE_02"),("action",0,"ACTION"),("post",0,"POST_ACTION")):
                        filename=name+".tsv"
                        (directory/filename).write_bytes(evidence.read(base+raw_directory+"/"+filename))
                        writer.writerow([phase,index,pids[0],sample["page_size_bytes"],filename])
                table.writerow([sid,f"windows/{sid}/manifest.csv"])
                measured=capture_sample_timings(sample)
                for name,value in measured.items():metrics[name].append(value)
                samples.append({"sample_id":sid,**measured})
                units[(record["source_batch"],record["unit_id"])].append((record,measured))
                expected.append(record)
        residual=defaultdict(list)
        for (batch,uid),items in units.items():
            receipt=journal[batch][uid]
            if len(receipt["samples"])!=len(items):raise ValueError("partial receipt cannot be timed as a complete unit")
            total=receipt["elapsed_ns"]/1e9
            remainder=total-sum(t["capture_wall_span_s"] for _,t in items)
            if remainder<0:raise ValueError("unit elapsed less than captured wall spans")
            kind=items[0][0]["subset"]
            residual[kind+"_unit_total_s"].append(total)
            residual[kind+"_unseparated_prepare_export_remainder_s"].append(remainder)
            if kind=="continuous":residual["continuous_amortized_unit_total_s"].append(total/len(items))
        save(output/"capture_timing.json",{"source":"producer Linux monotonic/wall timing evidence, not a new live run",
             "selected_windows":190,"preflight_excluded":36,"units":"seconds","phase_stats":{k:stats(v) for k,v in metrics.items()},
             "unit_stats":{k:stats(v) for k,v in residual.items()},"samples":samples,
             "warning":"clear/collector/action/verification are nested within windows; do not add them again; old feature/export time cannot be separated from the unit remainder"})
        save(output/"expected_local_only.json",expected)
        save(output/"input_provenance.json",{"inputs":inputs,"model_sha256":sha(Path(model)/"model.json"),
             "centers_sha256":sha(Path(model)/"centers.csv"),"payload_scope":"190 selected formal compact TSVs and unlabeled manifests; no documents or semantic text",
             "payload_files":{str(p.relative_to(payload)):sha(p) for p in sorted(payload.rglob("*")) if p.is_file()}})
        save(payload/"SHA256SUMS.json",json.loads((output/"input_provenance.json").read_text())["payload_files"])
    finally:evidence.close()
    return {"observations":190,"payload":str(payload),"output":str(output)}


def benchmark(payload,native,models,output,repeats=5):
    # Stdlib-only worker. Suitable for the existing server without installing
    # dependencies, opening a desktop, reading /proc targets or clearing refs.
    payload=Path(payload).resolve()
    def check_payload():
        hashes=json.loads((payload/"SHA256SUMS.json").read_text())
        files={str(p.relative_to(payload)) for p in payload.rglob("*") if p.is_file() and p!=payload/"SHA256SUMS.json"}
        if set(hashes)!=files:raise ValueError("profiling payload file coverage changed")
        for name,expected in hashes.items():
            if Path(name).is_absolute() or ".." in Path(name).parts or sha(payload/name)!=expected:
                raise ValueError("profiling payload hash/path mismatch")
    check_payload()
    began=time.monotonic_ns()
    command=[str(Path(native).resolve()),"--models",str(Path(models).resolve()),"--model-profile",PROFILE,
             "--manifest",str(Path(payload).resolve()/"observations.csv"),"--output",str(Path(output).resolve()),
             "--warmup","1","--repeats",str(repeats)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=1200,check=False)
    elapsed=time.monotonic_ns()-began
    if result.returncode:raise RuntimeError("Native benchmark failed: "+result.stderr[:1000])
    info=json.loads(result.stdout)
    if info.get("status")!="ok" or info.get("observations")!=190 or info.get("model_profile")!=PROFILE:
        raise ValueError("unexpected benchmark coverage/model")
    info.update(platform=platform.system(),architecture=platform.machine(),kernel=platform.release(),
                python=platform.python_version(),native_sha256=sha(native),models_root=str(Path(models).resolve()),
                model_sha256=sha(Path(models)/"linux/wps_writer_pilot_v1/model.json"),
                centers_sha256=sha(Path(models)/"linux/wps_writer_pilot_v1/centers.csv"),
                process_elapsed_ns=elapsed)
    check_payload()
    save(Path(output)/"execution.json",info)
    return info


def report(run,benchmark_dir,intake,model):
    import numpy as np
    from classification.linux_pilot import load_intake,verify_candidate,LABELS
    from classification.predict import predict
    from classification.algorithms import metrics
    records,expected_vectors,inputs=load_intake(intake)
    info,centers=verify_candidate(model)
    run=Path(run);source=Path(benchmark_dir)
    execution=json.loads((source/"execution.json").read_text())
    if (execution["observations"]!=190 or execution["model_profile"]!=PROFILE
            or execution["model_sha256"]!=sha(Path(model)/"model.json")
            or execution["centers_sha256"]!=sha(Path(model)/"centers.csv")):
        raise ValueError("benchmark did not use this exact candidate")
    if json.loads((run/"input_provenance.json").read_text())["inputs"]!=inputs:
        raise ValueError("timing preparation and report used different intake evidence")
    ids=[r["sample_id"] for r in records];expected_ids=set(ids)
    def read(name):
        with (source/name).open(newline="",encoding="utf-8") as f:return list(csv.DictReader(f))
    def by_id(rows):
        if len(rows)!=190 or {r["sample_id"] for r in rows}!=expected_ids:raise ValueError("Native output identity coverage differs")
        return {r["sample_id"]:r for r in rows}
    vectors=by_id(read("vectors.csv"));predictions=by_id(read("predictions.csv"))
    values=np.asarray([[float(vectors[sid][f"v{i:04d}"]) for i in range(2048)] for sid in ids])
    delta=float(np.max(np.abs(values-expected_vectors)))
    if not np.isfinite(values).all() or delta>1e-9:raise ValueError("Native compact-to-vector parity failed")
    reference=predict(expected_vectors,info,centers)
    score_delta=max(abs(float(predictions[sid]["cosine_similarity"])-score) for sid,(_,score) in zip(ids,reference))
    if score_delta>1e-9 or any(predictions[sid]["operation_label"]!=label for sid,(label,_) in zip(ids,reference)):
        raise ValueError("Native candidate prediction differs from saved Python model")
    timing=read("timings.csv");groups=defaultdict(list);seen=set()
    for row in timing:
        key=(row["sample_id"],int(row["repeat"]))
        if key in seen or key[0] not in expected_ids or not 0<=key[1]<execution["repeats"]:raise ValueError("duplicate/unknown timing observation")
        seen.add(key)
        for name,value in row.items():
            if name.endswith("_ns"):groups[name].append(float(value)/1e6)
        parts=sum(int(row[name]) for name in row if name.endswith("_ns") and name!="total_ns")
        if parts>int(row["total_ns"]):raise ValueError("overlapping compute timer definitions")
    if len(seen)!=190*execution["repeats"]:raise ValueError("incomplete profiling repetitions")
    y=np.asarray([LABELS.index(r["label"]) for r in records]);pred=np.asarray([LABELS.index(predictions[sid]["operation_label"]) for sid in ids])
    accuracy={}
    for subset in ("independent","continuous"):
        for split in ("train","validation","test"):
            selected=np.asarray([r["subset"]==subset and r["split"]==split for r in records])
            score=metrics(y[selected],pred[selected],LABELS)
            score.update(n=int(selected.sum()),correct=int((y[selected]==pred[selected]).sum()),used_for_training=subset=="independent" and split=="train")
            accuracy[subset+"/"+split]=score
    capture=json.loads((run/"capture_timing.json").read_text())
    result={"schema":"linux.wps.progress-profile.v1","model_profile":PROFILE,"platform":execution,
            "accuracy":accuracy,"compute_timing_ms":{key:stats(value) for key,value in groups.items()},
            "capture_timing":capture,"parity":{"vector_max_abs":delta,"score_max_abs":score_delta,"labels_matched":190,"tolerance":1e-9},
            "live_wps_run_this_turn":False,"autonomous_segmentation_validated":False,
            "accuracy_scope":"previously observed document holdouts; one machine/app instance; controlled scripted operations only",
            "timing_scope":"producer live capture records and separate Native replay; do not add across machines as measured live end-to-end"}
    save(run/"progress_report.json",result)
    c=capture["phase_stats"];m=result["compute_timing_ms"]
    lines=["# Linux WPS 操作反推：进展与分阶段耗时","","日期：2026-10-28。已完成 Linux 候选的 Native 显式加载、紧凑 VMA → 语义键 → baseline/excess → 2048 维 → 分类与逐阶段计时；这次没有新的真实 WPS 桌面操作。", "",
           "## 准确率（Native 重放既有真实样本）","","| 分组 | 正确数 | Accuracy | Macro-F1 |","|---|---:|---:|---:|"]
    for key in ("independent/validation","independent/test","continuous/train","continuous/validation","continuous/test"):
        s=accuracy[key];lines.append(f"| {key} | {s['correct']}/{s['n']} | {s['accuracy']:.2%} | {s['macro_f1']:.4f} |")
    continuous_correct=sum(accuracy["continuous/"+split]["correct"] for split in ("train","validation","test"))
    lines += ["",f"54 个独立 train 样本参与拟合，不作为泛化准确率；100 个连续样本合计 {continuous_correct}/100（{continuous_correct}%），其中训练文档上的流程与留出文档分开报告。结果属于小试诊断，doc4/5 结果此前已被查看，不是未触碰最终测试集。", "",
              "## 采集机上的真实窗口耗时（190 样本，秒）","","这些数值来自慧京归档的 Linux 计时证据，不是本轮重新采集。","","| 阶段 | 平均 | P50 | P95 |","|---|---:|---:|---:|"]
    names={"baseline_01_s":"基线 1（含清位/等待/读取）","baseline_02_s":"基线 2（含清位/等待/读取）","action_window_s":"操作观察窗口","post_window_s":"操作后观察窗口",
           "inter_phase_overhead_s":"窗口间焦点检查/写文件等间隔","capture_wall_span_s":"采样开始到 post 完成（不含后续推理/导出）",
           "action_command_nested_s":"其中：动作命令执行","semantic_verification_nested_s":"其中：语义验证命令",
           "clear_refs_nested_s":"其中：四次清引用合计","collector_read_parse_save_nested_s":"其中：四次采集/解析/写原始文件合计"}
    for key,name in names.items():
        s=c[key];lines.append(f"| {name} | {s['mean']:.4f} | {s['p50']:.4f} | {s['p95']:.4f} |")
    lines += ["","“其中”均已包含在观察窗口中，不能再次相加；动作和验证在 15 秒 action 内完成。准备、原 Python 特征、导出和 CLI 启动未分别埋点，不能伪造逐项实测值。", "",
              "## 原始任务单元耗时（归档 receipt，秒）","","包含生产方当时的 Python/导出/原模型 CLI 工作，不是本轮 Native 新模型的在线时延。","","| 单元 | 数量 | 平均 | P50 | P95 |","|---|---:|---:|---:|---:|"]
    for key,name in (("independent_unit_total_s","独立：前置恢复到保存/预测完成"),("continuous_unit_total_s","连续：整个 20 步会话"),("continuous_amortized_unit_total_s","连续：会话均摊每步，非单步实测")):
        s=capture["unit_stats"][key];lines.append(f"| {name} | {s['n']} | {s['mean']:.4f} | {s['p50']:.4f} | {s['p95']:.4f} |")
    lines += ["","## Native 离线重放耗时（毫秒）","",f"环境：{execution['platform']} {execution['architecture']}，内核 {execution['kernel']}；190 个样本，1 次全量预热、{execution['repeats']} 次测量，共 {len(timing)} 条。模型在该进程首次校验/加载 {execution['model_load_ns']/1e6:.4f} ms，不包含在每窗口热重放中；没有清理操作系统缓存。","",
              "| 阶段 | 平均 | P50 | P95 |","|---|---:|---:|---:|"]
    native_names={"file_read_ns":"读取四份 compact TSV","compact_parse_ns":"解析 Referenced/PID/实际页数","semantic_ns":"语义键与 VMA 聚合","baseline_ns":"基线中位数","excess_ns":"操作超额页计算","vector_ns":"特征哈希/log1p/L2","inference_ns":"18 类中心余弦分类","output_ns":"JSON 序列化/写入/flush","total_ns":"一次完整离线重放（含上述阶段及管理开销）"}
    for key,name in native_names.items():
        s=m[key];lines.append(f"| {name} | {s['mean']:.4f} | {s['p50']:.4f} | {s['p95']:.4f} |")
    lines += ["","输出测的是文件流 flush 到页缓存，不是 fsync、宿主 IPC 或设备存储持久化；统计 CSV 和向量诊断输出不计入每窗口计时。阶段 P50/P95 不能相加成总量分位数。", "",
              "## 运行器计时与剩余验收","","新的受控运行器会在结果中给出特征/推理/最终检查耗时；输出和历史更新完成后才能知道它们的耗时，因此通过 health 的 last_success_timing_ns 查询，结果发出前不填假值。输出失败或窗口失效时，不生成有效成功计时。", "",
              "真实采集证据和离线重放来自不同执行环境，不能相加后称新版本的实测端到端时延。当前协议仅观察窗口就至少 30 秒；分类快不代表自由操作实时识别已解决。真实 WPS 在线、跨重启、RPC 观测负载迁移、自主分段及下一步预测提前量仍需桌面会话验证。", "",
              f"Python/Native：190 标签一致，向量最大误差 {delta:.3g}，分数最大误差 {score_delta:.3g}；模型参数没有重训或覆盖，默认模型没有切换。"]
    (run/"REPORT.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return {"report":str(run/"REPORT.md"),"compute_p50_ms":m["total_ns"]["p50"],"capture_p50_s":c["capture_wall_span_s"]["p50"],"parity":result["parity"]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    p=commands.add_parser("prepare");p.add_argument("--intake",type=Path,required=True);p.add_argument("--output",type=Path,required=True);p.add_argument("--model",type=Path,required=True)
    b=commands.add_parser("benchmark");b.add_argument("--payload",type=Path,required=True);b.add_argument("--native",type=Path,required=True);b.add_argument("--models",type=Path,required=True);b.add_argument("--output",type=Path,required=True);b.add_argument("--repeats",type=int,default=5)
    r=commands.add_parser("report");r.add_argument("--run",type=Path,required=True);r.add_argument("--benchmark",type=Path,required=True);r.add_argument("--intake",type=Path,required=True);r.add_argument("--model",type=Path,required=True)
    args=parser.parse_args()
    if args.command=="prepare":result=prepare(args.intake,args.output,args.model)
    elif args.command=="benchmark":result=benchmark(args.payload,args.native,args.models,args.output,args.repeats)
    else:result=report(args.run,args.benchmark,args.intake,args.model)
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":main()
