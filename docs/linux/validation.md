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
