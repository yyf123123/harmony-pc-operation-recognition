# 鸿蒙现场一页卡：接服务、看结果

适用已带入的约 364 KB memmgrserver 极简包。本卡和显示脚本只作辅助，不替换原服务或模型。

**编译只接三处：**

1. 原服务实际 target 的 BUILD.gn 加 `deps += [ "operation_recognition:memory_ops_event" ]`。此相对路径要求 BUILD.gn 位于 server/memmgrserver；不要重复加入包内 .cpp 源文件。
2. `platform_bridge.cpp` 的 ReadOperationFocus 接真实前台接口，填 app_id、window_id、主 PID、完整授权 PID 集合和接口来源。默认 -1 不改成虚假成功；这一步请现场同事帮你定位现有接口。
3. 原服务分别放 `hop_embedded_start`、`hop_embedded_trigger`、`hop_embedded_stop` 三调用；沿用原来编译、部署 memmgrserver 的命令。模型随原服务资源安装，输出选原服务已有可写目录。三个调用的可复制片段见《详细接入说明.md》。

**效果默认就在文件里：**

`输出父目录/onsite-本次目录/` 下：status.txt 是中文状态，stage.jsonl 是阶段，results.jsonl 每个有效窗口新增一个预测标签。看到 action 阶段才做一个动作，整个窗口约 30 秒及额外开销。编译通过或 model_ready 不等于识别成功。

**更直观的现场展示：**

先启动下面的只读脚本，再从原服务调试入口发一次 trigger；原窗口结束前不要连续触发。脚本只接人工对照标签，不把标签传给识别核心。

```sh
sh show_wps_result.sh /实际输出路径/onsite-本次目录 SAVE_DOCUMENT
```

脚本提示 action 时执行“保存”，结果完成后显示新事件ID、预测标签、是否命中和耗时。实际识别结果默认写文件，未自动接 HiLog；保持 publish 回调为空即可使用此显示方式。

**先检查这三个动作：**

| 动作 | 触发前准备 | action 时操作 | 正确标签 |
| --- | --- | --- | --- |
| 输入 | 空白副本，光标就位 | 输入短句 | WRITE_TEXT |
| 保存 | 已有可写副本且有未保存修改 | 点击保存 | SAVE_DOCUMENT |
| 加粗 | 预先选中未加粗文字 | 点击加粗 | FORMAT_BOLD |

先各一次，流程有效后各 5 次。向导师展示原服务内结果，记录有效窗口/请求、正确标签/有效窗口、正确标签/请求和每次耗时；错误保留，失败不冒充成功。日志和表可留在内网，无需传出闭源材料。
