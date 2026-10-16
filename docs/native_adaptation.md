# Native 用户态系统服务适配

`native/` 可链接到现有鸿蒙 Native 用户态系统服务。原 `NativePipeline` 保持兼容；新 `ServiceRuntime` 已实现严格采集、受控窗口、原始 VMA 特征、推理、取消、错误事件、应用实例和有界历史。`opsctl` 与宿主库使用同一核心。

## 已实现的计算边界

- `CentroidModel::Load` 依应用 ID 与 schema 选择冻结的中心顺序，在运行时校验 `model.json` 和 `centers.csv` 的 SHA-256，并检查 CSV 的 2048 列、中心 ID、标签顺序和有限数值。发布前另用 `python -m scripts.audit_models` 审计元数据字段。
- `CentroidModel::Predict` 对输入重新做 L2 归一化，再与 L2 中心点积；取第一个最高余弦分数。零向量、NaN/Inf、维度错误、错误 schema 和未知应用均报错。没有拒识阈值。
- `harmony_vma_collector` 提供严格原始 smaps 解析和默认 procfs 访问，库拥有的快照用库函数释放。缺 Referenced、权限、读取、PID 复用和任一进程失败均明确失败。实际页大小来自采集环境。
- `SemanticFeatures` / `BuildWindowVector` 完成原始 VMA → 语义键 → baseline 中位数 → action/post 正向 excess → 2048 维。WPS 使用冻结 C 分类器、路径空白/斜线归一化与向上取整页数；跨应用 v2 保留其 Python 语义与字节/实际页大小，不混用。signed v3 的 8192 维不能调用这些模型。
- `NativePipeline::RunOne` 每次按适配层给出的应用身份路由一个完整窗口，缓存模型并输出一次预测。没有平滑、迟滞或未经验证的自动切换策略。

## 一个桥接文件与一份配置

`native/examples/platform_bridge.cpp` 的 `ReadOperationFocus` 是闭源焦点接口接线点。默认 -1 表示未编入提供者，0 成功，其他状态失败。填写真实应用、窗口、主进程、当前覆盖 PID 和证据来源；不能用固定值冒充实际焦点。`StartOperationRecognition` 也允许宿主传入已有的焦点、阶段和发布回调。操作人员无需重写旧的五个通用适配接口。

`opsctl` 编入相同桥接文件。Linux 未编入提供者时默认调用 `xdotool` 并核验当前进程树；鸿蒙未接入时仅受控采集明确失败，诊断、自检和回放仍可运行。包中 `operator/device.conf` 配置授权 PID、路径和窗口参数。回调必须及时返回；轮询不能证明两次采样之间焦点从未变化。

默认 `/proc` 底层已完成，现场核实权限；仅标准接口不可用时填写可选 `identity/clear_refs/read_smaps`，返回启动 ticks、完整原始文本和实际页大小。默认写 JSONL，需要宿主通道时提供 publish。服务注册、宿主 ABI、权限和真实焦点调用仍须现场确认。

## 窄 C ABI 调用顺序

公共头文件 `harmony_ops/service.h`，C++ 包装 `service.hpp`。使用 C 标量、定长结构、借用字符串与调用方缓冲区，不跨宿主边界传 STL 或异常。链接 `harmony_operation_recognition`、`harmony_vma_collector` 和匹配的 C++/线程运行库。

1. 用 `hop_default_config` / `hop_default_callbacks` 初始化，填写路径、会话及真实 focus；`hop_create`、`hop_start` 校验模型。
2. `hop_replay` 接收包内原始 manifest，在宿主内无副作用回放。NULL/0 查询长度，再分配缓冲区；不调用焦点、清位、发布或历史。
3. worker 调用 `hop_run`，仅提供不透明窗口/事件 ID；阶段回调通知 baseline/action/post，真值在外层隔离。
4. `hop_health_json` / `hop_history_json` 读状态与历史。控制线程 `hop_stop` 取消并等 worker 返回；回调内用 `hop_request_stop`。停止并 join 所有宿主调用后再 destroy。重启清空历史。

主进程 PID/start_ticks 标识实例，子进程覆盖独立记录。焦点/身份/覆盖变化、任一 PID 失败、缺字段、超时、输出失败或取消使整个窗口失效并清空未知区间历史；下一触发重建完整 baseline。重复动作保留，以事件 ID 去重。事件记录实际页大小、profile、各 PID 清位/读取时段和整体单调时钟。

默认完整窗口至少约 30 秒及采样开销，不是快速自主识别。`run` 单次退出；`serve` 或宿主持久对象才保存连续历史。入内网按“宿主内回放 → 权限与焦点 → 真实受控采集 → 操作与切换”验收。交叉编译不证明服务接入、设备兼容或实时准确率。

## 本地离线命令

```bash
cmake -S . -B build/native
cmake --build build/native
ctest --test-dir build/native --output-on-failure
./build/native/native/opsctl selftest --models models --fixtures native/fixtures/raw-window
python -m scripts.audit_models
./build/native/native/operation_predict --models models --app bilibili --schema cross_app.vma.feature.v2 --vectors /external/vma_vectors_l2.csv --output /external/new_predictions.csv
./build/native/native/operation_feature --schema wps.vma.feature.v1 --sample-id SAMPLE --input /external/aggregated_features.csv --output /external/new_vector.csv
```

跨应用 v2 的特征入口使用重复的 `--baseline FILE`、`--action FILE` 和 `--post FILE`。每个文件的列为 `namespace,feature_key,pages`，namespace 只能是 `FILE` 或 `ANON`。它们是已有窗口的语义页数，不是原始 smaps。逐样本 Python/C++ 对照命令及数值结果见 `docs/validation.md`。

双入口包、无网络脚本及 SHA/依赖说明见 [移植包说明](handoff/README.md)、[一页操作卡](handoff/OPERATOR_CARD.md)。
