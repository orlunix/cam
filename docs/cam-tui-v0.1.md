# CAM TUI v0.1

开发分支：`cam-tui`。这是可运行的第一版，完整目标仍见 [TUI Spec](cam-tui-terminal-spec.md) 与 [本地 daemon Spec](cam-client-daemon-spec.md)。

## 技术路线与当前实现

参考 Herdr 的 Rust/Tokio 后台与 Ratatui/Crossterm 交互分层，采用 Desktop 的四区布局：左上 New/Settings/Nodes，左下可筛选 agent 列表，右上 tab bar，右侧交互终端。

本仓库保留 Rust 1.71 基线，使用 Ratatui 0.24、Crossterm 0.27 和 VT100 解析器。Herdr 当前源码使用更新的 Ratatui 与 Ghostty；本版没有移植其源码或引入 Ghostty 构建链。后台仍用 tmux 托管已有工具，control mode 输出交给 TUI 解析，不嵌套显示 tmux attach 界面。

| 已实现 | 行为 |
| --- | --- |
| 本地 Rust `cam-clientd` | 每台机器/用户一份，多个 TUI 共用；退出 UI 不停止 daemon、Server 或 agent；普通状态同步默认 30 秒，修改操作立即刷新目标 Node，当前新启动的 agent 临时每 2 秒刷新，started/dead 或取消后结束 |
| Nodes | 配置新增/编辑/移除，连接/断开/重连；Unix socket 或长连接 SSH argv transport；认证 Node/token |
| Agent 信息 | daemon 通过显式授权的 Server API 定期同步本 Node 的非敏感元数据，汇总过滤；离线标 stale，保持最后状态 |
| Agent 操作 | 创建、状态、capture、发送 prompt、stop/kill/rm、reboot；调用 Server-owned SDK，不运行 camc CLI 派发业务 |
| 终端 | 多 tab 独立 VT 模型、颜色、UTF-8、输入、resize、PageUp/PageDown、滚轮、稳定选区和文本粘贴 |
| 多窗口 | 同一 agent 的输出可共享；每个终端一个输入/resize owner，其它视图只读；释放或超时后交接 |
| 定时任务 | 间隔与指定 Unix 时间一次性任务；agent prompt、本机 argv 命令；持久记录、编辑、暂停/删除和运行历史 |
| Engineering loop | 固定 Node/agent 注册；目标端重查 idle，持久绑定调度 owner；忙碌/离线延后，成功触发保留 60 秒活动窗口 |
| Forwarding | daemon 内置既有 Rust relay，独立于 tab；沿用统一 message 与路由规则 |
| 健康 | IPC、调度 tick、状态同步、已认证连接/心跳；转发端到端未验证时明确显示 unknown |

TUI 的普通 prompt 与 engineering continuation 使用已有 `agent.send`，工具回答显示在交互终端。跨 agent 的 mailbox/request/reply 继续由既有 Router 与 camc SDK 管理；不能把终端内容当作消息回复协议。

## 构建与试用

```sh
CARGO_TARGET_DIR=/tmp/cam-tui-build cargo build --locked \
  --manifest-path components/cam-router/Cargo.toml --workspace --bins
```

先用两个真实的临时 shell 试界面：

```sh
python3 components/cam-router/scripts/demo-tui.py --bin-dir /tmp/cam-tui-build/debug
```

demo 使用临时 Home、Server、daemon 和 agent；`Ctrl+Q` 退出后全部回收，不读写用户原有 agent。生产 Server/Client daemon 已运行时，单实例规则可能拒绝 demo，需使用正常 Nodes 连接流程，不会停止已有服务。

正常使用：

```sh
/path/to/cam-tui
# 默认配置：$XDG_CONFIG_HOME/cam-client/<hostname>.json 或 ~/.config/cam-client/<hostname>.json
# 默认本机状态：/var/tmp/cam-client-<uid>，可在配置中指定其它私有本地目录
/path/to/cam-tui daemon status
/path/to/cam-tui daemon stop
```

