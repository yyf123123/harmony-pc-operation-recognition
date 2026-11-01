# Linux WPS 桌面包交付记录

日期：2026-10-28。当前是 **Linux M4：Native 接入已完成，真实桌面待慧京执行**；内网 C++ M3 状态不变。

## 已生成的可转发文件

- 压缩包：`/Users/cvue/Documents/huawei_projects/deliverables/20261028-linux-wps-a7dd4eb/linux-wps-native-20261028-x86_64.zip`（1,789,827 字节，约 1.8 MB）。
- SHA-256：`9e4f7b5d824e6fd38fa9993df937d19745903ee59cebb530d650846b4abcf944`；同目录有 `.zip.sha256`。
- 同目录另存 `操作卡.md`、`PROGRESS_REPORT.md`、`BUILD.json` 与运行库依赖清单，便于先阅读。
- 包内源码提交：`a7dd4eb74d219ebad34e9db38f871b989e555f8b`。预编译 Native 使用已验证的 `bc04d12`，两提交间 native/collector/CMake 源码无差异；模型参数仍为 2026-10-23 原发布 SHA。

当前远端公开，因此文件以本地私下转交方式提供，未推送、未通知同事，也未更改仓库可见性。

## 真实执行的交付检查

server-lbz 新目录：`/home/lbz/harmony_ops_runs/wps-operator-20261028-a7dd4eb/`。

- 使用已构建 Native 和 C 采集器的显式路径，运行九个相关标准库测试模块：**58 项全部通过，无跳过**。包含桌面包装器六项合成契约测试，模拟输出不计真实准确率；`/proc` 仅对自有测试子进程。
- 打包器在 Linux x86_64 验证 ELF 与源码允许清单，生成 272 个静态文件的 SHA 清单，并从 `/tmp` 运行包内 selftest：原三模型合成 golden 和 Linux 候选 SHA/加载检查通过。
- 用系统 `unzip` 解压 ZIP 到另一个新目录，**直接执行** selftest 和 run_check --help 均通过，确认脚本权限、相对路径、标准库导入可用。
- 在该解压目录实际执行 `build_local.sh`：无网络/安装/提权，源码构建成功，**四项 CTest 全部通过**，重建后的冻结自检与 Linux profile doctor 再次通过。
- 回传 Mac 后再次核对 ZIP SHA、全部 272 文件哈希、可执行位、无重复/越界/符号链接条目，以及允许清单中无 `.git`、SDK、原始 payload、真实文档或凭据文件。

日志为服务器的 `operator-tests-native.log`、`package-result-native.json`、`roundtrip-help.log`、`roundtrip-rebuild.log`；副本保存在本机同一交付目录的 `validation/`。

## 慧京接下来执行

按[操作说明](desktop_native_check.md)先运行 `./selftest.sh`，再用已有批采配置运行 `./run_check.sh --batch-config /实际/config.json`。默认三类流程有效后，追加 `--operations all` 跑 18 类各一次，回传终端打印路径中的 `feedback.zip`。

“流程有效”不要求预测全对，错误预测同样计入准确率。配置不匹配、焦点/权限/进程覆盖变化或语义验证失败由 Codex 根据报告修复，不能放宽成成功。该包尚未在慧京 WPS 虚拟机运行；**在线准确率、跨重启、连续历史与完整端到端时延仍待回传**，正式批采没有恢复。
