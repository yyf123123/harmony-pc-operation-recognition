# 鸿蒙现场：编进原服务，展示 WPS 识别结果

适用已带入内网的 `harmony-ops-memmgrserver-minimal-20261029.zip`（363,823 字节）。不要求传出内网源码、接口文档或日志。当前是约 30 秒及采样开销的受控识别；启动、编译通过和识别正确分别检查。

## 先解决编译：只改原服务的三处接入

**不单独运行 `g++ 某个.cpp`，不创建另一套服务。** 包里的 C/C++ 文件需要一起链接进原 memmgrserver，使用原工程的产品、工具链与增量构建命令。

1. 确认解压后 `server/memmgrserver/operation_recognition/BUILD.gn`、`src/event/operation_recognition_event.cpp` 和 `include/event/operation_recognition_event.h` 已按相对路径放入原仓库。已有同名文件先比较合并。
2. 在原来真正编译 memmgrserver 的 **服务 target** 的 `deps` 中增加下行，不是在 BUILD.gn 最外层随便增加，也不再次手工列出包内的七个 C/C++ 文件：

```gn
deps += [ "operation_recognition:memory_ops_event" ]
```

这一相对标签要求宿主 BUILD.gn 位于 `server/memmgrserver/`；其他位置按原工程的路径调整。若原工程整体不是 GN，请现场同事把包内 BUILD.gn 的源码闭包接入原构建方式，不能照抄不匹配的目标。

3. 在已有服务对象中持有识别句柄，并在启动、现有调试命令处理、退出三个不同位置放入下面调用。两个路径都是**设备上的路径**，不是内网编译电脑的源码路径。

```cpp
#include "event/operation_recognition_event.h"

// 作为服务对象成员，一直保留到退出；不能放在启动函数的短期局部对象里。
hop_embedded* recognizer_ = nullptr;

// 原服务启动完成处：资源根目录下直接有 models/wps/ 等目录。
// 下列两个占位字符串必须换成现场批准的设备路径。
const char* device_resource_root = "/现场设备资源根目录";
const char* device_output_root = "/原服务已有可写输出目录";
hop_error error{};
hop_status rc = hop_embedded_start(device_resource_root, device_output_root,
                                   "wps", &recognizer_, &error);
// 检查 rc。非 HOP_OK 时用原服务日志记录 error.message，并停止本次识别接入。

// 原服务已有的无标签调试命令/按钮/IPC 处理处，每次只发一个请求。
rc = hop_embedded_trigger(recognizer_, &error);
// HOP_OK 仅表示收到请求；HOP_BUSY 表示上一窗口还没结束。

// 原服务退出控制线程，先禁止新调试请求，再停止。
rc = hop_embedded_stop(&recognizer_, &error);
// 不在 focus/stage/publish 平台回调中调用 stop；宿主避免 trigger 与 stop 并发。
```

请现场同事只帮你定位：**实际服务 target、原有增量构建命令、现有无标签调试入口**。构建命令因产品/输出目录不同，不能在外网编造一个通用 `hb build` 或 `ninja` 命令。沿用该服务此前成功的生成/编译/安装/启动流程，确保新产物实际替换进测试设备。

## 再接唯一平台接口

打开 `server/memmgrserver/operation_recognition/platform_bridge.cpp`。默认 `ReadOperationFocus` 返回 -1，因此**即使编译成功也不会产出真实识别**。

请现场同事把原系统真实前台/焦点查询接到此函数，成功时返回 0，并填写：

| 字段 | 必须来自真实系统状态 |
| --- | --- |
| app_id | 当前前台应用对应 `wps`，切到别的应用不能仍填 wps |
| window_id | 当前真实焦点窗口标识，不能为空 |
| root_pid | WPS 当前主进程 PID，不能把应用/包 ID 当成 PID |
| covered_pids、pid_count | 本次授权采样的主/子进程集合，含主 PID，数量不超过 HOP_MAX_PIDS |
| evidence_source | 实际使用的焦点接口来源名称 |

字符串必须完整放入头文件数组并以 `\0` 结束；读取失败、未知覆盖或应用不匹配返回失败。只把默认返回值改为 0、固定 PID 或“WPS 已启动”都不算接通。其余采集、模型、线程、特征和历史逻辑已经提供。

`ConfigureOperationCallbacks` 中通常保留 identity/clear_refs/read_smaps/publish 为 NULL：分别使用现有严格 `/proc` 和默认 results.jsonl。只有标准采集接口不兼容或确需原服务输出通道时才接额外回调。按原服务批准权限解决访问问题，不默认 sudo 或放宽系统策略。

