# 历史回放、Markov 制品与文件页候选

状态：适配器与合成夹具单元测试已实现；真实预测模型制品、璟雯的真实映射和独立会话指标尚未验收。这里的 Python 入口用于 Linux 数据准备与回放，内网目标设备的 Native 推理不依赖 Python。

## 识别事件接入

运行：

```bash
python -m scripts.linux_followup replay-events \
  --events /private/run/recognition.jsonl --output /private/run/followup.jsonl
```

同一文件只放一个会话、一个应用实例；进程重启、未知区间或识别方法切换后开始新文件并重置历史。支持 Native `harmony.ops.recognized.v1` 成功受控窗口事件，以及 `linux.operation-event.v1` 识别事件。WPS 旧模型标签通过当前 catalog 转为规范操作 ID；不读取自动化请求中的操作名称。其他方法须按共同事件格式输出，再用 `--allow-method 方法名` 显式启用。真值或未来预测不能进入历史。

相同事件 ID 去重；同标签不同事件保留。历史默认最多 64 项，去重最多 1024 项。重启不恢复。Native 事件使用自己的 `end_ns`，不会用回放时刻替换实际识别时刻。没有预测模型时输出 `status=not_ready, reason=model_unavailable`；没有页映射时输出 `status=unknown, reason=mapping_unavailable`。

## 既有 Markov 计数模型的 JSON 导出

`collector.linux.next_operation.OpMarkovModel` 保留旧实现的全局计数、1–4 阶上下文计数、最长上下文回退和相同计数时按数值 ID 排序的公式。没有改变分类器，也没有安装默认下一步模型。可在原有训练/导出流程中使用同名 `fit` / `predict_topk`，然后导出：

```python
from collector.linux.next_operation import OpMarkovModel, MarkovArtifact

# vocab: {app_id: {规范操作ID: 数值ID}}，包含原模型实际使用的映射。
# samples: 原训练流程的 app/history_ops/label_next_op；不要在此拼造训练数据。
model = OpMarkovModel(order=2, op_vocab=vocab)
model.fit(samples)
payload = model.export(model_id="团队实际模型版本", vocab_version="团队实际词表版本",
                       source_kind="real", source_sessions=train_session_ids)
MarkovArtifact.from_dict(payload).save("/private/model/next-operation.json")
```

训练和评估按完整会话分组；`source_sessions` 保存训练来源，不能把同一会话的相邻窗口随机拆开。导出不是对这些来源真实性的自动认证。`<PAD>` / `<UNK>` 是特殊 ID，不参与输出；识别历史出现未知规范操作时返回 `vocabulary_mismatch`，避免静默删掉未知操作后拼接错误上下文。

制品 schema 为 `linux.next-operation-markov.v1`，包含 `model_id`、`vocab_version`、`order`、`op_vocab`、`training_provenance`、`global_counts` 和 `transitions`。计数为正整数；应用内词表 ID 唯一；上下文长度不能超过阶数。保存使用新文件，不覆盖旧制品。加载拒绝不支持的 schema、未知 ID、重复上下文/输出、空计数和错误来源元数据。输出记录制品规范 JSON 的 SHA-256。

```bash
python -m scripts.linux_followup validate-model --model /private/model/next-operation.json
python -m scripts.linux_followup replay-events \
  --events /private/run/recognition.jsonl --model /private/model/next-operation.json \
  --output /private/run/predictions.jsonl
```

候选分数语义为 `markov_empirical_frequency`，是所选计数分布的经验频率，未校准成实际准确率。`synthetic_fixture` 模型和 mock 事件默认拒绝使用；只有测试时显式 `--allow-fixture`。

## 璟雯的操作—文件页映射接入

映射 schema 为 `linux.operation-file-pages.v1`。下面是字段示意，不是可用的真实制品：

```json
{
  "schema_version": "linux.operation-file-pages.v1",
  "mapping_id": "实际映射ID", "mapping_version": "实际版本",
  "app_id": "wps", "app_version": "实际WPS版本",
  "label_schema_version": "cross-app.operation-catalog.v1",
  "source_kind": "real", "source_sessions": ["实际采集会话"],
  "created_unix_ns": 100, "expires_unix_ns": 1000,
  "candidate_ttl_ns": 500,
  "entries": [{
    "operation_id": "wps.writer.save_document",
    "file_identity": {"sha256": "64位小写内容SHA256", "size_bytes": 8192},
    "file_page_index": 1, "page_size_bytes": 4096,
    "support_count": 5, "access_frequency": 0.8,
    "evidence_kind": "observed_file_page_access"
  }]
}
```

时间字段使用实际整数纳秒。跨会话文件身份是内容 SHA-256 加字节长度；虚拟地址、PFN、只有路径以及 VMA 汇总均不接受为精确文件页证据。页索引从 0 开始，必须落在该文件范围内，页大小必须等于当前目标记录的实际系统页大小。

运行时调用方提供当前已核实的文件身份。可对明确选定的测试文件调用 `collector.linux.page_mapping.inspect_file(Path(...))`；函数校验哈希前后的设备、inode、大小和 mtime 没有变化，不上传文件内容。CLI `--file-identities` 接收这些身份的 JSON 列表。它不会把映射里声称的身份直接当作当前观测。

```bash
python -m scripts.linux_followup replay-events \
  --events /private/run/recognition.jsonl --model /private/model/next-operation.json \
  --mapping /private/maps/pages.json --file-identities /private/run/verified-identities.json \
  --app-version ACTUAL_VERSION --page-size 4096 --clock-domain HOST_BOOT_ID \
  --output /private/run/page-candidates.jsonl
```

版本、文件身份、页大小或有效期不符，以及映射未命中，均保持 `unknown`。匹配页只输出 `hot_candidate`；其他页不自动标冷。候选有效期取映射剩余有效期与配置 TTL 的较小值，并携带主机启动时钟域。多操作支持同一页时保留每项依据，不把相关频率相加成虚假概率。此输出未接内核，也不表示回收/预取已生效。

回放输出不是提前预测时延验收。下一步仍需在独立真实会话中核对候选时间先于下一操作，并分别统计操作预测、页命中/覆盖和有效期指标。
