"""Reuse the desktop owner's automation, keep truth outside Native, return timing.

Finite validation only; never starts a formal batch or trains a model. Python
stdlib only. The package supplies opsctl/models; the desktop supplies its already
verified six-role batch configuration and explicit current process IDs.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid
import zipfile

from apps.wps.adapter import WpsAdapter
from collector.linux.batch import expanded,validate_config,file_sha,atomic_json
from collector.linux.session import run_command

PROFILE="linux-wps-writer-pilot-20261023-v1"
QUICK=("wps.writer.write_text","wps.writer.save_document","wps.writer.format_bold")


def create_feedback(root):
    root=Path(root)
    # Fixed allowlist, never recurse into the external driver's working copies.
    with zipfile.ZipFile(root/"feedback.zip","x",compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("report.json","REPORT.md","truth.jsonl","health.jsonl","native-recognition.jsonl"):
            path=root/name
            if path.is_file() and not path.is_symlink():archive.write(path,name)
        for native_log in sorted(root.glob("operation-*/native.jsonl")):
            if native_log.is_file() and not native_log.is_symlink():
                archive.write(native_log,"native-details/"+native_log.parent.name+".jsonl")


class NativeStream:
    def __init__(self,argv):
        self.process=subprocess.Popen([str(x) for x in argv],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        self.selector=selectors.DefaultSelector();self.buffers={};self.closed=False;self.error=None
        for stream in (self.process.stdout,self.process.stderr):
            os.set_blocking(stream.fileno(),False);self.selector.register(stream,selectors.EVENT_READ);self.buffers[stream.fileno()]=b""

    def next(self,deadline):
        while time.monotonic()<deadline:
            for stream in list(self.buffers):
                data=self.buffers[stream]
                if b"\n" in data:
                    line,self.buffers[stream]=data.split(b"\n",1)
                    if not line:continue
                    record=json.loads(line)
                    if record.get("status")=="error":
                        self.error=record
                        raise RuntimeError("Native window failed: "+str(record.get("code",record.get("message","unknown"))))
                    return record
            for key,_ in self.selector.select(timeout=min(.2,max(0,deadline-time.monotonic()))):
                block=os.read(key.fileobj.fileno(),65536)
                if not block:self.selector.unregister(key.fileobj)
                else:
                    self.buffers[key.fileobj.fileno()]+=block
                    if len(self.buffers[key.fileobj.fileno()])>2*1024*1024:raise ValueError("Native diagnostic line exceeded limit")
            if not self.selector.get_map():raise RuntimeError("Native exited before a complete window/health result")
        raise TimeoutError("Native controlled window timed out")

    def close(self,normal=False):
        if self.closed:return
        try:
            if self.process.poll() is None:
                if normal:
                    try:self.process.stdin.write(b"STOP\n");self.process.stdin.flush()
                    except BrokenPipeError:pass
                else:self.process.send_signal(signal.SIGINT)
                # STOP prints bounded history. Drain pipes while waiting so
                # even an 8 KiB Linux pipe cannot deadlock normal shutdown.
                until=time.monotonic()+3
                while self.process.poll() is None and time.monotonic()<until:
                    for key,_ in self.selector.select(timeout=.1):
                        if not os.read(key.fileobj.fileno(),65536):self.selector.unregister(key.fileobj)
                try:self.process.wait(timeout=max(.1,until-time.monotonic()))
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
        finally:
            self.selector.close()
            for stream in (self.process.stdin,self.process.stdout,self.process.stderr):stream.close()
            self.closed=True
        if normal and self.process.returncode:raise RuntimeError("Native did not stop cleanly")


def action_stage(config,context,stage,window_s=15):
    # Linux steady_clock and Python monotonic share CLOCK_MONOTONIC. The window
    # begins before clear_refs, so only its remaining budget is available here.
    end=stage["monotonic_ns"]+int(window_s*1e9)
    result={}
    for role in ("action","verify"):
        remaining=(end-time.monotonic_ns())/1e9
        if remaining<=0:raise TimeoutError("action/verification missed the Native action deadline")
        began=time.monotonic_ns()
        run_command(expanded(config["automation"][role],context),remaining,role)
        result[role+"_ns"]=time.monotonic_ns()-began
    if time.monotonic_ns()>end:raise TimeoutError("verification ended after the observation deadline")
    return result


def check(config,native,models,output_root,operations=QUICK,document_id=None):
    config=validate_config(config)
    if sys.platform!="linux":raise ValueError("desktop validation requires Linux")
    catalog=WpsAdapter().catalog
    operations=list(operations)
    if not operations or len(operations)>18 or len(set(operations))!=len(operations):raise ValueError("choose 1..18 distinct catalog operations")
    for op in operations:catalog.get(op)
    document=next((d for d in config["documents"] if d["document_id"]==document_id),None) if document_id else next(d for d in config["documents"] if d["split"]=="train")
    if document is None or document["split"]!="train":raise ValueError("calibrate only on an explicitly selected train test-copy template")
    root=Path(output_root).expanduser().resolve()/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")+uuid.uuid4().hex[:10])
    root.mkdir(parents=True,exist_ok=False)
    native=Path(native).resolve();models=Path(models).resolve()
    report={"schema":"linux.wps.desktop-validation.v1","status":"running","output":str(root),"model_profile":PROFILE,
            "formal_collection_enabled":False,"requested_operations":operations,"completed_valid_windows":0,
            "config_sha256":file_sha_data(config),"native_sha256":file_sha(native),"observations":[],
            "scope":"finite controlled desktop validation; semantic verifier implementation supplied by desktop owner"}
    stream=None
    def save():atomic_json(root/"report.json",report)
    save()
    try:
        # Read-only diagnosis; no clear_refs until Native receives a trigger.
        doctor=[str(native),"doctor","--models",str(models),"--app","wps","--model-profile",PROFILE]
        for pid in config["pids"]:doctor.extend(("--pid",str(pid)))
        run_command(doctor,30,"native doctor")
        template=Path(document["path"]);template_hash=file_sha(template)
        for number,operation in enumerate(operations):
            started=time.monotonic_ns();event_id=uuid.uuid4().hex
            state=root/f"operation-{number:02d}";state.mkdir()
            automation_state=state/"automation-state";automation_state.mkdir()
            context={"document_path":document["path"],"document_id":document["document_id"],"state_dir":str(automation_state),
                     "session_id":event_id,"trial_id":event_id,"operation_id":operation}
            item={"operation_id":operation,"event_id":event_id,"status":"running"}
            report["observations"].append(item);save()
            if file_sha(template)!=template_hash:raise ValueError("immutable test template changed")
            for role in ("prepare_independent","precondition"):
                tick=time.monotonic_ns();run_command(expanded(config["automation"][role],context),30,role)
                item[role+"_ns"]=time.monotonic_ns()-tick
            # Independent preparation contains unobserved actions. Start a
            # fresh service/session rather than treating resets as history.
            device=state/"device.conf"
            device.write_text(f"app_id=wps\nmodels_root={models}\nmodel_profile={PROFILE}\nsession_id={event_id}\nroot_pid={config['root_pid']}\ncovered_pids={','.join(map(str,config['pids']))}\noutput_jsonl={state/'native.jsonl'}\nbaseline_count=2\nbaseline_ms=5000\naction_ms=15000\npost_ms=5000\nmax_sample_ms=1000\nfocus_poll_ms=100\n",encoding="utf-8")
            stream=NativeStream([native,"serve","--config",device])
            # Opaque IDs only: operation truth never enters Native stdin/config.
            stream.process.stdin.write((event_id+"\t"+event_id+"\n").encode());stream.process.stdin.flush()
            deadline=time.monotonic()+90;acted=False;event=None;health=None
            while health is None:
                record=stream.next(deadline)
                if record.get("schema")=="harmony.ops.stage.v1":item.setdefault("stage_events",[]).append(record)
                if record.get("schema")=="harmony.ops.stage.v1" and record.get("phase")=="action":
                    if acted:raise ValueError("duplicate Native action stage")
                    item.update(action_stage(config,context,record));acted=True
                elif record.get("schema")=="harmony.ops.recognized.v1":
                    if (not acted or event is not None or record.get("status")!="success" or record.get("event_id")!=event_id
                            or record.get("model_profile")!=PROFILE or record.get("source")!="controlled_live_window"
                            or record.get("is_mock") is True):raise ValueError("unexpected, synthetic or unverified Native result")
                    event=record
                elif record.get("schema")=="harmony.ops.health.v1":
                    if event is None or record.get("last_code")!="ok" or not record.get("last_success_timing_ns"):
                        raise ValueError("successful output/history timing was not confirmed")
                    health=record
            if file_sha(template)!=template_hash:raise ValueError("automation modified the bound original template")
            truth={"event_id":event_id,"operation_id":operation,"operation_label":catalog.get(operation).action_spec["legacy_label"],
                   "verification_status":"passed","truth_source":"external_automation_semantic_verifier","app_version":config["app_version"],
                   "session_id":event_id,"sequence_kind":"independent_operation"}
            with (root/"native-recognition.jsonl").open("a",encoding="utf-8") as handle:handle.write(json.dumps(event,ensure_ascii=False)+"\n")
            with (root/"truth.jsonl").open("a",encoding="utf-8") as handle:handle.write(json.dumps(truth,ensure_ascii=False)+"\n")
            with (root/"health.jsonl").open("a",encoding="utf-8") as handle:handle.write(json.dumps({"event_id":event_id,**health},ensure_ascii=False)+"\n")
            item.update(status="success",predicted_label=event["operation_label"],correct=event["operation_label"]==truth["operation_label"],
                        native_timing_ns=event["timing_ns"],phases=event["phases"],post_output_timing_ns=health["last_success_timing_ns"],
                        model_load_ns=health["model_load_ns"],wrapper_total_ns=time.monotonic_ns()-started)
            report["completed_valid_windows"]+=1;save()
            stream.close(normal=True);stream=None
        report.update(status="passed",accuracy=sum(o["correct"] for o in report["observations"])/len(operations),
                      accuracy_scope="this finite controlled check only; not a final generalization estimate")
    except (Exception,KeyboardInterrupt) as error:
        report.update(status="interrupted" if isinstance(error,KeyboardInterrupt) else "failed",error_type=type(error).__name__,
                      reason=str(error),accuracy=None)
        if stream is not None and stream.error:report["native_error"]=stream.error
        if report["observations"] and report["observations"][-1]["status"]=="running":report["observations"][-1]["status"]="failed"
    finally:
        if stream is not None:stream.close()
        save()
    lines=["# WPS Native 桌面有限验证","",f"状态：{report['status']}；有效窗口：{report['completed_valid_windows']}/{len(operations)}；正式批采始终关闭。",
           "","| 操作 | 状态 | 预测 | 正确 | Native 触发至历史更新/s | 特征/ms | 分类/ms | 输出/ms | 历史/ms |","|---|---|---|---|---:|---:|---:|---:|---:|"]
    for o in report["observations"]:
        if o["status"]!="success":lines.append(f"| {o['operation_id']} | {o['status']} | 未形成有效结果 | — | — | — | — | — | — |");continue
        t=o["native_timing_ns"];p=o["post_output_timing_ns"]
        lines.append(f"| {o['operation_id']} | success | {o['predicted_label']} | {o['correct']} | {p['trigger_to_history_updated']/1e9:.4f} | {t['feature']/1e6:.4f} | {t['inference']/1e6:.4f} | {p['output']/1e6:.4f} | {p['history']/1e6:.4f} |")
    lines += ["","完整模型加载、四阶段/PID 清位与读取、准备/动作/语义检查耗时见 report.json。动作与验证嵌在 15 秒内，不可重复相加。真值与 Native 识别结果分文件；失败/超时不计有效配额，不用预测是否正确筛选样本。", "",
              "回传 report.json、REPORT.md、truth.jsonl、health.jsonl 和 native-recognition.jsonl；先检查自己的本地语义证据中是否有文档正文，正文不必回传。"]
    if report.get("reason"):lines += ["", "失败原因："+report["reason"]]
    (root/"REPORT.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    report["feedback_zip"]=str(root/"feedback.zip");save()
    create_feedback(root)
    return report


def file_sha_data(config):
    import hashlib
    return hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-config",type=Path,required=True)
    parser.add_argument("--native",type=Path,required=True);parser.add_argument("--models",type=Path,required=True)
    parser.add_argument("--output-root",type=Path,required=True);parser.add_argument("--document-id")
    parser.add_argument("--operations",default="quick",help="quick, all, or comma-separated canonical operation IDs")
    parser.add_argument("--root-pid",type=int);parser.add_argument("--covered-pids",help="explicit comma-separated PID list; never auto-add children")
    args=parser.parse_args();config=json.loads(args.batch_config.read_text())
    if args.root_pid is not None:config["root_pid"]=args.root_pid
    if args.covered_pids:config["pids"]=[int(value) for value in args.covered_pids.split(",")]
    operations=QUICK if args.operations=="quick" else [o.operation_id for o in WpsAdapter().catalog.operations] if args.operations=="all" else args.operations.split(",")
    result=check(config,args.native,args.models,args.output_root,operations,args.document_id)
    print(json.dumps({"status":result["status"],"report":str(Path(result["output"])/"REPORT.md"),"feedback_zip":result["feedback_zip"],"valid_windows":result["completed_valid_windows"],"accuracy":result.get("accuracy")}))
    return 0 if result["status"]=="passed" else 2


if __name__=="__main__":raise SystemExit(main())
