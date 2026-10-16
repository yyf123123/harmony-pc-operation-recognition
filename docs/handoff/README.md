# 双入口 Native 移植包

`harmony_operation_recognition` 和 `harmony_vma_collector` 是两个链接目标，使用同一个 C++ 运行核心。`NativePipeline` 原入口保留；新增 `ServiceRuntime` 和窄 C ABI 用于完整受控窗口，`opsctl` 使用相同原始特征和模型实现。

包结构：`source/` 源码、`include/` 公共头文件、`models/` 冻结模型、`fixtures/` 合成夹具、`targets/架构/` 可执行文件及两个静态库、`operator/` 脚本和桥接、`docs/` 操作说明、`BUILD_INFO.json` 编译与运行库信息、`SHA256SUMS` 全文件清单。静态库仍依赖目标的 C/C++ 运行库，不意味着完全静态链接。包内不包含 SDK、真实文档/采集数据或凭据。

本机打包使用标准库 Python（目标机不需要）：

```sh
python3 -m scripts.package_native --output /新的包目录 \
  --binary macos-arm64=build/native --binary ohos-arm64=build/ohos-handoff \
  --elf-reader /实际SDK/native/llvm/bin/llvm-readelf
```

源码中的 CTest 需在仓库根目录有 models；包内脚本以 BUILD_TESTING=OFF 编译，再通过包外层的 models 运行原生 selftest。无需复制 SDK 或联网安装依赖。不同工具链、目标 API、libc++ 版本或 ABI 不匹配时，用同包源码和内网现有工具链重建。

服务接入只需真实平台事实，不能用固定应用 ID 假装焦点成功。闭源接口缺失时回调明确失败。真实桌面 18 类验证、鸿蒙宿主服务及页面收益均另行验收，交叉编译通过不代替这些证据。
