# 鸿蒙内网适配环境与边界

- 用户只能携入代码现场适配，不能把内网工程、闭源接口材料或日志传出。本包不要求远程连接、上传报告或回传源码；故障在内网用 status.txt/status-history.jsonl 本地诊断。
- 已携入的2026-10-29 memmgrserver极简包使用 `server/memmgrserver/operation_recognition/platform_bridge.cpp`、`event/operation_recognition_event.h` 与 `memory_ops_event` GN模块；不含下文旧通用包的 operator/路径、fixtures、启动黄金回放或独立自检工具。以[极简包现场卡](MEMMGRSERVER_OPERATOR_CARD.md)为准；新增只读显示脚本不修改服务、权限或识别输出通道。
- 接入现有 Native 用户态服务，沿用其构建、部署、服务身份、注册和访问控制。本包不自动注册服务、修改权限策略或 sudo；不需要设备 Python、网络下载或安装依赖。
- 唯一平台桥接文件为 operator/platform_bridge.cpp；真实 focus 必须给出当前应用、窗口、主进程和完整进程覆盖，不能固定 PID、伪造焦点或跳过失败。回调须及时返回，阻塞厂商接口不能被核心强制安全中断。
- 默认采集使用进程启动 ticks、smaps/Referenced、clear_refs 及实际系统页大小。仅在内网接口不兼容时替换对应底层回调；字段缺失、权限、部分 PID 失败或重启必须报错，不能填零继续。
- clear_refs 只用于已授权测试目标，与其他页位采集器确定唯一清位协调者；不同时竞争清引用。使用测试文档副本，不覆写历史证据，不持久化动作命令中的正文。
- 不自动清缓存、制造内存压力、杀应用、加载内核模块、修改系统参数或执行页预取/回收。不得操作其他用户的桌面或未经授权的应用进程。
- 模型使用三个原冻结鸿蒙配置；不加载 Linux 小试候选，不自动重训、平滑或增加拒识阈值。C++/SDK/架构/运行库以现场宿主为准；已有 OpenHarmony ARM64 交叉构建不是设备兼容证明。
- 保持 2×5s baseline、15s action、5s post 的受控协议；action 才执行动作。不默认自主分段。启动 offline_ready、触发已接收、自检通过和真实窗口成功分别记录。
- 不携带或索要 SDK、真实文档、采集数据、凭据、token 或私钥；本包不包含这些内容。软件验证、宿主集成、真机效果与内存管理收益分别验收。
