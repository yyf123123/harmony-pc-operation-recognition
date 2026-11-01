# 内网只做两件事

**若你已携入 2026-10-29 的约 364 KB memmgrserver 极简包，请直接使用 [极简包接入与效果展示卡](MEMMGRSERVER_QUICKSTART.md)。** 该包使用 `server/memmgrserver/` 路径、`event/operation_recognition_event.h`，不含本页通用包的 operator/、fixtures、启动回放或独立自检入口。下文保留作旧通用包说明。

适用：把操作反推装进**已有 Native 用户态服务**。不需要把内网代码、接口文档或日志传出来；全部自检和诊断可留在内网本机。包不替你注册新的系统服务，也不修改现有权限策略。

## 1. 填一个文件

只改包中的 **`operator/platform_bridge.cpp`**，把 `ReadOperationFocus` 换成现场真实系统调用，并填入：

| 字段 | 填什么 |
|---|---|
| app_id | 当前应用对应的 `wps` / `bilibili` / `huawei_browser` |
| window_id | 真实焦点窗口的稳定标识 |
| root_pid | 该应用实际主进程 PID |
| covered_pids、pid_count | 当前需要覆盖的主进程和子进程集合，含主进程 |
| evidence_source | 真实焦点接口来源的固定名称 |

当前服务启动所选应用和实际前台应用必须一致；切到其他应用会使该窗口失效，不自动给错误应用套模型。数组/字符串遵守头文件长度限制，返回 0 才表示真实读取成功。默认返回 -1 明确表示未接入，不能改成空成功或固定 PID。

其他逻辑已经写好：模型/特征、采集与取消、线程、基线、结果文件和历史。默认直接使用 `/proc`；若现场只能走闭源采集接口，仍在**同一个文件**的 `ConfigureOperationCallbacks` 填对应 identity/clear_refs/read_smaps 回调。输出默认本地 JSONL，无需接服务消息通道；必要时也在这里设置 publish。闭源回调必须及时返回，停止不能强制中断阻塞的厂商接口。

## 2. 现有服务加三个调用，按原方式构建发布

包含 `harmony_ops/embedded.h`，服务保存一个 `hop_embedded* recognizer = nullptr` 和 `hop_error error`：

```cpp
// 启动：两个路径换成内网批准的包目录、可写输出父目录。
hop_status rc = hop_embedded_start(package_dir, output_dir, "wps", &recognizer, &error);

// 需要验证一次操作时：可从原有调试按钮/无标签命令触发，不传操作名。
rc = hop_embedded_trigger(recognizer, &error);

// 服务退出：控制线程调用；会取消采样、回收内部线程和对象。
rc = hop_embedded_stop(&recognizer, &error);
```

每处检查 `rc != HOP_OK` 时记录 `error.message`，不能忽略错误继续报成功。启动自动验证所选鸿蒙冻结模型 SHA 和合成原始窗口/特征/分数，之后自动创建工作线程。**start 成功只表示 offline_ready；trigger 成功只表示已接收任务**，识别是否成功看输出。每次仅一个窗口，重叠返回 busy，不偷偷排队。停止开始后宿主禁止新的调用；不要在平台回调里调用 stop。

构建只选现有工程用的一种：

- **CMake 服务**：`add_subdirectory(/包目录/operator ops)`，服务目标 `target_link_libraries(现有目标 PRIVATE harmony_operation_host)`。目标已带桥接和两项底层库，不要再重复加入 platform_bridge.cpp。
- **GN 服务**：将包内 `operator/BUILD.gn.example` 复制为 `BUILD.gn`，现有服务 `deps` 加 `包目录/operator:harmony_operation_host`，沿用它原来的 SDK/ABI/服务发布方法。编译配置仍需匹配现有服务；GN 接入片段未在闭源工程中验证。
- **需要先独立构建**：已有 SDK 下设置 `OPS_TOOLCHAIN=/实际路径/ohos.toolchain.cmake`、`OPS_OHOS_ARCH=arm64-v8a`，只执行 `sh operator/ready.sh`。它合并核心校验、构建及可运行主机的自检/三应用回放。交叉构建不在编译电脑上执行目标程序，目标侧自检由上面的 start 自动完成。架构不匹配时用现场工具链重建；不是必须使用 ARM64。

模型和 fixtures 随包放到启动参数 package_dir；服务用户须能读。输出父目录由现场选择可写位置；每次启动自动新建子目录，不覆盖旧报告。库链接到已有服务，不另起一套系统注册流程；宿主系统权限和打包发布仍按原服务规范处理。

桥接修改后须用以上构建入口重新编译；包内预编译库包含未适配的默认桥接，不会自动吸收文件修改。初始化 handle 为 nullptr，重复启动返回 busy，先 stop 再重建。

## 现场只看两个文件

输出目录下自动生成 `onsite-.../`：

- **`status.txt`**：中文状态、错误和处理方法；`status.json`/`status-history.jsonl` 保留完整 health 与各阶段耗时。停止后最后状态会变 stopped，先前失败仍在 history 文件中。
- **`results.jsonl`**：默认真实识别或失败记录；`stage.jsonl` 给出 baseline/action/post，action 才执行动作。若显式设置宿主 publish，结果改由该回调负责接收，不声称同时完成两个输出通道的原子提交。

首轮：服务启动后无标签触发一次，保持目标应用聚焦，action 时执行一个动作，等结果。窗口至少 30 秒加开销。阶段文件可由已有调试界面显示，或普通本机终端 `tail -f` 观察；不是自由操作自动分段。权限不足、缺 Referenced、焦点/覆盖/启动身份变化、输出失败均保留错误，不生成有效预测。

出现问题按 status.txt 本地处理即可，**无需回传内网材料**。包不安装依赖、不提权、不清系统缓存、不杀应用、不修改服务注册或访问控制。
