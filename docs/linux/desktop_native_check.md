# 慧京本机验证：模型和代码直接传输

2026-10-28：无需提供虚拟机远程连接。将私下生成的 `linux-wps-native-20261028-x86_64.zip` 和 `.zip.sha256` 一起转交慧京；不要从当前公开 GitHub 获取尚未发布的新模型/代码，也不上传真实文档。Linux x86_64 包有同一 Native 核心、显式 Linux 候选、静态库/头文件、源码、自检/重建脚本、操作卡和 SHA 清单。

## 慧京只需做这些

用普通桌面账户解压到新目录，启动原采集使用的 WPS/RPC 环境，进入包目录：

```sh
./selftest.sh
./run_check.sh --batch-config /现有自动化配置的绝对路径/config.json
```

使用已有 `linux.wps.batch.v1` 配置（六个 argv 角色、五份测试模板、六条工作流）；代码复用原准备、动作和语义验证，不能改成无条件成功。配置中的脚本与模板仍为她电脑原路径。WPS 重启后显式更新 `root_pid`/`pids`，也可追加 `--root-pid 实际主PID --covered-pids 实际PID列表`，列表用逗号分隔。不能照抄示例 PID；焦点/权限/覆盖失败先反馈，不 sudo 或放宽检查。

默认输入、保存、加粗三个独立操作，至少约 1.5 分钟加前置开销。运行时保持 WPS 聚焦。报告 `status=passed` 表示动作/窗口/语义/输出有效，不要求预测全对；流程通过后运行：

```sh
./run_check.sh --batch-config /现有自动化配置的绝对路径/config.json --operations all
```

全 18 类各一次，至少约 9 分钟加前置开销。每个独立操作启动新 Native 会话，避免把未观测的恢复动作拼成历史。没有开启 1500 批采，也不替代连续会话/跨重启验收。

结束后只需发回终端打印路径中的 `feedback.zip`。它按允许清单包含准确率、分阶段耗时、分离真值/预测、health 和 Native 错误；不递归收集工作副本或语义正文。失败也保留报告；配置文件不存在/JSON 语法错误等启动前问题按终端提示反馈路径，由 Codex 修复适配，不要求她重新开发动作。

## 依赖与兼容

编排使用 Python 3.10+ 标准库，模型推理无需 Python/NumPy；桌面仍需已有 X11/xdotool 和原自动化/RPC 依赖。GLIBC/GLIBCXX 不匹配时，在已有 CMake/G++ 环境运行 `./build_local.sh`，会从同包源码生成独立 local-build 并自检；不覆盖预编译程序，不安装依赖或提权。具体依赖见包内 `runtime-dependencies.txt`。

## 维护者生成包

在已阅读环境约束的 Linux x86_64 项目唯一新目录，从干净源码构建/验收后执行：

```sh
python3 -m unittest tests.test_linux_desktop_check -v
python3 -m scripts.package_linux_wps --build /已验证的构建目录 \
  --output /私有新目录/linux-wps-native-20261028-x86_64 \
  --source-sha 实际源码提交 --report docs/linux/progress_20261028.md
```

打包器拒绝非 Linux x86_64 ELF、已存在输出及源码符号链接；采用源码允许清单，不包含 `.git`、SDK、数据集、真实文档或凭据。生成源码/平台记录和 SHA 清单后，从另一工作目录运行**同一产物**的自检，成功才输出 ZIP。真实桌面验证仍由慧京执行；合成包装器测试不提供准确率。
