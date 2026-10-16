# Harmony PC operation recognition

私有代码仓库：鸿蒙 PC 的设备采集、VMA 特征构造和已知应用内的操作识别。数据、SDK、日志、截图和构建产物均不随仓库分发。原项目和原始数据保持在原位置。

Linux 分支新增本机 WPS 采集、回放、现有 Native 推理与历史接口，运行方法和团队分工见 [Linux 版本](docs/linux/README.md)，项目状态见 [PROJECT_STATUS.md](PROJECT_STATUS.md)。它保留原 Windows/OpenHarmony 路线；Linux 桌面与真实 WPS 验收仍需按目标环境执行。

## 新电脑最短离线步骤

Python 3.10+；只做离线评估需要 NumPy，不需要设备、HDC 或鸿蒙 SDK。

```powershell
git clone https://github.com/yyf123123/harmony-pc-operation-recognition.git
cd harmony-pc-operation-recognition
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\evaluate.ps1 -Python .\.venv\Scripts\python.exe -App bilibili -Data <外部CSV目录> -Output <新输出目录> -Schema cross_app.vma.feature.v2
```

macOS/Linux 使用同一 Python 模块，无需 PowerShell：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m classification.evaluate --app bilibili --data /path/to/csv --output /path/to/new-output --schema cross_app.vma.feature.v2
```

输入目录必须包含 `dataset_manifest.csv`、`labels.csv`、`vma_vectors_l2.csv`，三个文件的 `sample_id` 必须一致；manifest 和 labels 都须有 `trial_id`、`session_id`、`label_id`、`operation_label`。不提供模拟训练数据，也不以 `.pkl` 操作序列替代 VMA。

默认 5 折按 trial 分组；完整 trial 不会被拆分。`--group session --folds 0` 是严格留一 session 评估。类别中心、聚类中心及 Hungarian 聚类到标签映射均仅在训练折拟合；未设置拒识阈值。输出 pooled Accuracy/Macro-F1、逐操作指标、混淆矩阵、逐折结果和预测；两种方法再以全体有效数据拟合并导出独立候选模型。这些全量模型没有独立测试集评分。

WPS 使用 `--app wps --schema wps.vma.feature.v1`。浏览器使用 `--app huawei_browser`。跨应用 signed v3 使用 `--schema cross_app.vma.feature.v3_signed`，为 8192 维，不能送入随仓库提供的 2048 维模型。

## 采集：另需连接设备

采集部分只依赖 Python 标准库；重新采集需要鸿蒙 PC、HDC、设备权限和可观察的应用 UI。配置中的状态标记及坐标来自原采集环境，新机器/新应用版本须先做小规模校验。不要对已有原始数据目录重复写入。

```powershell
# 仅编译，不连接设备；SDK 不随仓库提供。
.\scripts\build.ps1 -Sdk <OpenHarmony-native-SDK目录> -Output build/collector
# 以下命令会实际连接设备并启动采集，请按需显式执行。
.\scripts\collect.ps1 -App bilibili --target <目标> --hdc <hdc.exe路径> --root <新的外部采集目录> --rounds 1 --collection-stage smoke
.\scripts\collect.ps1 -App huawei_browser --target <目标> --hdc <hdc.exe路径> --root <新的外部采集目录> --fixture-base-url <设备可访问的固定页面地址> --rounds 1 --collection-stage smoke
$env:HDC='<hdc.exe路径>'
.\scripts\collect.ps1 -App wps --target <目标> --out <新的外部采集目录> --trials 1 --no-build --collector-binary build/collector
```

浏览器和哔哩哔哩通过 `/proc/PID/smaps` 与 `clear_refs` 的 HDC 窗口采集实现，不依赖 C 二进制；C 采集器供 WPS 使用。浏览器固定页面可用 `python -m http.server 8765 --directory apps/huawei_browser/fixtures` 在主机提供，再显式传入设备可访问的 URL。正式采集仍需要原实现中的 `--formal-gate-report`；本仓库不捏造通过报告。

## 特征构造与推理

浏览器/哔哩哔哩采集器同时写出 v2 与 signed v3。已有外部 SQLite/特征 JSON 可转成 CSV：

```bash
python -m features.export --root /path/to/captured-workspace --database /path/to/metadata.sqlite --app bilibili --schema cross_app.vma.feature.v2 --session SESSION_ID --output /path/to/new-csv
python -m features.build --manifest /path/to/window-manifest.json --schema cross_app.vma.feature.v3_signed --output /path/to/new-feature.json
python -m features.wps --root /path/to/wps-raw --output /path/to/new-csv --compact-only --skip-pairwise
python -m classification.predict --model models/bilibili/model.json --schema cross_app.vma.feature.v2 --vectors /path/to/vma_vectors_l2.csv --output /path/to/predictions.csv
```

`features.build` 的 manifest 格式见 `docs/window_manifest.md`。WPS compact-only 读取现有窗口序列的聚合值，不重新解析大型报告；不完整 trial 的组级筛选需由使用者在评估前依据完整 18 类要求完成，不能把缺类数据称为失败采集。

随附模型是 Windows 已验证的类中心参数，来源、SHA-256、标签映射与兼容说明见 `models/README.md`。没有训练样本。没有已接入鸿蒙 PC 的三应用自动选择或实时切换引擎；离线命令显式指定应用，Native 库中的应用身份由独立适配层提供。在线特征一致性、时延和跨新 session 泛化尚未在鸿蒙 PC 系统服务中验证。

## Native C++ 用户态库

只需 CMake 3.16+ 和 C++17 编译器，即可在 Windows/macOS/Linux 上编译无设备依赖的静态库与离线命令。构建不需要 HDC，也不需要鸿蒙 SDK。`models/` 的三个 `centers.csv` 在运行时加载；C++ 中冻结的 schema、标签顺序及两份模型文件的 SHA-256 与已验证导出对应。先用标准库审计脚本核对元数据：

```powershell
python -m scripts.audit_models
cmake -S . -B build/native
cmake --build build/native --config Release
ctest --test-dir build/native -C Release --output-on-failure
```

Windows 单配置生成器下示例程序通常在 `build/native/native/operation_predict.exe`；Visual Studio 多配置生成器下可能位于 `build/native/native/Release/`。下面的 `$predict` 取实际构建路径；输入是**外部** 2048 维 `vma_vectors_l2.csv`，输出必须是新文件：

```powershell
$predict = '.\build\native\native\operation_predict.exe'
& $predict --models models --app bilibili --schema cross_app.vma.feature.v2 --vectors $biliVectors --output $biliPredictions
& $predict --models models --app huawei_browser --schema cross_app.vma.feature.v2 --vectors $browserVectors --output $browserPredictions
& $predict --models models --app wps --schema wps.vma.feature.v1 --vectors $wpsVectors --output $wpsPredictions
```

`operation_feature` 可从外部语义特征 CSV 生成向量。WPS 输入为聚合后的正向 excess pages；跨应用 v2 输入为 baseline、action、post-action 的 `Referenced` 语义页数。CSV 列为 `namespace,feature_key,pages`，命令例子和适配边界见 [Native 系统服务适配说明](docs/native_adaptation.md)。signed v3 是独立的 8192 维 schema，现有 2048 维模型及 C++ 入口都会拒绝它。

现有 Native 用户态系统服务可链接 `harmony_operation_recognition` 静态库。可选的 OpenHarmony native SDK 交叉构建只产出本地二进制，不会部署设备：

```powershell
$env:OHOS_SDK = '<OpenHarmony native SDK 根目录>'
cmake -S . -B build/ohos -G Ninja '-DCMAKE_TOOLCHAIN_FILE=native/cmake/openharmony.cmake' -DBUILD_TESTING=OFF
cmake --build build/ohos
```

仓库不含系统服务注册配置。进入内网后，按 `native/examples/service_adapter_template.cpp` 实现焦点证据、窗口采样、Referenced 采集、权限与结果输出，并在真机核对；没有默认平滑或切换迟滞策略。

## 代码与验证

`collector/` 是 C 构建、窗口、HDC、持久化和 WPS 编排；`apps/` 是实际 UI 采集器与操作目录；`features/` 保留冻结及 signed schema；`classification/` 提供评估、训练、导出和推理；`native/` 是用户态 C++ 计算核心与待接适配接口；`scripts/` 是稳定 Windows 入口。

```bash
python -m unittest discover -s tests -v
python -m scripts.collect --app bilibili --describe
python -m classification.evaluate --help
```

迁移对应关系见 `SOURCE_MAP.md`；本次验证详情和未验证项见 `docs/validation.md`。原环境的路径、账户、设备序列号及日志没有纳入仓库。模型许可/数据使用权不由本整理产生新授权。