模型按原服务资源安装方式放到 `device_resource_root/models/{wps,bilibili,huawei_browser}/`；输出父目录 `device_output_root` 对服务身份可写。资源包不要混入 Linux 候选模型，也不需要 fixtures 或启动回放。

## 展示效果：先做输入、保存、加粗

每次 start 会创建新的 `onsite-*`。先读此目录 `status.txt`：`model_ready_live_pending` 仅代表模型/线程就绪。保持 WPS 聚焦，先恢复下面的前置状态，再发一次无标签 trigger；**看到 stage.jsonl 的 phase=action 才做一个动作**，然后等待窗口结束。准备动作在 trigger 之前完成，窗口中不要切应用或连做多个操作。

| 人工约定动作 | 触发前准备 | action 阶段操作 | 正确标签 |
| --- | --- | --- | --- |
| 输入 | 空白测试副本，正文光标就位 | 输入一小段文字，并确认可见 | WRITE_TEXT |
| 保存 | 已有可写副本，已有未保存修改，避免首次另存为弹窗 | 点击保存并确认修改已保存 | SAVE_DOCUMENT |
| 加粗 | 选中一小段未加粗文字 | 点击加粗并确认样式变化 | FORMAT_BOLD |

可先直接查看 `stage.jsonl`、`status.txt`、`results.jsonl`。为了更容易展示，还提供只读脚本 `show_wps_result.sh`，不改服务、不提权、不传标签给识别核心：

```sh
# 在设备原有终端启动监视，然后从原服务调试入口 trigger，再按阶段做动作。
sh show_wps_result.sh /实际输出路径/onsite-实际目录 SAVE_DOCUMENT
```

脚本等待启动监视后新增的一次完整窗口，显示“现在做动作”、预测标签、人工约定标签是否命中，以及采样开始到结果就绪的秒数。它不会拿旧结果充作新结果，也不会因为预测错了丢掉本次记录。设备缺少脚本所需基础命令时，直接查看原生 status/stage/results 文件，不安装新依赖。

本次若修改过 publish 回调，结果归该回调接收，不能假定仍有默认 results.jsonl；只读脚本对应默认文件输出方式。

## 导师检查时看四项

先确认三项各一轮都能产生有效完整窗口（不以预测全对为门槛），再各测 5 次，共 15 个请求；准备副本和状态保持一致。按真实结果记录，失败不重命名成成功：

1. **集成**：原 memmgrserver 已运行新增模块，真实焦点/权限/采样可用；展示新构建版本和服务内结果。
2. **有效率**：有效完整窗口数 / 15。权限、焦点、进程变化等失败计入请求分母。
3. **识别**：正确标签数 / 有效窗口数，同时给出正确结果数 / 15；只描述这三项小样本，不叫 18 类总体准确率。
4. **耗时**：各次 `timing_ns.trigger_to_result_ready / 1e9`；这是采样开始到结果就绪，不含人工准备，也不等于动作结束后的响应时间。最终输出/历史完成时长在当前成功窗口的 status.json/health 中另查。

测试表和日志可全部留在内网，直接给导师现场展示。没有真机运行或只有编译通过时，明确记录为待验证，不补填效果。

## 常见阻碍直接处理

| 现象 | 处理 |
| --- | --- |
| 找不到 event/… 或 harmony_ops/… 头文件 | 检查 host 实际依赖是否连到 memory_ops_event，路径是否对应当前 BUILD.gn；头路径由模块 public_configs 提供 |
| undefined reference to hop_embedded_* | 模块未进入实际链接目标，或重复/旧库仍在；用一个模块依赖，避免同时链接旧识别库 |
| throw / exceptions disabled | 让模块与宿主批准的 C++17/异常配置保持一致；不删除错误检查或改成空成功 |
| 启动模型错误 | 检查设备资源根下 models/ 的位置和文件完整性；不改 JSON/参数或关闭 SHA 检查 |
| 平台焦点接口未接入 | ReadOperationFocus 仍为默认 -1；需现场接真实接口，重新编译并部署 |
| 采集失败 / Referenced 缺失 | 核对服务实际身份对已授权目标的 stat/smaps/clear_refs 访问和系统字段支持；必要时只替换底层回调 |
| process_changed / focus_changed | 废弃本窗口，恢复真实焦点与稳定进程覆盖后重建完整基线；不拼接前后数据 |
| window_success 但标签错误 | 这是模型识别错误，照实计入结果；不能修改真值或过滤该样本 |

本卡对应已携入的极简包，原来约 1 MB 通用包中的 operator/ 路径、启动黄金回放和自检工具不适用于这份 364 KB 包。
