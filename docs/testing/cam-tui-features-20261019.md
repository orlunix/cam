# CAM TUI 功能验收补测（2026-10-19）

本次按当前 v0.1 的功能逐项验收，不要求每个物理 Server 都完成全部工具登录测试。
此前七个实际 Node 的连接、生命周期、跨站点通信与模型结果见 [fleet 验收](cam-tui-fleet-20261019.md)。本文补充真实交互、故障和调度边界。

## 验证范围

| 流程 | 验证内容 |
| --- | --- |
| 创建与管理 | New 表单、绝对目录校验、缺失工具失败原因；status/capture/send/key/stop/kill/reboot/remove；同 ID reboot 与保存的 launcher/参数 |
| Nodes | socket/stdio transport、新增/编辑、错误 token、连接/断开/重连、移除连接保留 Server 与 agent |
| 多终端 | 鼠标开 tab/切换、键盘输入、独立 history、PageUp/PageDown/滚轮/End、选区稳定、UTF-8/CJK、颜色、alternate screen、工具鼠标透传、真实 pane resize |
| 原生剪贴板 | 真实 X11/xclip 写入并读回跨行 CJK 文本、跨 tab 保留选区、Ctrl+C 复制不传入工具、无选区 Ctrl+C 传入工具、Ctrl+V 不追加 Enter、未启用 bracketed paste 时拒绝多行 |
| 多窗口与恢复 | 同 daemon 两个 UI，单 writer 与只读视图，退出后交接；Server 与 daemon 重启后通过新输出确认恢复；退出 UI 不停止任务或 agent |
| 任务与 loop | interval/at、prompt/本机命令/loop 注册、编辑保留 ID/历史/暂停状态、暂停/恢复/删除、历史、并发上限、不重叠、超时杀掉子进程、unknown 不重放、连续三次失败停用、删除后不复活 |
| Forwarding | 双向与多路径、完整消息/返回 stdout、来源与目标权限、去重冲突、TTL、断线恢复、背压、unknown 与不可重放边界 |
| 健康 | IPC/调度 tick、已认证连接与心跳、活进程但通道卡死、健康 Node 不被慢 Node 拖延、离线 stale 保留原状态、恢复 |
| 降频 | 默认 30 秒普通快照；修改立即刷新相关 Node；本 daemon 新建/重启的 agent 临时 2 秒；started/dead/取消后结束；已有 initializing 记录不触发持续快查 |

隔离进程测试使用真实 Rust Server/daemon、Python SDK、tmux 2.7、实际 PTY 与可见文本解析，不修改已有用户 agent。剪贴板用例显式使用 VNC `:171` 与已有 `/home/utils/xclip-0.13/bin/xclip`；未重装共享工具。

## 实测中修复的问题

- Settings 增加任务编辑，保留任务身份、历史与附加设置。
- 初始 capture 保留空白行，避免旧 launch history 挤进可见画面。
- 旧 tmux attach 用有超时的只读 PTY observer 恢复 bracketed-paste 状态，回收后不留下 client 或改变窗口大小。
- control client resize 直接使用可见尺寸，去掉错误的状态栏补偿；实测可见 66×18 时之前实际 pane 为 66×19。该处理符合 [tmux 2.7 源码](https://raw.githubusercontent.com/tmux/tmux/2.7/resize.c) 对 control client 的尺寸计算。
- 普通轮询优先处理，跳过错过的 tick，避免慢 Node 的启动检查持续拖延其它 Node。
- 取消超时请求时释放 relay pending 容量；旧请求的清理不会删除使用同一 operation ID 的新请求。
- 终端输入通过有容量限制的顺序队列发送，避免异步单键请求颠倒；等待远端应答期间累积的同一终端按键会合并，保持 Enter 与文本的顺序。

## 证据与限制

Python 全套：1717 passed、1 skipped；Rust workspace：18 passed；真实 Server/daemon/tmux/TUI/PTY 进程测试：31 passed，159.710 秒。剪贴板用例在真实 X11 上执行，没有跳过。Rustfmt、diff 检查和 34 个 Python 模块的 Python 3.6 语法检查通过。

后续个人部署为 camc `1.2.188`、Rust `0.1.0`，release `cam-20261019-v1.2.188-a629bed558`。7 个可连接 Node 均已切换并通过 63 个 release 文件及 5 个个人入口的哈希核对；每个 Node 恰好一个 Server。静态 Linux x86_64 musl 构建保持无系统 glibc 依赖。本机配置与新配置默认的普通同步间隔均为 30 秒。

36.12 秒的只读观察中，有 agent 的 4 个 Node 的相邻快照时间戳差均为 30 秒；其余 3 个 Node 没有可计时的 agent 记录，不能据此声称它们的采样间隔也已直接测得。7 个 Node 均 ready，聚合保留 64 个原有 agent，定时任务为 0。原生剪贴板 helper 已加入本次 VNC 测试窗口的 PATH，没有修改全局 PATH。

## 额外真实 qsub 试验暴露的问题

后续已经按“tmux 接管排队、run 立即返回”的约定修复排队计时，并完成真实 qsub 等待补测；见 [异步 qsub 修复记录](camc-qsub-async-20261019.md)。以下保留原始失败证据，SDK worker 退出问题仍独立未决。

首轮自动 launcher 的真实 Claude/Codex 回答结果仍以 fleet 报告为准。本轮新增的显式自定义 qsub 启动没有到达 started，不能记为成功的模型验收：

- 测试 agent `371db135`、job `1935780816` 处于等待 dispatch 时，被已知工具的 20 秒 boot timeout 判为 failed。自定义 launcher 没有 auto runner 的 `launch_status_file`，目前 queue 等待会进入该计时；不能把普通 auto 路径已有的等待保护推广到自定义前缀。
- 同一试验还观察到本机 SDK worker 退出，Server 进程继续存在，后续操作返回 `backend unavailable`。worker 的退出状态为 0，尚未确定是输入通道关闭还是 SIGTERM 清理路径，不能断言与 boot timeout 是同一原因。本次恢复了经过身份验证的本机 Server，之后元数据和删除测试 agent 恢复正常；没有以缺少证据的源码修改代替根因定位。
- 该额外试验使用本机 `/tmp` 工作目录，也不适合作为跨主机成功验收的 fixture；后续真实工具重测需使用共享绝对目录。
- 另建的共享目录交互分配 `1935796134` 持续 PEND，LSF 报 waiting for scheduling；已精确取消，没有启动第二个工具或回退到本机执行。

最终两个测试 job 均核实为 EXIT；测试 agent、隔离 Codex home 和工作目录已删除，未取消原有 allocation。所有隔离功能用例已经执行，但额外真实 qsub 场景存在上述待修问题，不能宣称所有真实 LLM/launcher 场景全绿。
私有执行记录位于 `/tmp/cam-tui-feature-e2e-20261019/`。该目录及旧 fleet 目录可能含连接信息或私有终端内容，不能整体提交或打包共享。

本验收针对当前 v0.1；增量状态事件、完整 cron、跨 daemon loop owner 自动迁移、Ghostty 原生 PTY、跨 Client 只读 attach 仍属于后续设计。心跳与连接健康不冒充端到端 forwarding 主动 probe，未验证时保留 unknown。其它终端的 OSC 52 支持也需要分别验证。
