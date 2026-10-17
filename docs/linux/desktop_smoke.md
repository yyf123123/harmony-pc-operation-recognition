# Linux 统一启动与桌面 smoke

状态：本页描述的单操作 smoke 和三项内置原语不开放正式批采，慧京桌面 AT-SPI 和实际成功判据仍待验证。需要复用已有 18 类自动化时，使用新增[batch 模式](batch_runner.md)，其正式门禁只在完整 36 窗口预检和空间检查通过后开放；这不表示内置 18 类 UI 方法均已完成。

## 单条启动命令

```bash
python -m scripts.linux_run --config /private/config/linux-run.json
```

`linux-run.json` 示例：

```json
{
  "schema_version": "linux.launcher.v1",
  "mode": "selftest",
  "output_root": "~/huawei-runs",
  "build_jobs": 2
}
```

启动器自动创建 UTC 时间加随机 ID 的新目录，完成构建、`opsctl doctor`、Native 自检和 CTest，最终始终输出 `report.json`。不安装依赖、不提权、不启动/杀死应用、不清缓存。构建依赖缺失和任何阶段失败均报告失败；不会把离线通过写作桌面验收。需要查看详细工具诊断时，在报告指向的 build 目录直接重运行对应工具；入口不持久化命令 stdout/stderr，避免保存文档文本。

`mode=controlled` 时增加 `device_config`（Native `key=value` 设备配置的绝对路径），可选 `controlled_timeout_s`。启动器在自检后调用同一个 `opsctl run`；窗口 ID 和事件 ID 自动生成且不包含操作标签。程序打印 action 阶段提示后，操作者才执行测试动作。此模式只是一次受控窗口，未实现自由操作自主分段。

## 先验证三项安全动作

```bash
python -m scripts.linux_desktop readiness
```

该命令列出当前 catalog 的全部 18 类。三项 `smoke_primitive_ready` 表示代码和语义检查已准备，不代表目标桌面通过；其余 15 项仍为 `desktop_adaptation_pending`。

慧京应在自己的 X11/Xwayland 桌面、一次性测试文档副本上校准。必须具备 `xdotool`、当前用户 `/proc` 权限，以及能读取 WPS 文档文本/选区/属性的 AT-SPI Python 绑定。原生 Wayland 和未暴露可编辑文档 AT-SPI 节点时明确失败；不退化成“快捷键发送成功”。

桌面配置示例（所有 PID、启动身份和窗口 ID 均须替换成实际值）：

```json
{
  "schema_version": "linux.wps.desktop-smoke.v1",
  "test_copy": true,
  "pid": 1234, "start_ticks": 123456, "window_id": "1234567",
  "operation_id": "wps.writer.write_text",
  "expected_before": "", "payload": "WPS Linux Smoke",
  "expected_after": "WPS Linux Smoke"
}
```

- 输入：预先打开空白 Writer 文档，焦点在编辑区，无选区、光标位于末尾。原语输入受控单行文本，AT-SPI 必须读到精确的结果。
- 保存：`operation_id=wps.writer.save_document`，增加 `document_copy` 指向已经存在的 `.docx` 测试副本。预先在 WPS 做受控未保存编辑，`expected_before` 与 `expected_after` 均为预期文档全文。发送 Ctrl+S 后，要求文件指纹变化且 DOCX 保存内容等于预期；没有 Save As 或自动覆盖其他路径。
- 加粗：`operation_id=wps.writer.format_bold`，`expected_before` 与 `expected_after` 相同。预先选中受控的非粗体文本，Ctrl+B 后要求原选区保持且每个属性段的文字权重为粗体。未提供文字权重属性时失败。

同一窗口/进程启动身份在每步前后验证；不会激活其他窗口。测试副本标识由配置明确声明，操作人员须核实路径确为副本。没有广泛杀进程、删除文件、剪贴板覆盖或全屏截图。保存语义检查读取测试 DOCX 内容，但不输出或上传文本。

将启动配置改为：

```json
{
  "schema_version": "linux.launcher.v1", "mode": "desktop-smoke",
  "output_root": "~/huawei-runs", "pids": [1234],
  "desktop_config": "/private/config/wps-desktop.json",
  "session_id": "实际会话", "trial_id": "实际trial",
  "app_version": "实际WPS版本", "device_id": "匿名桌面ID"
}
```

启动器自动生成 precondition/action/verify/focus argv 计划，完成一次原有固定窗口采集、回放导出和 Native 预测。真值仍留在采集文件，Native 分类只读取特征向量。固定协议至少约 30 秒加采样开销，不能写成快速自主识别。验证动作在 action 区间内，超时或焦点丢失使采集失败。

## 错误处理

| 报告/错误 | 处理 |
| --- | --- |
| build tools unavailable / configure / build failed | 在既有允许的环境准备 CMake、C++17 编译器；不要用 sudo 运行整个入口 |
| doctor / native-selftest failed | 按 Native 错误码核对模型、进程身份和权限；不继续采集 |
| selected WPS test window is not focused | 返回正确测试窗口，更新显式 PID/窗口/启动身份，再开始新运行 |
| AT-SPI document text / weight unavailable | 在慧京桌面确认 WPS 可访问性接口；需要补适配时保留 pending，不填恒成功脚本 |
| document text differs / semantic success failed | 校准测试文档、选区和焦点；失败输出单独保留 |
| operation has no Linux semantic verifier | 此类仍待实现，不能用占位动作加入正式数据 |

完整 18 类的前置状态、动作、语义成功检查和全类预检全部通过之后，[batch 入口](batch_runner.md) 自动开放正式配额。输入/保存/加粗的单次成功不能代替该验收，也不能证明模型在 Linux 上的准确率。
