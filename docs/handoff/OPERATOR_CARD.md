# 内网操作卡：只填桥接、接三处调用

1. 改 `operator/platform_bridge.cpp` 的真实焦点调用，返回应用、窗口、主进程和完整 PID 集合。默认采集、模型、线程、窗口、历史与输出已完成。
2. 现有 Native 服务包含 `harmony_ops/embedded.h`，启动调用 `hop_embedded_start`、调试触发调用 `hop_embedded_trigger`、退出调用 `hop_embedded_stop`；检查返回码。链接 `harmony_operation_host`（已包含桥接及依赖），按现有服务原方式编译/发布。模型和 fixtures 放到指定 package_dir，output_dir 选择服务可写位置。
3. 模型校验和合成回放由 start 自动执行，采样线程由库管理。需要独立构建时只跑 `sh operator/ready.sh`，它合并校验、构建与可运行主机上的自检/回放；交叉构建只编译，目标 start 才自检。

首轮触发后，看到 `stage.jsonl` 的 action 才执行动作，保持应用聚焦。只看自动新建运行目录中的 **status.txt** 和 **results.jsonl**；完整 health/耗时和旧状态另存 JSON。无需把任何内网材料传出。

启动成功表示离线就绪，触发成功表示任务已接收，真实识别以窗口结果为准。保持受控 5/5/15/5 秒协议，至少约 30 秒加开销；没有自由操作自动分段。不自动申请权限、注册服务或绕过访问控制；权限/焦点/采集错误按本机中文提示处理。

详细接线与 CMake/GN 示例见 **docs/SIMPLE_DEPLOY.md**。原静态库/C ABI、opsctl 独立入口仍保留供排错；本轮优先使用三调用入口，不要求手动串联旧六步。
