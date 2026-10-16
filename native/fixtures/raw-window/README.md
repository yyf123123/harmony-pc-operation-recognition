# 合成原始窗口夹具

此目录为人工合成的 smaps，PID、地址和路径均为虚构。不包含真实文档，不构成识别准确率证据。

manifest.csv 每行是一进程一阶段；同阶段相同 index 的多 PID 相加，再做 baseline 中位数及 action/post 正向 excess。page_size 是原采集环境真实页大小，回放不得使用回放机页大小。index 从 0 连续，至少两个 baseline；action、post 各一个。

包含 .so、native heap、匿名、重复文件语义键、带空格及 deleted 路径、stack。WPS 使用冻结 C 分类器；另外两应用使用 FilePage other / AnonPage other 语义，不可互换。
