# M1＋M2 软件验收记录

日期：2026-10-16。最终验证代码提交 `6a908eae55b49bd783de6849586041abf5b022a5`，包含公开桥接头文件与两库默认 PIC（可链接共享服务）。原始完整计算验收提交为 `0b0a365`。后续文档记录不改变 Native 算法，实际包内 source_commit 与各文件 SHA 见 BUILD_INFO.json / SHA256SUMS。

| 环境 | 通过项 |
| --- | --- |
| Mac arm64 / AppleClang 17 / CMake 4.3 | C/C++ 构建、4 项 CTest、模型 SHA/schema 审计、5 项模型负例；54 项 Python 中 45 通过、9 项 Linux 环境测试明确跳过 |
| server-lbz x86_64 / Linux 6.8 / glibc 2.35 / G++ 11.4 / CMake 3.22 / Python 3.10 | 统一启动器构建/诊断/三应用自检/4 项 CTest；32 项 Linux/历史/预测/映射/启动器相关 Python 测试全部通过，无系统依赖安装 |
| GitHub Ubuntu / Python 3.10 与 3.12 | 两组各 54 项 Python、4 项 CTest、模型负例与原生自检全部通过 |
| GitHub macOS、Windows MSVC | 各自 C/C++ 构建、4 项 CTest、原生三应用自检全部通过 |
| OpenHarmony SDK 6.1.0.105 / API 23 / Clang 15.0.4 | 官方 ohos.toolchain.cmake、arm64-v8a Release 交叉构建两个静态库及 opsctl，通过；未在设备运行 |
| 离线包 | Mac 在新包目录从同包源码重建、无 Python 子进程自检及三应用回放通过；SHA 清单全通过；最终包复核记录见交付目录 |

