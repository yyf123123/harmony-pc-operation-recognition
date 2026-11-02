# 完整窗口公开共享版（2026-11-01）

2026-11-02 维护：新增全部 18 类有限验收、同次采样侧录与独立服务器候选参数，详见 [最新报告](docs/linux/server_wps_20261102.md)。默认模型/完整窗口不变；候选回放 26/37、普通 GUI 输入仍错分，未晋升默认。下方三项结果与验证数为 11 月 1 日版本记录。

用户已授权通过公开仓库向同事共享项目代码和必要运行模型。此版由实际远端linux作为父提交发布，经筛选的源码与本地已验证完整窗口版一致；短窗口/自主分段/watch及其本地实验Git历史不发布。main不改动。

先读[运行说明](RUN_STABLE.md)。克隆时选linux分支：

```sh
git clone --branch linux https://github.com/yyf123123/harmony-pc-operation-recognition.git
cd harmony-pc-operation-recognition
cmake -S . -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
build/native/native/opsctl selftest --models models --fixtures native/fixtures/raw-window
build/native/native/opsctl doctor --models models --app wps --model-profile linux-wps-writer-pilot-20261023-v1
```

CMake3.16+、C11/C++17；Python编排要求3.10+，完整Python评估另需requirements.txt。Linux真实采集要满足[环境规则](docs/linux/environment.md)，默认X11/Xwayland焦点桥。Mac只能做离线验证。

验证依据：Mac和Linux各5/5 CTest，Mac Python94项中85通过/9 Linux跳过，Linux29项相关测试通过，三冻结模型SHA审计通过。源码和模型逐文件核对与已测版本相同，公开授权仅更新规则及发布说明。

同一源码的真实WPS三项（12.1.0.17900、独立测试桌面）：

| 动作 | 有效窗口 | 预测 | 命中 | 触发至结果就绪 |
| --- | --- | --- | --- | --- |
| 输入 | 是 | INSERT_PAGE_BREAK | 否 | 30.639秒 |
| 保存 | 是 | SAVE_DOCUMENT | 是 | 30.620秒 |
| 加粗 | 是 | FORMAT_BOLD | 是 | 30.637秒 |

默认仍为两次5秒baseline、15秒action、5秒post。这是三项功能运行，不能称完整18类准确率或自由操作实时验收。Linux模型仍仅54独立样本训练的小试参数；不默认重训、拒识、平滑或扩大批采。输入错分、跨独立会话和鸿蒙真机接线仍待验证。

仓库只包含代码、运行参数、合成夹具及运行说明。真实文档、原始采集、逐样本预测、运行日志、SDK、闭源材料和凭据不随源码公开；不得因公开授权扩大到这些材料。
