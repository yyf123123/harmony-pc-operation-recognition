# 错误与处理

CLI 失败返回 2，并输出 JSON 错误；C ABI 返回明确状态并可读取健康状态。采集中途失败使整个窗口失效，下一次触发从完整 baseline 开始。

| 错误 | 操作人员处理 |
| --- | --- |
| invalid_argument / parse_error | 检查配置、正数 PID、完整 smaps、页大小及回放 manifest；不要填入操作标签作为特征 |
| unsupported | 此主机没有 Linux/OpenHarmony procfs；使用离线回放，或在目标系统重新构建 |
| permission_denied / collection_failed | 核实实际服务用户与 smaps/clear_refs 访问策略；不要自动 sudo 或把失败转换为零 |
| missing_referenced / empty_snapshot | 核实目标接口和进程存活；标准字段不可用时只替换底层访问并重新验证 |
| process_changed | 主进程/子进程已退出或启动身份变化；更新真实 PID 覆盖并重新触发完整窗口 |
| focus_changed | 焦点、窗口、覆盖或证据来源改变；核实焦点适配，恢复目标后重新采集 |
| sampling_timeout | 采样、焦点或平台回调超时；查权限/负载，单独验证新 profile，不能复用失效窗口 |
| model_error | 核实 models 目录、应用/schema、SHA-256 与中心标签；禁止绕过校验 |
| output_failed / io_error | 检查唯一输出目录、空间和通道；修复后新触发，不能宣称已经发布成功 |
| busy / not_started | 同一运行器仅一个窗口；先启动，按宿主线程生命周期调用 |
| duplicate_event | 事件已发布；为新的真实操作使用新 ID，重复动作标签本身允许保留 |
| cancelled | 正常停止或操作者取消；此窗口不能当作成功样本 |
| no_memory / internal_error | 保留诊断状态，检查资源或平台实现；不要继续生成有效预测 |
| next-operation not_ready / mapping unknown | 需要真实模型/词表或有效页映射；保持未知，不以合成预测或默认冷页替代 |

输出发布回调返回“已接管”时，宿主负责保证记录已被接收。宿主若发生部分写入后异常，失败诊断仍可能留下截断 JSON 行；消费者只接受完整且 status=success 的有效结构。运行日志的失败记录不是识别事件。
