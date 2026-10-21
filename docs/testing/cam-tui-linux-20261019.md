# CAM TUI v0.1 Linux 验证记录

日期：2026-10-19。分支：`cam-tui`；本轮实现位于工作区，未执行 commit/push。

范围与使用方式见 [v0.1 说明](../cam-tui-v0.1.md)。这是可运行首版的验证，不代表完整目标 Spec、全部 Linux 发行版或真实 AI 工具登录流程已验收。

## 环境与产物

- 当前 Linux x86_64 主机；Rust 1.71.1，Python 3.12.2，tmux 2.7。
- 构建了 `cam-server`、`cam-forwarder`、`cam-clientd`、`cam-tui` 四个 musl 静态二进制。
- 产物目录：`/tmp/cam-router-target/linux/x86_64-unknown-linux-musl/`，校验值在同目录 `SHA256SUMS`。
- `readelf -l/-d` 确认四个最终 ELF 都无 INTERP 与 DT_NEEDED；TUI/daemon/Server 的版本入口可正常运行。未安装到 Home、shared 或远端机器。
- 默认 Client 配置在隔离 Home 中验证为 `cam-client/<hostname>.json`、权限 0600；正常启动自动拉起 daemon，界面收到首个快照约 0.85 秒。测试使用显式的临时 runtime 目录。显式配置文件仍可使用。

## 已通过的验证

| 层次 | 结果与边界 |
| --- | --- |
| Python 全量回归 | 1711 passed，1 skipped；12 项原有 datetime 弃用警告。包含既有 camc 启动、launcher、生命周期和消息路径 |
| 聚焦 SDK/管理 API | 50 passed，已包含在上面的全量结果中；覆盖授权、操作结果持久复用、expiry 变化不重做、loop owner 与当前状态门控、快照不泄露环境 secret |
| Rust workspace | 16 tests passed；覆盖协议、转发失败状态、tmux 2.7 字节输入、VT/UTF-8/颜色、备用屏幕、粘贴、稳定选区、单个/多个中文宽字符、回到底部恢复跟随、连接状态恢复、剪贴板 helper 超时及小窗口布局 |
| Router/TUI 真实进程 | 最终完整 23 项通过；使用静态 Client/生产 Server/stdio bridge，以及隔离多 Node 的 Server test harness，共用同一份 Server core；均在一台机器上 |
| 最终交互二进制复核 | 另通过 2 项真实 PTY/生产 Server 可用性检查：默认自动启动、实际 bash 启动到 started、160 行历史翻页/滚轮、释放后保留可见选区、OSC 52 内容、粘贴不补 Enter、80×24 缩放及退出回收 |
| 格式与兼容语法 | rustfmt、git diff --check、文档本地链接与代码围栏、SDK/worker/demo 的 Python 3.6 grammar 检查通过；未在 Python 3.6 runtime 运行全部测试 |

真实进程用例观察到：

1. 两个 agent 的终端 tab 分别接收输入，不串到其它 agent；只读视图不能写入/resize。
2. 通过实际鼠标菜单 reboot 后，同 ID 的新工具可在重新连接的 tab 中继续接收输入。
3. 通过实际 New 表单创建第三个 agent，按原后端策略启动并自动打开交互 tab。隔离测试显式使用 local launcher，不改 NV 默认策略。
4. PageUp/滚轮由 UI 接管；初始历史中的字面 `%begin/%output/%end` 不被误认为 control 协议。
5. 退出 UI 后 daemon 与 agent 继续运行；一次性任务执行、loop busy 延后/idle 投递、单调度 owner 和 60 秒 continuation 活动窗口生效。
6. daemon 崩溃后的在途任务标 unknown，不自动重复执行；后台 forwarding 支持双向流量，Node 断开不影响其它 Node。
7. 旧 Router 未通告管理能力时，不发送未知 SDK 帧，保留其转发连接；失联 agent 显示 stale，最后的 alive 状态不被改写成 dead。
8. 临时 demo 使用生产静态 Server、daemon、TUI 和两个真实 bash agent；通过 UI 输入执行文件写入。退出后专用服务子进程和 agent socket 均被回收。
9. 显式开启 F8 鼠标透传后，真实 raw-mode 工具收到转换为终端局部坐标的按下和释放事件；默认鼠标依然由 CAM 接管。

## 可用性实测发现及修复

- demo 的 `tool_args` 曾误传字符串，后端逐字符展开导致 bash 退出。原先文件写入检查能在 tmux 保留的诊断 shell 中通过，不能证明目标工具已启动。现已改为参数列表，并在显示 UI 前等待两个工具均为 `alive / started`；回归也检查保存的真实 argv 与状态。
- 成功收到快照后清除初始 Connecting 提示；连接恢复后更新 Ready，同时保留正常操作消息。
- 父进程 `NO_COLOR=1` 会使 Crossterm 0.27 发出裸 SGR reset，连选区属性也被清除。全屏 TUI 现在显式启用配色，并用反显标记选区；实际 PTY 验证松开后高亮仍在。
- 修复 F8 鼠标透传时左键释放被本地选区处理提前吞掉的问题，新增 raw-mode 工具端到端检查。

两项补充可用性检查使用 `/tmp/cam-tui-usability-20261019.py`；实际终端录制与画面位于 `/tmp/cam-tui-usability-20261019/`。OSC 52 验证仅确认发出的文本与选区一致，并且 UI 不虚报系统剪贴板成功。

测试使用私有临时 Home/config/runtime，按创建的稳定 ID 清理 agent；没有对用户现有 agent 发送消息或修改系统 crontab，未停止用户服务。

## 性能样本

最终 `/tmp` 静态产物运行 `cam-tui --version`，10 个独立进程的中位耗时约 0.92 ms。这只是当前主机/文件系统的版本入口测量，不等同于 daemon 冷启动、远端建连或 AI 工具启动延迟。

## 仍需单独验收

- 物理跨机器 SSH、Ubuntu/其它 Linux 与 ARM64。
- 真实 Claude/Codex 登录、完整交互与 farm/LSF 启动。当前真实进程用例使用可回收 shell/cat 工具，不冒充 AI 工具验收。
- 用户终端的实际系统剪贴板与 OSC 52；headless PTY 与 helper 返回码不能代替跨 SSH 的系统粘贴验证。
- 完整状态增量订阅、原生 PTY/Ghostty、主动 forwarding 往返 probe、完整 cron 表达式及 loop owner 自动交接。

当前 clipboard/健康/unknown 反馈会明确显示能力与验证边界，不把请求写出当作完成。
