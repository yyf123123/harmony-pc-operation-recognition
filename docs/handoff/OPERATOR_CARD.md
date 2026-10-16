# 内网操作卡：先回放，再采集

此包供现有 Native 用户态系统服务链接，也可用 `opsctl` 单独自检。无需设备 Python 或网络。包内原始窗口是合成夹具，不能作为准确率证据。

1. **校验**：解压到新目录，执行 `sh operator/verify.sh`。核对 `BUILD_INFO.json` 中目标架构、工具链和运行库。不要将 macOS 或 Linux glibc 二进制用于鸿蒙 musl 服务。
2. **离线构建**：设备/内网构建机有 CMake、C/C++17 工具链时，执行 `sh operator/build_offline.sh`。使用现有 OpenHarmony SDK 时设置 `OPS_TOOLCHAIN=/实际SDK/native/build/cmake/ohos.toolchain.cmake` 与 `OPS_OHOS_ARCH=arm64-v8a`；SDK 不在包内。无网络下载步骤。GN 接入参考 `operator/BUILD.gn.example`，按实际服务编译配置调整。
3. **自检**：`OPSCTL="$PWD/targets/目标名称/opsctl" sh operator/selftest.sh`；自行构建可省略 OPSCTL。三个应用的模型校验、原始 VMA 到特征/分数的合成黄金对照应全部成功。
4. **回放**：`OPSCTL="$PWD/targets/目标名称/opsctl" sh operator/replay.sh /新的输出目录`。目标服务也先通过 C ABI 启动并回放/验证模型，再接真实采集。回放不修改 `/proc`，不修改历史，不代表真实识别。
5. **接线**：只修改 `operator/platform_bridge.cpp` 的真实焦点调用，离线构建脚本会将此文件同时编入 opsctl；现有服务也接入同一文件。返回焦点应用、窗口、主进程与实际覆盖 PID；核心已负责身份校验、clear_refs、完整窗口、语义特征、推理、历史和 JSONL。需要服务输出通道时填写发布回调，否则提供可写 JSONL 路径。按现有服务构建规则链接两个静态库及 C++ 运行库/线程。C ABI 的回调和字符串归属见头文件，不把 STL 传入宿主边界。
6. **真机分阶段验收**：宿主内模型加载/回放 → 实际身份及权限 → 焦点回调与受控窗口 → 单操作及切换。现场确认服务注册、构建 ABI 和权限；任一步失败均保留失败报告，不记作有效识别。

Linux 桌面可直接使用内置 X11 `xdotool` 焦点桥接：复制 `operator/device.conf` 到新的运行目录，填入授权 PID、模型及输出路径，运行 `opsctl run --config 文件 --window-id 唯一ID --event-id 唯一ID`。`action` 阶段事件才触发操作者执行操作；命令和核心不接收操作名称。请勿在其他用户的桌面操作。OpenHarmony 的 `opsctl run` 必须编入已确认的焦点桥接，默认明确报未适配；离线诊断、自检与回放可直接运行。

默认窗口为 2×5s baseline、15s action、5s post，加上采集开销至少约 30s。不是快速自主分段。停止使用宿主 stop 或 Linux SIGINT/SIGTERM；宿主回调内仅调用 request_stop。回调须及时返回，核心不能强行中断闭源阻塞接口。

连续受控运行用 `opsctl serve --config 文件 --output 新JSONL`，stdin 每行 `窗口ID<TAB>事件ID` 触发一个完整窗口，STOP 或 EOF 结束。同一进程保持有界历史；单次 run 退出后历史清空。阶段 action 提示才执行动作；焦点变化和失败会清空未知区间的历史。

只在测试文档副本和授权目标上使用 clear_refs；与其他采集器约定唯一清位协调者。不自动清缓存、加压、修改内核或执行页面预取/回收。