本机 daemon 自动启动；默认配置按主机区分，避免共享 Home 的不同客户端共用身份。显式 `--config` 也应为每台客户端使用独立的 `client_id` 与凭据；不能把同一配置文件复制到多台机器后当作多个独立 Client。Server 需要按 [Router 使用说明](../components/cam-router/README.md) 独立运行。Server 的 Client grant 必须显式加入 `"management": true`，TUI Client ID 与 token 必须匹配。旧 forwarding-only grant 默认没有管理权限；不要把所有中继凭据升级为管理凭据。

Server Client grant 示例：

```json
{"client_id":"my-tui","token_file":"/private/my-tui.token","source_nodes":[],"management":true}
```

Client 配置示例：

```json
{
  "client_id": "my-tui",
  "runtime_dir": "/var/tmp/my-cam-client",
  "endpoints": [
    {"node_id":"actual-server-node-id","token_file":"/private/my-tui.token","socket":"/private/server/server.sock"}
  ]
}
```

远端 Node 将 socket 替换为长连接 command，例如 `['ssh','-T','host','/path/to/cam-server','connect','--socket','/private/server/server.sock']`（实际配置使用 JSON 双引号）。服务器进程和 token 仍各自独立配置。

## 操作

- 点击 agent 打开/切换 tab；名称右侧 `…` 或右键打开菜单，提供 Settings、Stop、Remove，其余命令在 More actions。Settings 可改名、设置逗号分隔的 tags；保存到所属 Server，不重启工具，已有 tab 名称也会更新。tags 支持字母、数字、下划线和短横线，可清空。New 选择 Node、工具与绝对工作目录，使用原有 launcher、autorun 和隔离 home 策略。
- Filters 与 Agents 是独立区域，滚轮在列表区域才滚动列表。agent 名称统一突出显示，长名称按终端宽度截断；下面一行弱化显示，左侧 `tool · status`，右侧 node 简称；状态点保留颜色。Node 有自定义简称时优先显示简称，否则将长主机名缩成 `pdx-046` 等短名。Nodes → Edit → Name (optional) 可保存简称，包括包含点号的名称；完整 Server ID 与路由不变。
- New、发送 prompt 和任务表单点击 Node 后选择机器，不需要复制内部 ID；Esc 返回原表单。Nodes → Details 可查看已有连接的完整 Server ID。旧连接 ID 与路由保持兼容；新增旧式连接的配对设置仍保留 Server ID 字段。
- New/run 请求交给 tmux 后即返回。qsub 在 pane 中自行等待 dispatch，排队期间保持 alive/running/initializing，不消耗 20 秒或 600 秒工具启动计时；auto 和自定义前缀都在目标端真正执行工具时才启动计时。stop/kill/rm 仍可取消等待。
- 启动欢迎页提供带颜色的操作引导；`F4` 或右上 Help 随时重开，PageUp/PageDown 浏览，Esc 关闭。包含历史、复制粘贴、退出、筛选与常用操作说明。
- `F1` New，`F2` Nodes，`F3` 全局 Settings。左侧 Node／Tool／Status 下拉菜单可组合筛选，支持鼠标或上下键＋Enter；搜索框可叠加筛选名称、ID、Node 简称或 `#tag`，Clear filters 清除全部条件。较矮窗口通过同名 Filters 按钮打开筛选面板。表单用 Tab 切换，最后一项 Enter 或点击 Save 提交，Esc 关闭。
- PageUp/PageDown、滚轮浏览历史；拖选后选区保留。End 或 History 提示返回实时画面。
- 全屏 TUI 显式启用配色与反显选区，即使父进程设置了 `NO_COLOR`，选区仍可见。
- Ctrl+C 有选区时复制，无选区时发送中断。松开鼠标保留 CAM 选区，不自动复制；在选区内右键也可复制。外层终端的右键菜单不认识 CAM 选区，其 Copy 可能仍为灰色。
- Linux 原生剪贴板使用 PATH 或 cam-tui 同目录中的 `wl-copy`/`wl-paste`、`xclip`/`xsel`；远端 Node 不需要这些依赖。原生剪贴板写入成功才显示 Copied；不可用时明确报错，默认不再发送可能显示成乱码的 OSC 52。
- 已确认外层终端支持时，可显式用 `CAM_TUI_CLIPBOARD=osc52 cam-tui` 允许终端剪贴板回退。该路径仅提示请求已发送，不能确认系统剪贴板已写入；不支持该协议的终端请使用原生剪贴板。
- 使用外层终端 Paste，或本地剪贴板可用时 Ctrl+V；不自动补 Enter。目标未启用 bracketed paste 时拒绝多行粘贴，避免直接执行多行 shell 内容。
- `F8` 显式切换工具鼠标透传；默认鼠标归 CAM。输入和 resize 由当前 writer 视图控制。
- Settings 管理任务/loop，选中后可 Edit；首版 schedule 输入正整数秒，或 `at UNIX_SECONDS`。编辑保留任务 ID、历史及暂停状态；正在执行时需等待结束再编辑。任务结果未知会保留 unknown，不自动重跑。
- `Ctrl+Q` 退出 UI 并释放输入权；正常模式 daemon 继续工作。需要停止后台时执行 `cam-tui daemon stop`。