[GitHub 最终四组任务](https://github.com/yyf123123/harmony-pc-operation-recognition/actions/runs/38870358235) 均 success。Mac/Linux 额外编译并链接 shared host bridge，核实 PIC 链接能力。Linux 原生运行报告保留在 server-lbz 本项目新目录 `harmony_ops_runs/native-20261016-6a908ea/outputs/headless/20261016T075629Z-1d867ccd072d/report.json`。没有连接其他用户桌面、运行 hdc 或采集真实 WPS 文档。

## M1 数学对照

合成原始 smaps 夹具明确标记 synthetic，覆盖 .so、heap、stack、匿名、重复文件键、连续斜线及带空格/deleted 路径。三应用分别检查原始语义映射、每个 baseline、median、action/post、excess、2048 维向量和模型输出。逐阶段页数及向量最大误差 0，标签全部一致；分数最大误差 `5.56e-17`，小于 `1e-9`。非 4KiB 页大小与 schema 差异另有 C/C++ 用例。

此前外部真实 WPS 12 个向量的公式一致性证据保留在 docs/linux/validation.md，没有将真实数据放入包。两类对照均不构成 Linux/WPS 准确率。

## 故障与生命周期

- 严格 parser：空、截断、非法/重复/缺 Referenced、溢出、重叠 VMA、非法页大小；默认 proc 真实自有进程检查 start ticks、清位与读取。
- 注入故障：权限拒绝、部分 PID、焦点/覆盖/重启、零向量、阶段超时、模型错误、输出回调与真实文件系统失败、并发窗口及停止。不得发布有效识别或更新历史；错误记录无操作标签。
- Linux 合成焦点 helper（无 DISPLAY、非真实 GUI）：自有目标真实 procfs；action 中新 child、缺 PID、目标退出、SIGINT 与 helper 超时均失败；serve EOF/空闲取消/连续失败行为通过。
- 成功历史：重复标签保留、事件 ID 去重、上限、主进程实例切换、重启清空；C 宿主回放不调用焦点/发布且不写历史。

默认采集至少约 30s＋开销；测试短 profile 仅用于软件故障验证。焦点轮询不证明两次检查间始终聚焦；阻塞的闭源回调不能被强制安全中断。

## 产物 ABI 与剩余验收

OpenHarmony opsctl：aarch64 ELF、解释器 `/lib/ld-musl-aarch64.so.1`，动态需要 `libc++_shared.so` 与 `libc.so`。Linux opsctl 为 x86_64 glibc；Mac 为 arm64 Mach-O。完整依赖写入包中 BUILD_INFO.json；包不带 SDK/系统运行库，现场核实或使用对应工具链重建。

M1/M2 软件交付通过。M3 内网宿主焦点调用、权限、服务注册及设备 ABI 待验证；M4 慧京桌面三项原语待校准、其他 15 类待实现和完整 smoke；M5 真实 Markov/页映射制品、独立会话指标及预测提前量待交付。正式批采与页面 Apply 未开放。

## 2026-10-28：简化宿主接入包

上文为 10 月 16 日历史验收，本节记录本轮实际执行。用户只能携入代码、不能外传内网材料；新增单一宿主链接目标及三调用入口，自动离线 golden/模型校验、内部工作线程、唯一输出目录和中文本地诊断，简化说明见 [SIMPLE_DEPLOY.md](SIMPLE_DEPLOY.md)。不自动注册服务或申请权限，不声称已在设备部署。

- **源码**：`9bc6d744c1522ca33b7a2d12304cbb585535e439`，打包时工作区干净。
- **Mac**：Release 构建及全部 5 项 CTest 通过；共享宿主实际链接新的三个静态目标通过。相关 Native/profile/桌面包装器 11 项 Python 回归和原三模型 SHA 审计通过。
- **Linux server-lbz**：仅新目录原生构建、共享宿主链接和全部 5 项 CTest 通过；新包装器测试使用合成回调，没有真实 WPS/UI 或准确率评估。未安装依赖、提权或触碰其他用户进程。
- **故障/生命周期**：验证未知应用、无效输出路径、黄金夹具损坏、重复启动、真实焦点回调失败、阶段文件写入失败、重叠触发 busy、取消并 join、正常完整 30 秒合成窗口、输出后健康计时；失败不能形成有效预测。合成 provider 只在测试目标，生产包默认焦点未接入即失败。
- **同包一键入口**：从包副本执行 ready.sh，Mac 原生构建、自检和三应用合成回放通过；仅修改 operator/platform_bridge.cpp 仍允许构建，核心文件改动被 SHA 门禁在构建前拒绝。
- **同包真实宿主接线**：独立外层 CMake 工程只用 add_subdirectory(operator) 和链接 harmony_operation_host，公共 C 头三调用运行通过；自动 golden 成功，默认真实焦点未接入被明确拒绝并生成中文本地报告，退出回收 worker。未修改外层 BUILD_TESTING 设置。
- **OpenHarmony**：现有 SDK 6.1.0.105/API 23 ARM64 交叉构建通过，包含新宿主库；包内 ready.sh 使用同包源码另建交叉目录通过，明确跳过在 Mac 执行目标二进制。实际闭源 GN 工程、宿主服务权限/注册及设备运行仍未验证。

交付 ZIP：`/Users/cvue/Documents/huawei_projects/deliverables/harmony-ops-innernet-simple-20261028.zip`（973,935 字节，约 1 MB）；SHA-256 为 `fe93e3ae222b9411f57df740ed0e18c7d26eac61fa8e438de7e835863a727dec`，旁置 `.zip.sha256`。161 个静态文件逐一 SHA 核验，通过文件/路径/可执行位审计；不含 SDK、凭据、真实文档/数据、Python 环境或 Linux 候选参数。包只选三个原冻结模型，原参数没有修改。

私有验证日志和 DELIVERY.json 在本机 `operation_reports/20261028-harmony-simple/`；Linux 构建日志已留本机副本。内网不需要把源码、接口材料或日志回传，现场直接看 status.txt 和 status-history.jsonl。触发被接收不等于已识别，start 离线就绪不等于真机权限通过，当前仍为受控完整窗口协议。
