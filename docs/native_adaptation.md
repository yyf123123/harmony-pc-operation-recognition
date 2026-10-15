# Native 用户态系统服务适配

`native/` 是可链接到**现有鸿蒙 Native 用户态系统服务**的 C++17 静态库。它不修改内核，也不声明服务注册方式或任何尚未核实的闭源 API。`operation_predict` 和 `operation_feature` 是本机离线例子，不是设备服务。

## 已实现的计算边界

- `CentroidModel::Load` 依应用 ID 与 schema 选择冻结的中心顺序，在运行时校验 `model.json` 和 `centers.csv` 的 SHA-256，并检查 CSV 的 2048 列、中心 ID、标签顺序和有限数值。发布前另用 `python -m scripts.audit_models` 审计元数据字段。
- `CentroidModel::Predict` 对输入重新做 L2 归一化，再与 L2 中心点积；取第一个最高余弦分数。零向量、NaN/Inf、维度错误、错误 schema 和未知应用均报错。没有拒识阈值。
- `BuildCrossAppV2Vector` 接收已有语义 VMA 的 `Referenced` 页数汇总，按 baseline 中位数、action/post 较大值、正向 excess、SHA-256 槽、`log1p` 和 L2 构造 2048 维。`BuildWpsVector` 接收 WPS 已聚合的 excess pages，使用相同槽布局。两个 schema 明确区分；signed v3 的 8192 维不能调用这些模型。
- `NativePipeline::RunOne` 每次按适配层给出的应用身份路由一个完整窗口，缓存模型并输出一次预测。没有平滑、迟滞或未经验证的自动切换策略。

## 入内网后要实现的接口

复制 `native/examples/service_adapter_template.cpp` 到现有服务工程，替换每个抛错的占位实现：

1. `PermissionGate`：核对服务进程实际身份及 `/proc` 观察权限；失败立即停止。权限规则以目标系统为准。
2. `WindowScheduler`：提供真实单调时间的 baseline/action/post-action 计划和窗口 ID，核对采样间隔、进程重启、窗口完整性与失败判定。
3. `ApplicationIdentityProvider`：由独立焦点证据确定当前应用，记录证据来源。普通应用的前后台生命周期回调不能单独证明实际焦点窗口。焦点来源须在目标服务环境确认。
4. `ReferencedFeatureProvider`：接入经过授权的 VMA/Referenced 观察，按照原采集代码的语义 VMA 键和对应 schema 形成窗口页数；验证 PID 归属、clear_refs/采样时序、权限失败、零向量及原始窗口一致性。此接口尚未连接设备。
5. `ResultSink`：使用现有服务认可的输出通道记录窗口 ID、应用证据、标签、余弦分数和失败状态；不要输出文档内容或敏感设备信息。

这些接口需要真机和既有服务 API 的证据后才能实现。仓库没有系统服务注册、焦点 API、权限授予或设备输出实现。交叉编译通过仅说明标准 C++ 源码适配目标工具链，不能证明服务已接入或可实时运行。

## 本地离线命令

```bash
cmake -S . -B build/native
cmake --build build/native
python -m scripts.audit_models
./build/native/native/operation_predict --models models --app bilibili --schema cross_app.vma.feature.v2 --vectors /external/vma_vectors_l2.csv --output /external/new_predictions.csv
./build/native/native/operation_feature --schema wps.vma.feature.v1 --sample-id SAMPLE --input /external/aggregated_features.csv --output /external/new_vector.csv
```

跨应用 v2 的特征入口使用重复的 `--baseline FILE`、`--action FILE` 和 `--post FILE`。每个文件的列为 `namespace,feature_key,pages`，namespace 只能是 `FILE` 或 `ANON`。它们是已有窗口的语义页数，不是原始 smaps。逐样本 Python/C++ 对照命令及数值结果见 `docs/validation.md`。
