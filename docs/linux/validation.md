# Linux 分支验证记录

日期：2026-10-16。初始 main：`269915cc3da022cf1d2b620c5e68069e8a45f4c6`，Mac 已通过 Git 独立核验。目标仓库已确认为 private。

## 本机已完成

环境为 macOS arm64、AppleClang 17、CMake、Python 3.10 虚拟环境、NumPy 2.2.6。没有 Linux 桌面或 WPS 真机采集。

- 三应用模型元数据与 SHA-256 审计通过；未修改模型文件或 Native 特征/分类公式。
- Native CMake Release 构建与 `operation_pipeline_test` 通过。
- 修复 Python 3.10 缺少 `enum.StrEnum` 的基线兼容问题，保留状态值与字符串语义。
- Python 共 31 项测试覆盖 Linux 时序、失败留存、焦点 PID、实际页大小、PID 启动身份、回放重建、mock 元数据、Native 特征一致性，以及历史去重/会话隔离/真值排除；在 macOS 上 30 项通过，1 项 Linux 真实进程测试按平台跳过。
- Native 负例检查通过：signed v3、错误维度、零向量、非有限数值、错误中心映射均被拒绝。

## 外部真实 WPS 历史数据的本机对照

只读使用已有 2026-07-23 WPS smoke 的外部向量与首条窗口序列，未复制或上传数据，也未重新采集。该数据来自历史鸿蒙采集，不是 Linux 数据。

| 检查 | 本次结果 |
| --- | --- |
| 前 12 个真实向量 Python/C++ 预测标签 | 12/12 一致 |
| 最大余弦分数差 | `2.55351295663786e-15` |
| 一条窗口序列的 Python/C++ 特征最大差 | `0.0` |
| 相对历史已导出向量的最大差 | `4.926892227530288e-13` |
| 对照容差 | `1e-9` |

这仅验证公式与已有模型调用的一致性，不是识别正确率测量。测试夹具不用于科学结果。

## Linux 验证入口与剩余项

工作流 `.github/workflows/linux.yml` 在 Linux 分支上构建 C/C++，运行 Python 3.10/3.12、模型审计、CTest、Native 负例，以及真实 `/proc` 子进程清引用/采样测试。CI 仅使用受控自建测试进程，不运行 WPS、不采集用户桌面、不上传测试数据制品。实际执行结果须以 Actions 记录为证据，不能仅凭工作流存在声称通过。

代码提交 `98fe15215e211aa5304c15e790bfd487fa435953` 的 [Linux CI 记录](https://github.com/yyf123123/harmony-pc-operation-recognition/actions/runs/38844454914) 已完成并通过。Python 3.10、3.12 两组均成功编译 C/C++、通过 1 项 CTest 和全部 31 项 Python 测试；`test_real_linux_child_process_clear_and_capture` 在两组均实际运行通过。另有 5 个 Native 拒绝路径检查通过。后续文档提交不改变此次验证的源码。

尚未验证：目标 Linux 的 WPS PID/子进程归属、独立桌面焦点、18 类动作与成功判据、真实窗口质量、已有模型在 Linux 上的操作识别准确率、连续会话实时效果、下一步模型接入、页级映射与内核消费者。

## 2026-10-17：Linux 批采与入库工程

源码提交 `b28f6b01564e88edbe5d2c2e8e0e9a8fa7124276`，仅扩展外层采集编排、元数据和验收，没有改动 C/C++ 核心、三应用模型或冻结公式。当前主线为 Linux M4 工程，内网 M1/M2 包沿用已交付版本。用户已转交慧京数据集要求是用户提供的信息，不是收到桌面验收或采集数据的证据。

| 环境 | 实际验证结果 |
| --- | --- |
| Mac / Python 3.10.20 | `NATIVE_BUILD_DIR=build/native .venv/bin/python -m unittest discover -s tests -v`：70 项中 61 通过、9 项 Linux 依赖跳过 |
| GitHub Ubuntu / Python 3.10、3.12 | 两组各 70 项全部通过，另四项 CTest、模型审计、Native 自检与拒绝路径通过 |
| GitHub macOS、Windows | C/C++ 构建、四项 CTest 和 `opsctl selftest` 通过 |
| server-lbz / Python 3.10.12、G++ 11.4、CMake 3.22.1 | 新目录中的统一 selftest 启动器构建、doctor、自检、四项 CTest 通过；相关 48 项 Python 全部通过，无跳过 |
| server-lbz / 真实 `/proc` | 仅对自有测试子进程运行：BatchProcfs 清引用/采样有效，新增未授权子进程后立即拒绝覆盖；既有取消、退出、helper 超时、serve 失败路径测试通过 |

完整 [Actions 证据](https://github.com/yyf123123/harmony-pc-operation-recognition/actions/runs/39003633908)。Linux 两组测试包括真实自建子进程；未访问 WPS 桌面、未采集其他用户进程。服务器运行目录 `/home/lbz/harmony_ops_runs/batch-20261017-b28f6b0/`，`verification-index.json` 定位构建报告，`verification-tests.log/.json` 记录 48 项测试，`verification-batch-procfs.json` 记录进程树检查。未安装依赖、提权或修改系统环境。

相关测试命令（服务器构建目录由 `verification-index.json` 指向，设置 `NATIVE_BUILD_DIR` 和 `LINUX_VMA_COLLECTOR` 后）：

```sh
python3 -m unittest tests.test_linux_batch tests.test_linux_batch_export \
  tests.test_linux_collection tests.test_linux_followup tests.test_linux_history \
  tests.test_linux_launcher tests.test_opsctl_linux -v
```

全配额 1536 窗口、整段重试、断点恢复、坏 journal/证据拒绝、磁盘门禁、互斥及 1500 正式窗口的分组导出使用**明确标记的合成夹具**验证。真实 Native 程序参与特征/预测集成核对，观察输入仍是夹具；这些测试不产生正式数据、真实准确率或真实用户序列。

尚未验证：慧京实际 18 类动作与语义检查、真实焦点与进程覆盖、模板/流程合法性、真实 WPS 数据与模型在 Linux 的识别效果。配置结构合法或外部验证器返回成功不等于独立认证 UI；下一步接实际脚本/数据验收。内网宿主 API/ABI、权限、服务注册及设备运行仍属于 M3，未在本轮替代验证；真实下一步模型、页映射与提前量仍待 M5。

## 2026-10-21：首批真实 WPS 小试 intake

已收到并离线核验 190 个选定样本＋36 预检，详见[接收与复核报告](intake_20261021.md)。904 段原始 TSV 到冻结特征链通过，Python/Native 向量差 0；原始模型预测重放一致，独立 5/90、连续 0/100，不能直接迁移为可用 Linux 模型。未重训或覆盖原模型，生产方 72.2% 的 Linux 诊断基线尚未独立复现。真实数据留在仓库外，不在 CI 中运行或上传。

本机 `NATIVE_BUILD_DIR=build/native .venv/bin/python -m unittest tests.test_linux_intake tests.test_linux_collection tests.test_linux_batch_export tests.test_linux_history -v`：21 项中 20 通过、1 项依赖 Linux 的测试跳过；实际 226 样本 intake 另外通过。模型数学和 C/C++ 核心没有改动。
