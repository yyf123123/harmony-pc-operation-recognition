# Linux WPS 版本

本分支基于 main `269915cc3da022cf1d2b620c5e68069e8a45f4c6`。复用现有 C 采集器的 WPS VMA 分段、语义键与实际页数，复用 Python 冻结特征公式和 C++17 类中心推理。原 Windows/OpenHarmony 入口、三应用模型及其 SHA 不变。

当前优先使用共用 Native 核心：`opsctl doctor/selftest/replay/run/serve` 完成诊断、原始回放与受控窗口；serve 或宿主持久对象实际维护识别历史。静态库与 C ABI 供既有服务链接，内网包见[操作卡](../handoff/OPERATOR_CARD.md)。

[统一启动器与桌面 smoke](desktop_smoke.md) 自动构建、自检和生成新目录/报告。输入、保存、加粗三项安全原语已实现，仍需慧京桌面校准与验收；其余 15 类待适配，正式批采关闭。[版本化 Markov、历史与页面候选](followup_runtime.md) 已有适配和回放，无真实制品时明确 not_ready/unknown。尚无真实 18 类、鸿蒙服务或页面收益验收。

## 构建和预检

Linux 需要 Python 3.10+、CMake 3.16+、C11/C++17 编译器；真实桌面焦点适配还需 X11/Xwayland 与 `xdotool`。采集和回放本身只用 Python 标准库，NumPy 用于已有离线评估。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cmake -S . -B build/linux -DCMAKE_BUILD_TYPE=Release
cmake --build build/linux --parallel 2
ctest --test-dir build/linux --output-on-failure
.venv/bin/python -m scripts.audit_models
```

Linux 上额外生成 `build/linux/collector/linux/linux_vma_collect`。macOS 可构建严格文本解析、Native 核心和离线入口，真实 `/proc` 明确拒绝运行。

## 外部动作计划

计划保存在 Git 外。下面仅说明格式；`/path/to/...` 必须换成目标机器已有且已校验的自动化脚本。不能用空命令或无条件成功的检查来冒充操作验证。操作 ID 必须出自 `apps/wps/catalog.json`；模型的旧标签通过 `legacy_label` 显式对应。

```json
{
  "schema_version": "linux.wps.action-plan.v1",
  "operation_id": "wps.writer.write_text",
  "precondition": ["/path/to/automation", "check-blank-document"],
  "action": ["/path/to/automation", "write-controlled-text"],
  "verify": ["/path/to/automation", "check-written-text"],
  "focus": [".venv/bin/python", "-m", "collector.linux.focus"],
  "baseline_s": 5,
  "action_s": 15,
  "post_s": 5,
  "command_timeout_s": 15
}
```

四个命令均为 argv 数组，通过退出码表示成败，执行不经过 shell。`focus` 必须输出 JSON：`pid`（整数）、`window_id`（字符串）、`source`（证据来源）。其他命令的 stdout/stderr 不写入采集记录，以免持久化文档内容。调用者负责提供不会泄露凭据的本地脚本。

先仅检查计划结构，不访问进程或启动 UI：

```bash
.venv/bin/python -m scripts.linux check-plan --plan /external/operation-plan.json
```

`plan_valid` 仅表示格式与操作 ID 合法，不表示自动化已验证。

## 单操作 smoke

先在桌面用户会话启动 WPS，并明确其实际 PID 集合。下例 PID 为示意值，须替换；`--pid` 可重复。不会通过进程名模糊匹配，也不会自动杀进程、启动应用、提权或增补子进程。

```bash
.venv/bin/python -m scripts.linux capture \
  --collector build/linux/collector/linux/linux_vma_collect \
  --pid 12345 --pid 12346 \
  --plan /external/operation-plan.json \
  --session-id session_001 --trial-id trial_001 \
  --app-version YOUR_WPS_VERSION --device-id anonymous_linux_01 \
  --output /external/new-linux-smoke
```

协议为前置检查、两次 baseline（各自 clear_refs）、ACTION（clear_refs → 动作 → 验证 → 等待 → 采样）、POST_ACTION（重新 clear_refs → 等待 → 采样），随后质量检查与保存。ACTION 与采样之间不会再次清引用。动作和验证超出所配置窗口时失败，不悄悄延长窗口。

每个窗口前后读取独立焦点证据，PID 必须在选择的集合内。新建/关闭文档可以改变集合内的焦点窗口；集合外焦点或进程重启导致失败。边界检查不能证明整个窗口持续聚焦。新增子进程不会自动纳入，必须在下一次完整样本前更新选择范围。

输出包括 `metadata/session.json`、四个原始 TSV/窗口 JSON、`derived/sample.json`、`validation/status.json` 和兼容的 `operation_window_sequences.jsonl`。失败保留已有证据和状态，不生成成功样本。输出根目录必须是新的目录。当前入口每次只做一个 smoke；不能把它称为完成 18 类正式 trial 或已有批采门禁。

## 回放、特征与现有 Native 推理

```bash
.venv/bin/python -m scripts.linux replay \
  --sample /external/new-linux-smoke/derived/sample.json \
  --output /external/new-linux-csv
.venv/bin/python -m scripts.linux predict \
  --native build/linux/native/operation_predict --models models \
  --vectors /external/new-linux-csv/vma_vectors_l2.csv \
  --output /external/new-predictions.csv \
  --sample /external/new-linux-smoke/derived/sample.json \
  --event-output /external/new-recognition-event.json
```

回放从窗口页数重建 baseline 中位数、正向 excess 与 ACTION/POST_ACTION 最大值，输出 2048 维向量及元数据，保留 mock/source/capture profile。Native CLI 独立校验模型和 schema，不使用真值进行推理；可选事件从 Native 预测标签映射到规范操作 ID。

`collector.linux.history.OperationHistory` 接收该事件，显式 reset 后维护会话/实例历史，去重事件 ID、保留真实重复操作、拒绝乱序及真值来源，并输出 `prediction_request()`。`event_from_native_runtime` 将 Native 预测旧标签映射到规范 ID；Markov 模型/词表与有效期页映射见 followup_runtime.md，真实制品和独立会话联调仍待交付。

## 团队任务

| 负责人 | 任务 |
| --- | --- |
| 用户 / 师兄 | [VMA 移植、历史与预测、整体集成](tasks/vma_owner.md) |
| 璟雯 | [操作—文件页映射](tasks/jingwen.md) |
| 欣彤 | [其他反推方法与横向评估](tasks/xintong.md) |
| 慧京 | [数据集 smoke、正式采集与交付](tasks/huijing.md) |

[接口约定](integration_contract.md)、[数据集要求](dataset_requirements.md)、[环境约束](environment.md)、[验证记录](validation.md)。任务已形成仓库文档，尚未发送人员通知或登记认领。

Linux 与鸿蒙的文件路径、应用实现、页大小和窗口条件可能不同；已有模型在 Linux 上的有效性需要独立验证。此次复用算法和模型仅证明计算与接口可接续，不证明 Linux/WPS 实时准确率或内存管理收益。