## 第一版边界

- Agent 状态当前采用 Server 受授权快照轮询，尚不是增量事件订阅；legacy runtime/monitor 仍负责权威状态，没有迁移为 Rust 原生 monitor。
- 使用 tmux control mode，已覆盖本机 tmux 2.7；旧版缺少 bracketed-paste 状态格式时，attach 会进行有超时的只读 PTY 状态探测并回收 observer。探测失败时拒绝多行粘贴。尚无 Herdr 的原生 PTY/Ghostty、图像协议或完整现代终端协议覆盖。
- Server 当前每个 agent 允许一个管理 Client 的 control attachment，同 daemon 的多个视图共享。不同 Client 的只读 attach 与主动抢占尚未实现。
- 新 attach 获取至多 2000 行 tmux 历史；daemon 输出缓存 1 MiB，冻结视图的待显示输出有上限。检测缺口/超限会明确要求重同步，不保证无限历史。
- 本机 X11/xclip 的真实剪贴板已单独验证；OSC 52 与其它外层终端/SSH 的剪贴板支持仍需分别验收，headless PTY 测试不能代替它们。
- 暂不支持每日/完整 cron 表达式、自动目标完成判定、自动重启 dead agent、跨 daemon 自动转移 loop owner。当前 legacy monitor 与非 Server 输入之间仍有原有竞态；不声称实现全局原子 idle gate。
- 转发健康检查目前依赖真实认证连接、心跳及路由状态；未增加生产端到端主动 probe。没有验证证据时显示 unknown。
- 本机命令保存退出结果，stdout/stderr 暂不提供完整日志浏览。任意远端 shell 不属于管理 API。
- TUI 后端 SDK 兼容仍需要 Python/tmux/tool；Rust UI/Router 静态构建不意味着已有 camc 后端也已重写为 Rust。

## 验证

```sh
cargo test --locked --manifest-path components/cam-router/Cargo.toml --workspace
cargo build --locked --manifest-path components/cam-router/Cargo.toml \
  --workspace --bins --examples --features cam-server/test-harness
CAM_ROUTER_BIN_DIR=/path/to/debug python3 -m unittest discover \
  -s components/cam-router/tests -v
PYTHONPATH=src python3 -m pytest tests/test_cam_server_management.py tests/test_cam_server_protocol.py -q
```

UI 验收测试另需安装 `components/cam-router/tests/requirements.txt` 中的开发依赖。原生剪贴板用例还需显式设置真实 `DISPLAY`，必要时用 `CAM_TUI_E2E_CLIPBOARD_BIN_DIR` 指定本机 helper 目录；缺失时该用例会跳过，不等于通过。

真实进程测试使用隔离 Home、生产协议、独立 Server/daemon、tmux 和实际 PTY；不会操作现有用户 agent。跨物理机器、完整 Claude/Codex/Herdr 对照交互与系统剪贴板验收需单独记录，不能由这些测试代替。
