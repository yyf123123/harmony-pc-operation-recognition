# 已导出的离线类中心模型

这些参数取自 Windows 已完成的 trial 分组离线实验 `outputs/offline_operation_recognition_20261003_102543`，是交叉验证完成后用全部合格样本重新拟合的类中心。`model.json` 的 `center_to_operation` 为标签映射，`centers.csv` 为 L2 中心向量。推理用余弦最高分；拒识阈值未拟合。模型不包含训练样本，尚未验证设备实时推理及自动应用切换。

| 应用 | 特征 schema | 样本 / 类别 | model.json SHA-256 | centers.csv SHA-256 |
|---|---|---:|---|---|
| 哔哩哔哩 | `cross_app.vma.feature.v2`，2048 维 | 180 / 6 | `0c0b171bca7b7e1d861e15da3b4845ebf66cbbe899d7cd6e02fab809e0068337` | `51f93ac0895c3e8900f600ce01d1aa3c27394d7836708c04790c1b03674d8f80` |
| 华为浏览器 | `cross_app.vma.feature.v2`，2048 维 | 250 / 5 | `0836b119d00326c4a8e419050d5306cbf33c4abc0f8a5bb1fd70c9444dc28c08` | `718b6b7d3bd37d8cbd41ba753aa1a7b100c932c17419848aaf29a3fe48a3d878` |
| WPS | `wps.vma.feature.v1`，2048 维 | 1332 / 18 | `2ca6bcce2856798c0998c28414053d9338e28a61923c50184c3f228a7cd42c7f` | `dbd1277f21252f2827ec965a2b195365b872b0f1eae4c7b2eb709d7aaba233ea` |

`model.json` 的来源说明和标签映射已经就地保存；上述摘要哈希用于核对首次纳入仓库时的文件。只有类中心模型进入仓库，spherical k-means 可由外部数据重新评估并导出。模型格式的 `schema_version` 对新导出产物为 `cross_app.vma-cluster-model.v1`；随附原产物沿用原有字段格式，推理器按其字段读取。

## Linux WPS 独立候选

用户授权新训练的 [wps_writer_pilot_v1](linux/wps_writer_pilot_v1/) 使用慧京 Linux 小试的 doc1–3 共 54 个独立样本，另两文档各 13/18、连续 66/100，详见[模型说明](../docs/linux/model_20261023.md)。18 个中心、2048 维，附来源/训练范围与 SHA 清单。它是尚未晋升默认模型的小试候选；可由 Python 推理器显式加载，Native 冻结注册和内网包保持原模型。真实数据和逐样本训练明细不在仓库。
