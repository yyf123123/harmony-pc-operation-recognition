# Linux 批采执行与数据验收

更新：2026-10-17。当前推进 **Linux M4 工程线**，共用 Native 核心和内网移植包保持不变。软件已提供受门禁控制的批采编排、续采及入库；慧京桌面真实 18 类执行、语义成功检查和数据尚未验收。此文不表示已经采到 1500 个真实样本。

## 外部自动化接口

复用慧京已有脚本，不复制旧工程中宽泛杀进程、删除或未验证动作。配置将六种角色映射为 argv 数组；不经过 shell，也不保存命令 stdout/stderr 的文档内容。

| 角色 | 必须完成的事 |
| --- | --- |
| prepare_independent | 为单次独立操作创建/打开测试副本并恢复前置状态，只在采样前运行 |
| begin_session | 为连续流程准备一次初始文档；一整个 20 操作会话只调用一次 |
| precondition | 检查本操作前置状态；连续会话中只能检查，不能恢复/重置文档冒充连续流程 |
| action | 执行指定 canonical operation_id 对应的真实动作 |
| verify | 检查实际语义结果；只有快捷键发送成功或无条件 return 0 不合格 |
| focus | 输出实际焦点 JSON：整数 pid、字符串 window_id/source；PID 必须在授权集合内 |

参数可使用 `{operation_id}`、`{document_path}`、`{document_id}`、`{state_dir}`、`{session_id}`、`{trial_id}`。每个 argv 元素独立替换，空格不会被解释成 shell。`state_dir` 是每次尝试的可变工作区，可以存放实际工作副本；不要改写作为输入绑定的模板文件。该工作区不作为不可变采样证据，正式 receipt 绑定原始采样、metadata、特征、Native 结果；脚本、模板和参数另外绑定。额外的 UI 成功依据由现有语义验证器负责，软件门禁不会自动认证闭源 UI 检查是否可靠。

## 配置与单条启动

先生成不会误执行的未完成配置：

```sh
python -m scripts.linux_batch template --output /private/config/wps-batch.json
```

填入：实际 WPS/设备版本，主进程 `root_pid` 与全部授权子孙 PID；5 份不同模板的绝对路径并确认 `test_copy=true`；6 个不同、合法且恰好 20 步的办公流程；六种角色的真实 argv 和相关脚本文件 `automation_files`。ID 必须来自 catalog。模板默认不填流程、不确认副本和 PID，因此不能直接执行。无需重写已有 18 类方法，只需提供这些调用映射；适配代码问题交给 Codex。

仅检查规格（不执行命令、不访问 `/proc`）：

```sh
python -m scripts.linux_batch plan --config /private/config/wps-batch.json
```

得到 966 个执行单元/1536 个窗口，仅说明结构合法：36 预检、900 独立操作、30×20 连续操作。不会标记桌面通过。5 个模板家族按 3/1/1 分 train/validation/test；同家族的副本与变体不得跨组。流程必须由已有自动化确认前置状态合法，不能为凑 20 步任意排列。

统一启动器配置：

```json
{
  "schema_version": "linux.launcher.v1",
  "mode": "batch",
  "output_root": "/private/wps-runs",
  "batch_config": "/private/config/wps-batch.json",
  "build_jobs": 2
}
```

```sh
python -m scripts.linux_run --config /private/config/wps-run.json
```

该命令自动创建唯一目录、构建 Native、诊断目标 PID、运行原生自检/CTest，再开始预检。前 36 个窗口全部通过后，按预检体积估算剩余空间（最大预检窗口占用×剩余窗口×2），不足时暂停。门禁通过后自动进入正式采集，不需要再聊天确认。外部验证器成功与数据完整性是门禁；**不要求预测等于真值**，避免按识别正确与否筛选数据。

同一批次有独占锁；不同批次对相同 PID/start_ticks 也有用户态锁。锁仅约束本批采工具，其他清位工具/Native 服务仍须按环境手册约定唯一 clear_refs 协调者。边界处核对可见子孙进程集合，新增/退出/复用 PID 都不能自动加入清位；当前批次暂停，重新核实配置后开新批次。焦点仍是窗口边界检查，不宣称连续聚焦或自主分段。

## 失败、恢复与配额

- 预检任一失败即暂停，不进入正式采集。正式尝试默认最多 3 次；每次使用新目录和新采样 ID，保留全部失败。
- 独立操作失败重新 prepare；连续会话中任一步失败，整个前缀只作为诊断记录，重新 begin_session，从第 0 步采满 20 个才提交。失败前缀不占 600 个配额，不与新后缀拼接。
- 每个单元通过后写入 fsync journal、样本/原始文件 SHA receipt 和进度 report。报告按单元提交计数，活动步骤单独显示，不能把只完成 5 步的会话计作 20。
- Ctrl+C 保留现场。恢复必须使用运行目录里的**冻结配置**（包含实际 build_dir）：

```sh
python -m scripts.linux_batch run --config /private/RUN/batch/config.json \
  --output /private/RUN/batch --resume
```

恢复会复核配置、脚本、模板、Native 程序、采集代码、Python/时钟域、进程启动身份与所有已提交证据。已完成单元不重复执行；中断单元重新准备，连续会话重新开新身份。配置/输入/进程变化或证据被改时拒绝恢复，旧记录保留，修复后使用新批次；不重置计数或覆盖记录绕过门禁。单次到达尝试上限也需要修复后新开批次。

## 自动入库与离线复核

统一入口成功后自动导出。独立执行/续采完成后，也可在 Mac 或 Linux 仅做验收：

```sh
python -m scripts.linux_batch export --batch /private/RUN/batch --output /private/NEW-dataset
```

它重验全部 receipt、固定标签/样本 ID、非零有限 L2 向量、Native 预测与识别事件对应关系、完整配额、单一 profile/版本、家族隔离和序列顺序。只消费完整批次，不覆盖输出。生成：

```text
dataset/
  independent/{train,validation,test}/  # 各 540/180/180
  continuous/{train,validation,test}/   # 各 360/120/120
    dataset_manifest.csv / labels.csv / vma_vectors_l2.csv / predictions.csv
  sequences/<会话单元>/truth.json       # 独立真值序列
  sequences/<会话单元>/recognition.jsonl # Native 预测序列，供既有历史/Markov 回放
  quality.json                         # 类分布、离线固定模型分数/混淆矩阵
```

Macro-F1 明确以冻结 18 类为分母，未观测类记 0；不等于默认忽略缺失类的平均。只有真实采集才计分；合成测试没有准确率。数据准备仍调用既有采集器，Native `operation_feature` 构造实际向量并逐样本与 Python 在 `1e-9` 内核对，`operation_predict` 校验既有模型并预测；没有新分类公式、重训或平滑。

这些均是固定窗口、自动化受控数据与离线指标，不代表自由操作实时准确率或预测提前量。页级采集未接时保持 unknown。源码/规则进入私有 Git，真实文档、工作区和采样输出保持在约定私有数据位置，不上传仓库。现有 M1/M2 内网包仍供 C++ 接线，本批采工具不要求改动宿主 ABI。
