#!/bin/sh
# Read-only display for the existing minimal memmgrserver package.
# The optional expected label stays in this observer, never in recognition.
set -eu
if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
  printf '%s\n' '用法：sh show_wps_result.sh /设备输出/onsite-目录 [WRITE_TEXT|SAVE_DOCUMENT|FORMAT_BOLD] [等待秒数，默认120]'
  exit 2
fi
RUN=$1
EXPECTED=${2:-}
LIMIT=${3:-120}
case "$EXPECTED" in ''|WRITE_TEXT|SAVE_DOCUMENT|FORMAT_BOLD) ;; *) printf '%s\n' '本次检查只接受输入、保存、加粗的人工标签。';exit 2;; esac
case "$LIMIT" in ''|*[!0-9]*) printf '%s\n' '等待秒数必须是整数。';exit 2;; esac
if [ "$LIMIT" -lt 1 ] || [ "$LIMIT" -gt 600 ];then printf '%s\n' '等待秒数范围为1到600。';exit 2;fi
for TOOL in tail sed wc sleep cat;do
  command -v "$TOOL" >/dev/null 2>&1 || { printf '%s\n' '缺基础命令；直接查看 status.txt、stage.jsonl 和 results.jsonl，不安装依赖。';exit 2; }
done
if [ ! -d "$RUN" ];then printf '%s\n' '输出目录不存在；请使用 start 新建的 onsite-* 目录。';exit 2;fi
field() { printf '%s\n' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"; }
number() { printf '%s\n' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p"; }
rows() { if [ -f "$RUN/results.jsonl" ];then wc -l < "$RUN/results.jsonl";else printf '0\n';fi; }
initial_rows=$(rows)
initial_status=''
if [ -f "$RUN/status.json" ];then initial_status=$(tail -n 1 "$RUN/status.json");fi
previous_stage=''
if [ -f "$RUN/stage.jsonl" ];then previous_stage=$(tail -n 1 "$RUN/stage.jsonl");fi
elapsed=0
printf '%s\n' '等待本次新窗口。请从原服务无标签调试入口触发，action 阶段只做一个约定动作。'
while [ "$elapsed" -lt "$LIMIT" ];do
  if [ -f "$RUN/stage.jsonl" ];then
    current_stage=$(tail -n 1 "$RUN/stage.jsonl")
    if [ "$current_stage" != "$previous_stage" ];then
      previous_stage=$current_stage
      case "$(field "$current_stage" phase)" in
        baseline) printf '%s\n' '阶段：建立基线，请先不要操作。';;
        action) printf '%s\n' '阶段：action，现在执行约定的一个动作。';;
        post) printf '%s\n' '阶段：操作后观察，保持应用聚焦。';;
      esac
    fi
  fi
  status=''
  if [ -f "$RUN/status.json" ];then status=$(tail -n 1 "$RUN/status.json");fi
  state=$(field "$status" state)
  if [ "$status" != "$initial_status" ];then
    case "$state" in
      window_failed|startup_failed|stopped)
        printf '%s\n' '本次没有通过完整窗口检查，请查看本机 status.txt；不计为有效识别。'
        if [ -f "$RUN/status.txt" ];then cat "$RUN/status.txt";fi
        exit 2;;
    esac
  fi
  current_rows=$(rows)
  completed=$(number "$status" completed_windows)
  if [ "$current_rows" -gt "$initial_rows" ] && [ "$state" = window_success ] && [ "${completed:-0}" -eq "$current_rows" ];then
    if [ "$current_rows" -ne "$((initial_rows+1))" ];then
      printf '%s\n' '监视期间出现多个新结果，无法唯一对应本次人工动作；请按事件ID逐条核对。';exit 2
    fi
    event=$(tail -n 1 "$RUN/results.jsonl")
    label=$(field "$event" operation_label)
    if [ "$(field "$event" status)" != success ] || [ "$(field "$event" app_id)" != wps ] || [ -z "$label" ];then
      printf '%s\n' '新记录格式或应用不匹配，请检查原始结果。';exit 2
    fi
    printf '新完成事件：%s\n预测标签：%s\n' "$(field "$event" event_id)" "$label"
    if [ -n "$EXPECTED" ];then
      printf '人工约定标签：%s\n' "$EXPECTED"
      if [ "$label" = "$EXPECTED" ];then printf '%s\n' '本次标签命中：是';else printf '%s\n' '本次标签命中：否（保留为错误样本）';fi
    fi
    ns=$(number "$event" trigger_to_result_ready)
    if [ -n "$ns" ];then
      if command -v awk >/dev/null 2>&1;then awk -v n="$ns" 'BEGIN{printf "采样开始→结果就绪：%.3f 秒\n",n/1000000000}'
      else printf '采样开始→结果就绪：%s ns（除以1e9为秒）\n' "$ns";fi
    fi
    printf '%s\n' '这是一条受控窗口结果，不代表18类总体准确率或自由操作自动识别。'
    exit 0
  fi
  sleep 1
  elapsed=$((elapsed+1))
done
printf '%s\n' '等待超时：没有发现监视开始后新完成的有效窗口；不使用旧结果，不计为识别成功。'
exit 2
