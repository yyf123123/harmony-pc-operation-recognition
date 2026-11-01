# Linux WPS Native 接入与计时入口

2026-10-28：原有 `frozen` 默认、三应用模型文件及 v1 C ABI struct 布局不变；新 Linux 候选必须显式选择 `linux-wps-writer-pilot-20261023-v1`。编译注册绑定该候选现有 model.json/centers.csv SHA，拒绝未知 profile、错误应用、缺文件、篡改文件及错误维度，不回退原模型。

模型制品仍是 54 个独立样本拟合的小试候选。原 metadata 的 `native_runtime_ready:false` 和“尚未接入”的说明记录的是参数导出时状态；本轮新增显式加载能力，但未因此晋升默认模型或完成真实桌面部署验收。参数文件没有重写。

## 构建与离线预测

```sh
cmake -S . -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
build/native/native/opsctl doctor --models models --app wps \
  --model-profile linux-wps-writer-pilot-20261023-v1
build/native/native/operation_predict --models models --app wps \
  --schema wps.vma.feature.v1 \
  --model-profile linux-wps-writer-pilot-20261023-v1 \
  --vectors /private/windows.csv --output /private/NEW-predictions.csv
```

`opsctl selftest` 继续验证原三应用的冻结 golden，Linux 候选通过单独测试和真实 compact 回放核对，不能拿原模型 golden 当成新模型预期标签。

## 受控桌面运行

慧京无法提供远程桌面时，使用[可转发的桌面验证包](desktop_native_check.md)。包装器复用她已有配置，在 action 阶段按剩余窗口预算运行动作/验证；不需要她手工对时触发，也不要求重写 18 类动作。

在已有 device.conf 中增加唯一一行：

```ini
model_profile=linux-wps-writer-pilot-20261023-v1
```

其余字段沿用环境手册：`app_id=wps`、`models_root`、新 `session_id`、实际主进程 `root_pid`、完整 `covered_pids`、新 `output_jsonl`。不要复制示意 PID 或对其他用户的桌面运行；焦点默认使用桌面账户已有 X11/xdotool，权限失败即停。

```sh
build/native/native/opsctl serve --config /private/device.conf
```

stdin 每行 `window_id<TAB>event_id` 触发一次完整窗口；操作人员在 action 阶段执行实际操作，核心不接收操作名。`STOP` 或 EOF 停止。真实 WPS 的 focus/进程覆盖、训练采集中的 RPC 观察负载差异和跨重启效果仍需桌面验收，不由自有进程测试替代。

C++ `RuntimeConfig.model_profile` 和 `NativePipeline(models, profile)` 可选择同一注册模型；纯 C 宿主新增 `hop_create_profile(config, callbacks, profile, out, error)`，旧 `hop_create` 仍加载 frozen。C++ RAII `Service` 的可选第三参数同义，不改变既有 C struct 的布局和所有权。

## 计时含义

- 结果 `model_profile` 记录实际参数选择；`timing_ns` 包含首次焦点/身份、特征、分类、最终检查和触发到结果就绪的时长。
- 四个观察阶段保留每 PID 的清引用与读取时间；固定窗口仍至少 30 秒，不能把分类微秒级耗时称为实时识别时延。
- 显式 Linux profile 的窗口从清引用前起算，清引用包含在 5/5/15/5 秒内，与 `collector/linux/session.py` 训练采集一致；旧 frozen 的清引用后时钟保持原行为，结果 `capture_profile.window_clock` 明确记录。
- 输出、历史更新结束后才知道它们的耗时，故结果输出时 `output_and_history` 为 null；随后 `health.last_success_timing_ns` 给出 output、history、trigger_to_history_updated。该轮失败则不出现该轮有效成功计时。
- `health.model_load_ns` 是当前服务启动的一次 SHA 校验/模型解析时长；无清系统缓存，不声称冷存储读性能。

## 分阶段离线报告

`scripts.linux_wps_report prepare` 读取已核验的 190 样本，生成不含标签的 compact TSV 回放输入、文件哈希清单及原采集机阶段统计。`benchmark` 是标准库 worker，不需要服务器 NumPy；执行全量预热后 5 次 Native 重放。`report` 在有 NumPy 的主机上核对 190 样本的原始到向量与 Python/Native 标签/分数，并生成 Markdown/JSON 报告。

```sh
.venv/bin/python -m scripts.linux_wps_report prepare \
  --intake /private/verified-intake --output /private/NEW-report \
  --model models/linux/wps_writer_pilot_v1
python3 -m scripts.linux_wps_report benchmark \
  --payload /private/NEW-report/payload \
  --native build/native/native/operation_benchmark --models models \
  --output /private/NEW-benchmark --repeats 5
.venv/bin/python -m scripts.linux_wps_report report \
  --run /private/NEW-report --benchmark /private/NEW-benchmark \
  --intake /private/verified-intake --model models/linux/wps_writer_pilot_v1
```

Native benchmark 区分文件读取、compact 解析、语义聚合、baseline、中位数后的 excess、2048 维构造/归一化、分类及输出；输出是 ostream flush 到页缓存，未 fsync，诊断 CSV 不计入。报告保留 P50/P95 和计时环境。原采集记录中无法分开的准备/旧特征/导出合并为余量，不补造单项耗时；不同主机的统计不相加称为实测端到端。
