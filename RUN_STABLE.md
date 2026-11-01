# 2026-11-01 可运行整理版：完整受控窗口

本版只提供约30秒完整窗口。请在新目录解压，不覆盖现有采集数据或开发仓库。
源码以短窗引入前的9ea43af为基线，补至11月1日已运行的独立WPS桌面入口、进程变化诊断和鸿蒙现场说明；完整来源见SOURCE_SNAPSHOT.json。

## 离线编译与自检（macOS/Linux）

```sh
cmake -S . -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
build/native/native/opsctl selftest --models models --fixtures native/fixtures/raw-window
build/native/native/opsctl doctor --models models --app wps --model-profile linux-wps-writer-pilot-20261023-v1
python3 -m scripts.audit_models
```

只需CMake 3.16+、C++17/C11编译器；测试和编排使用Python3.10+。完整Python评估另需requirements.txt中的NumPy。macOS只验证离线，真实/proc明确拒绝。

## Linux已有桌面/自动化

Linux二进制包先执行`./selftest.sh`；沿用现有linux.wps.batch.v1配置执行：

```sh
./run_check.sh --batch-config /已有配置的绝对路径/config.json
```

默认输入、保存、加粗三项有限检查。WPS/PID、焦点、实际采集权限及原自动化必须有效；约30秒/窗口。失败保留证据，不能据预测是否正确筛选样本。详细说明见docs/linux/desktop_native_check.md。

## Linux独立测试桌面

已有Xvfb、xauth、D-Bus、xfwm4、X11开发库和WPS时，使用scripts.linux_server_desktop。先人工初始化私有测试profile并确认WPS许可；入口不代替确认、不安装依赖、不提权。运行时使用新的合成DOCX，不接管其他用户桌面。

```sh
python3 -m scripts.linux_server_desktop --help
python3 -m scripts.linux_server_desktop --profile-root /已有私有测试profile --output-root /新的运行目录 --native "$PWD/build/native/native/opsctl" --models "$PWD/models" --app-version 实际WPS版本
```

详细环境和现场接线分别见docs/linux/environment.md、docs/handoff/ENVIRONMENT.md和docs/handoff/MEMMGRSERVER_OPERATOR_CARD.md。鸿蒙内网极简现场说明对应已携入的独立包；当前Linux二进制不能在鸿蒙运行。

## 已知效果与限制

软件可运行不等于分类全对。既往服务器输入操作仍错分，保存/加粗命中；完整18类、跨环境与鸿蒙服务真机尚未验收。Linux参数仍是54条训练的小试候选，未用1500重新训练，不自动晋升默认。短窗口实验没有纳入本版。用户已授权通过目标公开仓库共享代码和必要运行模型。
