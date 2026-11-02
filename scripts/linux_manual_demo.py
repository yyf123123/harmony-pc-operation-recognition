"""Prompted manual WPS capture; no automation, labels or shortened windows in Native.

Run in the desktop user's terminal beside a disposable Writer document. Labels
are requested only after sampling has stopped and are operator reports, never
semantic verification. Each round is independent because preparation is unseen.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

from collector.linux.procfs import descendant_pids, identity
from scripts.linux_desktop_check import NativeStream, PROFILE

NAMES = dict(zip(("NEW_DOCUMENT", "WRITE_TEXT", "SAVE_DOCUMENT", "CLOSE_DOCUMENT", "SELECT_ALL",
    "COPY_SELECTION", "PASTE_SELECTION", "CUT_SELECTION", "UNDO_EDIT", "REDO_EDIT", "FIND_TEXT",
    "REPLACE_TEXT", "INSERT_PAGE_BREAK", "INSERT_TABLE", "FORMAT_BOLD", "FORMAT_ITALIC",
    "FORMAT_UNDERLINE", "ALIGN_CENTER"),
    ("新建", "输入", "保存", "关闭", "全选", "复制", "粘贴", "剪切", "撤销", "重做", "查找",
     "替换", "分页", "插表", "加粗", "斜体", "下划线", "居中")))
PROFILES = (PROFILE, "linux-wps-writer-server-20261101-v2")
PHASES = (("baseline", 0), ("baseline", 1), ("action", 0), ("post", 0))


def ui(*args):
    proc = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=3)
    value = proc.stdout.strip()
    if proc.returncode or not value.isdecimal():
        raise RuntimeError("无法读取真实 X11 焦点；请使用桌面普通用户、X11/Xwayland 和 xdotool")
    return value


def select_target(wps_executable):
    """Select only the explicitly foregrounded WPS, never search by process name."""
    window = ui("getactivewindow"); pid = int(ui("getwindowpid", window))
    proc = Path("/proc") / str(pid)
    if proc.stat().st_uid != os.getuid() or (proc/"exe").resolve(strict=True) != wps_executable.resolve(strict=True):
        raise RuntimeError("当前焦点不是本账号指定的 WPS 主进程；请点击测试文档正文后重试")
    pids = sorted(descendant_pids(pid))
    identities = [identity(p) for p in pids]
    for item in identities:
        process = Path("/proc")/str(item.pid)
        if process.stat().st_uid != os.getuid(): raise RuntimeError("覆盖集合包含其他账号进程")
        with (process/"smaps").open() as handle:
            if not handle.read(1): raise RuntimeError("smaps 为空")
        # Opening checks access; no write/clear happens before Native owns timing.
        with (process/"clear_refs").open("w"): pass
    if (ui("getactivewindow") != window or descendant_pids(pid) != set(pids) or
            any(identity(item.pid) != item for item in identities)):
        raise RuntimeError("选择期间焦点或进程覆盖改变，请重试")
    return {"window": window, "pid": pid, "processes": [vars(v) for v in identities]}


def consume_window(stream, token, trial, notify):
    """Require all phases, one matching result and post-output health confirmation."""
    event = health = None; phases = []
    until = time.monotonic()+75
    while health is None:
        record = stream.next(until)
        schema = record.get("schema")
        if schema == "harmony.ops.stage.v1":
            phase = (record.get("phase"), record.get("index"))
            if len(phases) >= len(PHASES) or phase != PHASES[len(phases)]:
                raise RuntimeError("采样阶段顺序异常")
            phases.append(phase); trial.setdefault("stages", []).append(record)
            notify(record)
        elif schema == "harmony.ops.recognized.v1":
            if (phases != list(PHASES) or event is not None or record.get("event_id") != token or
                    record.get("status") != "success" or record.get("source") != "controlled_live_window" or
                    record.get("operation_label") not in NAMES):
                raise RuntimeError("Native 结果身份或完成状态异常")
            event = record
        elif schema == "harmony.ops.health.v1":
            if (event is None or record.get("last_code") != "ok" or record.get("completed_windows") != 1 or
                    not record.get("last_success_timing_ns")):
                raise RuntimeError("Native 输出和历史更新未确认成功")
            health = record
        else:
            raise RuntimeError("收到非预期 Native 事件")
    return event, health


def annotate(trial, answer):
    """Self-report is kept separate from capture validity and automatic evidence."""
    trial["truth_source"] = "operator_self_report_after_capture"
    trial["semantic_verified"] = False
    labels = list(NAMES)
    if answer.isdecimal() and 1 <= int(answer) <= len(labels):
        trial["reported_label"] = labels[int(answer)-1]
        trial["reported_protocol"] = "single_operation"
    else:
        trial["reported_label"] = None
        trial["reported_protocol"] = {"m": "multiple_operations", "n": "no_operation",
            "f": "operation_failed", "o": "outside_catalog"}.get(answer, "unknown")
    eligible = trial.get("capture_valid", False) and trial["reported_label"] is not None
    trial["self_reported_correct"] = trial["predicted_label"] == trial["reported_label"] if eligible else None


def save_report(root, report):
    trials = report["trials"]
    scored = [t for t in trials if t.get("self_reported_correct") is not None]
    report.update(total_attempts=len(trials), valid_windows=sum(t.get("capture_valid", False) for t in trials),
                  self_reported_scored=len(scored), self_reported_hits=sum(t["self_reported_correct"] for t in scored),
                  self_reported_agreement=sum(t["self_reported_correct"] for t in scored)/len(scored) if scored else None)
    temporary=root/"report.tmp"; temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n");temporary.replace(root/"report.json")
    with (root/"comparison.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer=csv.writer(handle);writer.writerow(["轮次", "采集有效", "自报操作", "识别操作", "自报情况", "是否一致", "触发至历史更新秒"])
        for i, t in enumerate(trials, 1):
            ns=t.get("post_output_timing_ns", {}).get("trigger_to_history_updated")
            writer.writerow([i, t.get("capture_valid", False), NAMES.get(t.get("reported_label"), "未知"),
                NAMES.get(t.get("predicted_label"), "无有效输出"), t.get("reported_protocol", "未填写"),
                t.get("self_reported_correct"), ns/1e9 if ns is not None else ""])


def feedback(root):
    path=root/"feedback.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("report.json", "comparison.csv", "truth.jsonl", "recognition.jsonl", "health.jsonl"):
            p=root/name
            if p.is_file() and not p.is_symlink(): archive.write(p, name)
    return path


def append(root, name, record):
    with (root/name).open("a") as handle: handle.write(json.dumps(record, ensure_ascii=False)+"\n")


def notify(record):
    phase, index = record["phase"], record["index"]
    if phase == "baseline": message=f"静置采样 {index+1}/2（5秒）：不要操作 WPS。"
    elif phase == "action": message="\a现在操作：只执行一次操作，15秒窗口内完成；完成后停手！"
    else: message="\a停止操作：后置采样5秒，请保持 WPS 焦点。"
    print(message, flush=True)


def run(args):
    root=args.output_root.expanduser().resolve()/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")+uuid.uuid4().hex[:8])
    root.mkdir(parents=True, mode=0o700)
    report={"schema":"linux.wps.manual-demo.v1", "status":"running", "is_mock":False,
        "model_profile":args.model_profile, "app_version_operator_report":args.app_version,
        "native_sha256":hashlib.sha256(args.native.read_bytes()).hexdigest(),
        "capture_protocol":"5+5+15+5", "autonomous_segmentation":False,
        "truth_protocol":"optional operator report only after sampling; no automatic semantic verification",
        "history_scope":"each round is independent; unobserved preparation is not continuous user history",
        "formal_collection_enabled":False, "trials":[]}
    save_report(root, report)
    print("只使用测试文档。把终端与 WPS 并排，关闭其他引用位采集器。\n"
          "每轮先准备文档；按回车后有5秒切回 WPS。听到提示再操作。\n"
          "本程序不输入、不保存、不修改文档，也不验证操作是否成功。Ctrl+C可取消。\n"
          f"日志目录：{root}", flush=True)
    try:
        for index in range(args.rounds):
            command=input(f"\n第{index+1}/{args.rounds}轮：准备好后回车，或输入q退出：").strip().lower()
            if command=="q": break
            token=uuid.uuid4().hex
            trial={"event_id":token, "status":"selecting", "capture_valid":False, "semantic_verified":False}
            report["trials"].append(trial); save_report(root, report)
            stream=None
            try:
                for remaining in range(5, 0, -1):
                    print(f"{remaining}秒后选择目标：请点击 WPS 测试文档正文。", flush=True);time.sleep(1)
                target=select_target(args.wps_executable);trial["processes"]=target["processes"]
                config=root/(token+".conf")
                config.write_text(f"app_id=wps\nmodels_root={args.models}\nmodel_profile={args.model_profile}\n"
                    f"session_id={token}\nroot_pid={target['pid']}\ncovered_pids={','.join(str(p['pid']) for p in target['processes'])}\n"
                    f"output_jsonl={root/(token+'.jsonl')}\n")
                stream=NativeStream([args.native, "serve", "--config", config])
                stream.process.stdin.write((token+"\t"+token+"\n").encode());stream.process.stdin.flush()
                event, health=consume_window(stream, token, trial, notify)
                stream.close(normal=True)
                append(root,"recognition.jsonl",event);append(root,"health.jsonl",health)
                trial.update(status="captured_unverified", capture_valid=True, predicted_label=event["operation_label"],
                    timing_ns=event["timing_ns"], post_output_timing_ns=health["last_success_timing_ns"])
                print(f"\a采样结束，可以回到终端。识别结果：{NAMES[event['operation_label']]}（{event['operation_label']}）", flush=True)
            except KeyboardInterrupt:
                trial.update(status="cancelled", capture_valid=False);raise
            except Exception as error:
                trial.update(status="failed", capture_valid=False, reason=str(error))
                if stream and stream.error:trial["native_error"]=stream.error
                print("本轮失败，没有有效操作输出："+str(error), flush=True)
            finally:
                if stream is not None:stream.close()
                save_report(root, report)
            print("采样已停止。选填刚才真正做的操作（这是自报，不是程序核验）：")
            print("  ".join(f"{i}.{name}" for i, name in enumerate(NAMES.values(), 1)))
            answer=input("填编号；m=多操作，n=没操作，f=动作失败，o=其他，回车=不确定：").strip().lower()
            annotate(trial, answer);append(root,"truth.jsonl", {k:trial[k] for k in
                ("event_id","truth_source","semantic_verified","reported_label","reported_protocol")})
            save_report(root,report)
        report["status"]="completed"
    except (KeyboardInterrupt, EOFError):
        report["status"]="interrupted"
    finally:
        save_report(root,report)
        print(f"\n测试结束。回传这个文件即可：{feedback(root)}", flush=True)
    return 0 if report["status"]=="completed" and report["valid_windows"] else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native",type=Path,required=True);parser.add_argument("--models",type=Path,required=True)
    parser.add_argument("--output-root",type=Path,required=True);parser.add_argument("--rounds",type=int,default=3)
    parser.add_argument("--model-profile",choices=PROFILES,default=PROFILE)
    parser.add_argument("--app-version",default="not_reported")
    parser.add_argument("--wps-executable",type=Path,default=Path("/opt/kingsoft/wps-office/office6/wps"))
    args=parser.parse_args()
    if sys.platform!="linux" or not os.environ.get("DISPLAY") or not shutil.which("xdotool"):
        parser.error("请在 Linux 桌面普通用户终端运行，需要 DISPLAY 与 xdotool（包内也可提供真实 X11 查询工具）")
    if not 1 <= args.rounds <= 18:parser.error("每次只允许1至18轮有限检查")
    args.native=args.native.resolve(strict=True);args.models=args.models.resolve(strict=True)
    return run(args)


if __name__=="__main__":raise SystemExit(main())
