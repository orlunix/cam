# CAM TUI：界面、Agent/Node 操作与终端交互

日期：2026-10-19。开发分支：`cam-tui`。

状态：Rust TUI、本地 Client daemon 与 tmux control 终端通道已有 v0.1 实现；状态目前通过受授权快照轮询同步，增量订阅与完整目标能力尚待实现。实际范围、试用和限制见 [CAM TUI v0.1](cam-tui-v0.1.md)。以下保留完整设计与验收目标。
本文补充 [Client / Server Spec](cam-client-server-spec.md) 与
[Server / TUI 早期计划](superpowers/plans/2026-10-14-cam-server-and-tui.md)。

## 1. 用户目标

- Herdr 风格的 workspace/agent 侧栏、多终端 tab，以及可直接输入的终端区域。
- 管理、切换、历史浏览、选择和复制粘贴主要使用鼠标；键盘用于输入及工具快捷键。
- PageUp/PageDown、鼠标滚轮、选区与 copy/paste 由 CAM 接管，用户无需进入 tmux copy mode 或记忆 tmux 前缀键。
- 每个 tab 是可交互终端，支持 Claude、Codex、普通 shell 等程序，而不只是 capture 文本窗口。
- tmux 可以继续作为后台托管方式；其上方的终端模型与 UI 承担显示和交互。

### 1.1 主界面布局

按用户确认的四区布局实现：

```text
┌──────────────────────┬──────────────────────────────────────┐
│ New  Settings  Nodes │ Agent A │ Agent B │ Agent C           │
├──────────────────────┼──────────────────────────────────────┤
│ Filter agents…       │                                      │
│                      │                                      │
│ ● Agent A            │     当前 tab 的 agent 交互终端       │
│ ● Agent B            │                                      │
│ ○ Agent C            │     输入 / 历史 / 选区 / 复制粘贴    │
│                      │                                      │
└──────────────────────┴──────────────────────────────────────┘
```

| 区域 | 内容与职责 |
| --- | --- |
| 左上 Command | 固定的 `New / Settings / Nodes` 入口；可点击，键盘可聚焦 |
| 左下 Agent list | 显示已连接 Nodes 的 agent，支持按名称、ID、Node、状态筛选；显示所属 Node，避免跨机器同名混淆 |
| 右上 Tab bar | 已打开的 agent/终端标签；显示当前选中项，点击切换；后台输出不抢焦点 |
| 右侧 Main | 当前 tab 的完整交互终端，接管 PageUp/PageDown、滚轮、选区与复制粘贴 |

导航和操作约定：

- `New` 选择目标 Node、工具和工作目录，再通过后端既有启动策略创建 agent；已有 agent 的打开入口是左侧列表。
- `Settings` 管理客户端偏好；`Nodes` 管理本客户端连接的多个 CAM Server。打开这些面板不重建后台 agent 或已有终端。
- 点击左侧 agent，激活其默认交互 tab；尚未打开时创建视图，已打开时复用，避免重复标签。一个 agent 的其它终端可有独立 tab，均遵守第 3 节的身份与状态边界。
- 点击 tab 后，左侧同步标记对应 agent；列表筛选只影响列表可见项，不关闭 tab、不停止 agent，也不切换当前终端。
- 关闭 tab 只关闭该客户端视图，不代表停止 agent；stop/kill/rm 使用独立、明确的操作。
- 左栏宽度与右侧终端区域相互独立；布局变化只 resize 当前可见终端，保持其它 tab 的历史和选区。

### 1.2 本地常驻后台

TUI 通过本地 Rust Client daemon（暂称 `cam-clientd`）管理跨 Node 连接、agent list/status、定时任务、engineering loop、forwarding 与健康检查。多个窗口共用一个 daemon；关闭 TUI 后这些后台工作继续运行。它属于 Client，不是另一个 CAM Server，也不接管远端 agent 的权威状态或 monitor。

职责、注册/调度规则与验收见 [CAM Client 本地守护进程 Spec](cam-client-daemon-spec.md)。终端显示、输入 trap、viewport、选区和剪贴板仍在 TUI。

## 2. 已有 Desktop 参考

本次只读检查 Desktop，未修改其实现，也不把源码检查当作打包客户端的交互验收。

| 能力 | 当前实现位置 | TUI 借鉴点 |
| --- | --- | --- |
| 统一输入接管 | [terminal-trap.js](../web/js/shared/terminal-trap.js) | paging、wheel、copy/paste 和右键由一个入口处理，避免重复消费或向工具泄露控制字符 |
| 终端实例与切换 | [agent-console.js](../web/js/desktop/agent-console.js)，`terminalSessions`、`createTerminalEntry`、`hideTerminalEntries` | 切换可见终端与关闭连接是不同操作；保留终端状态和历史 |
| 历史跟随 | 同文件 `terminalIsAtBottom`、`setupTerminalEvents` | 写入前判断是否位于底部；用户在历史中时不强制滚到底部 |
| 初次 attach 的历史 | 同文件 `startTerminalHistoryFetch`、`settleTerminalHistory` | 历史与实时流需要顺序边界，不能随网络到达顺序交错拼接 |
| window 标签与新增 | 同文件 `renderTerminalTabs`，以及 [main.cjs](../apps/cam-desktop/electron/main.cjs) 的 `termSelectWindow`、`termCreateWindow` | 用户点击 tab 和 `+`，底层窗口操作由后端封装 |
| 剪贴板 | 同文件 `filesReadClipboardText`、`filesWriteClipboardText`，及 [preload.cjs](../apps/cam-desktop/electron/preload.cjs) | 剪贴板属于用户所在客户端，与远程 agent 分离 |
| 终端数据与尺寸 | [ssh-transport.cjs](../apps/cam-desktop/electron/ssh-transport.cjs)、`main.cjs` ready gate | 保序输出、流式解码、正确初始尺寸、resize 与连接代次 |

Desktop 使用 Electron/xterm.js；TUI 使用 Rust 终端控件和解析器。复用的是行为、状态边界和验收场景，不直接移植 DOM/Electron 代码。

## 3. tab 的状态边界

每个 tab 明确绑定 owner Node、agent 和稳定的 terminal ID。tmux window/pane 是后端映射，不以可变的 window index 充当跨重连身份。

每个 tab 独立保存终端模型、历史 viewport、选区、连接状态和输出游标：

- 点击切换 tab 不断开连接、不重启工具、不清空历史，也不改变其它 tab 的尺寸。
- 新输出只更新所属 tab；后台 tab 的输出不能抢焦点。
- 已有 tab 的选区和滚动位置在切换后保持；空间不足时采用明确的有界缓存与恢复策略。
- 关闭 tab 默认只关闭客户端视图/订阅，不等于 stop/kill/rm agent。停止工具是独立操作。
- `+` 通过后端能力建立新的终端；工具启动仍遵守已有 launcher/环境策略。
- 多个 Client 同时查看时，各自浏览历史；写入/resize 的所有权由 Server 明确管理。

Desktop 当前是每个 agent 一个 xterm 实例，内部 window 标签切换该 attach 连接的 tmux 窗口。TUI 的每-tab 历史和选区隔离需要单独实现，不能假定显示多个 tab 按钮就已完成。

## 4. 输入接管契约

| 操作 | CAM 行为 | 向工具发送 |
| --- | --- | --- |
| 点击侧栏或 tab | 切换焦点、显示对应终端 | 否 |
| PageUp/PageDown | 按页移动当前 tab 的历史 viewport | 否 |
| 鼠标滚轮 | 滚动当前 tab，暂停跟随新输出 | 否 |
| 拖选文字 | 显示高亮；松开后保留，直到明确清除 | 否 |
| Copy | 提取选区中的实际文字，交给客户端剪贴板适配器 | 否 |
| Paste | 交付粘贴文本；按工具的 bracketed-paste 模式编码 | 是，但不自动附加 Enter |
| Ctrl+C，有选区 | 复制选区 | 否 |
| Ctrl+C，无选区 | 保持工具的中断语义 | 是 |
| 返回实时画面 | 回到当前输出底部，恢复跟随 | 否 |
| 普通文字与未占用快捷键 | 交给当前聚焦终端 | 是 |

鼠标默认由 CAM 管理。需要操作程序内部鼠标界面时，提供明确的透传模式；坐标相对终端内容区域转换，不能将侧栏坐标交给程序。同一个事件只能被一层消费。

右键行为可参考 Desktop 的“有选区复制，无选区粘贴”，首版须清楚展示该约定；粘贴不自动执行内容。

## 5. tmux 上方的终端模型

```text
tmux / PTY 后端
    ↕ Server 交互终端通道
终端模型：屏幕、光标、模式、历史、输出序号
    ↕
CAM TUI：多 tab、鼠标、viewport、选区与剪贴板
```

- 解析终端协议，包括颜色、光标移动、覆盖/清屏、换行、正常屏幕和备用屏幕；不能用字符串追加模拟屏幕。
- UTF-8 和 escape sequence 可以跨数据帧，解析状态不能按帧重置。
- 屏幕内容与可浏览历史分开；重绘不能被当作一轮新的对话追加。
- 正确处理中文宽字符、换行重排、跨行/跨页选区；Copy 不包含终端控制序列。
- 浏览历史时新输出继续接收，不改变 viewport 或选区；提供新输出提示和返回底部入口。
- 初次 attach/重连明确区分快照与后续输出，并有版本/序号边界；检测缺口后重新同步，而非重复追加。
- 输出有界缓存及背压，不让一个后台 tab 阻塞其它终端或管理操作。

Desktop 的 `sanitizeTerminalStream` 通过过滤备用屏幕切换来保留 xterm 的正常缓冲历史。这是特定客户端的兼容方案。通用 TUI 要保留正常/备用屏幕语义并提供独立历史视图，不能无条件删掉这些序列，否则可能改变 vim、less 和其它 TUI 的显示行为。

## 6. 剪贴板与准确的成功提示

剪贴板操作发生在 CAM TUI 所在客户端，不能用远端 tmux buffer 代替用户系统剪贴板。

- 本机剪贴板接口确认写入成功后，才显示“已复制”。
- 通过 OSC 52 发送且没有完成确认时，提示“已向终端发送复制请求”，不能冒充已确认写入。
- 不支持/写入失败时明确提示，保留选区供手动复制。
- Paste 使用本机可用接口或外层终端交付的粘贴事件；首版聚焦文本，文件/图片另行设计。
- SSH 后运行 TUI 的场景必须在真实用户终端验证鼠标、粘贴与 OSC 52 能力，不承诺任意终端软件都能提供同等体验。

已发现的 Desktop 参考限制：`terminal-trap.js` 的 `copySelection` 没有等待剪贴板写入结果便显示 Copied；`agent-console.js` 的桥接回调也丢弃了返回 Promise。TUI 实现必须采用上面的结果语义，不能原样照搬。此处只是记录源码发现，未修改 Desktop。

## 7. 交互终端通道与原型验收

Rust Server 最初实现 Router 与 agent-specific 操作接入；v0.1 新增显式授权的 tmux control 终端输出、输入、resize 和初始历史通道。新增能力需独立协商，不能把已有 capture/send/key 组合当作完整终端，或绕过当前白名单使用任意命令。完整协议与恢复目标仍按本节验收。

终端通道需要明确 attach/detach、输出订阅、输入、resize、历史读取、连接代次和恢复游标。它与普通管理消息分离，不给每次按键创建一条持久业务消息；断线不盲目重发执行结果未知的输入。

先验证一个 Node、至少两个 tab：

1. 一个真实 agent 终端和一个 shell，点击切换，均可正常输入。
2. PageUp/PageDown、滚轮浏览历史；另一 tab 持续输出也不抢焦点、不拉回底部。
3. 拖选中文与多行文字，松开及切换 tab 后保留选区，实际粘贴验证系统剪贴板内容。
4. 粘贴多行内容不自动执行；Ctrl+C 无选区时能中断工具。
5. 正常/备用屏幕切换、窗口 resize 后显示正确，两个 tab 的历史不混合。
6. 断线重连恢复正确终端；关闭 TUI 不停止后台 agent。
7. 使用临时工作区和测试 agent，完成后回收；通过后再扩展到多 Node 与多 Client。

## 8. 必须实现的运行功能

CAM TUI 不只是终端布局，还必须接入以下能力。状态与业务所有权遵守 [Client / Server Spec](cam-client-server-spec.md)；转发遵守 [Client Forwarding Spec](cam-client-forwarding-spec.md)。下表中的客户端后台工作由本地 daemon 运行，TUI 提供操作与展示。

| 功能 | Server/后端职责 | CAM TUI 职责 |
| --- | --- | --- |
| Agent loop：status update / monitor | 提供权威状态、快照与更新事件；最终统一调度已接管 agent 的监控 | 后台同步状态，更新列表、tab 和详情，不自行扫描进程或 tmux |
| Agent operation | 对具体 agent 执行操作、检查能力与权限、去重、保存进度与结果 | 提供操作入口，绑定明确 Node/agent，显示进度、结果与错误 |
| Nodes operation | 提供真实 Node 身份、Server 能力、连接/路由诊断 | 管理本客户端的 Node 配置、连接与断线重连，展示各 Node 状态 |
| Forwarding | 两端负责持久消息、选路、去重与业务结果关联 | 后台维护专用转发通道，按目标 Node 转发原消息，不等待业务回复 |
| 定时任务与 engineering loop | 核验目标状态、权限与调度权，执行已开放的 agent 操作 | daemon 持久注册并调度；TUI 管理计划、查看运行与延后原因 |
| 健康检查 | 提供可协商的连接/路由、监控与探测能力 | daemon 检查 tunnel alive、forwarding、状态同步、调度/loop；TUI 分层显示证据、时间与原因 |

### 8.1 Agent loop：状态更新与监控

这里的 loop 指持续状态同步与监控；定时触发工程任务的 engineering loop 另见第 8.5 节，两者都需要实现。

- daemon 连接各 Node 时读取 agent 快照，随后订阅带序号的增量事件；重连、Server 代次变化或检测到序号缺口时重新同步。各 TUI 订阅本地 daemon，不重复轮询所有 Node。
- Agent 缓存以 `(owner_node_id, agent_id)` 为键，与 forwarding 的名称/ID 定位共用，不再建立另一份权威 registry。更新包含来源、版本/时间和新鲜度。
- Agent 状态沿用简化模型：`alive` 下为 `idle / running`，启动中的 `running` 标注 `initializing / started`；`dead` 下为 `stopped / failed / killed`。不引入 completed、planning、editing 等工作阶段。
- Node 断线、缓存过期与 agent 状态分开显示；失联不能将所有 agent 改成 dead，也不能继续把缓存标为实时状态。
- idle/running 使用后端监控判断；TUI 不凭终端暂时没有输出判断 idle。请求后的 running 保持策略由后端统一处理，各 Client 显示一致。
- run/reboot 的受理或 initializing 不等于 started。启动任务必须持续跟进到 started，或给出明确的失败/取消原因；关闭 tab 不取消后端启动任务。
- 迁移期 legacy agent 继续由原 monitor 负责，Server 只读接入其信息；原生 Server 接管后才使用集中监控。同一 agent 不启动两套 monitor，也不由每个 TUI 各自监控。
- Agent 快照/事件是需要新增并协商的管理能力，不把现有 agent-specific `status` 变成空目标查询，也不开放被排除的远端 `list` 业务命令。

### 8.2 Agent operation

首版操作范围与已有通信白名单一致：`msg / send / capture / run / stop / rm / kill / reboot / key / status`。交互终端的 attach/detach/input/resize 使用第 7 节的独立通道。

- 每次操作绑定明确的 owner Node 和 agent ID；New/run 预分配新 ID。点击之后的列表筛选、焦点或选中项变化不能改变已提交的目标。
- 创建、停止、删除、重启等实际语义沿用对应管理后端；reboot 保留 agent ID，使用已保存的启动配置重新启动，不自动添加 resume。
- 启动仍遵守 launcher、autorun、环境、tool resolve 与隔离 home 策略；TUI 不另外拼装一套绕过后端的 tool 命令。
- 明确区分 accepted、执行进度、最终结果、错误和结果未知。capture/status 等返回内容在 TUI 可查看，错误不能冒充空结果。
- 结果未知时使用原操作 ID 查询或恢复，不因超时、重连或重复点击自动创建第二次副作用。
- 已接入的 SDK/backend 可复用；不支持或未授权的能力明确显示，不能通过任意远端 shell/camc CLI 兜底执行。

### 8.3 Nodes operation

`Nodes` 面板至少支持添加、编辑和移除连接配置，以及 connect、disconnect、reconnect；展示 Node 身份/别名、连接状态、Server 能力、可达目标和最近错误。

- Node 以 Server 验证的稳定 ID 标识，SSH 地址和别名只是连接信息；同一 Node 的多条路径不能变成多个独立 agent 副本。
- 一个 TUI 可连接多个 Server，一个 Server 可连接多个 Client。连接一个 Node 不影响其它 Node 的终端和管理操作。
- disconnect/移除本地 Node 配置只撤回本 Client 的连接与路径，不关闭 Server、不停止其 agent。
- 配置连接不代表成功建连；认证/握手完成后才标记 ready 并宣告路径可用。连接错误可见，并提供手动重试。
- 此处的 Nodes operation 是客户端连接管理，不代表开放任意 Node 级远端 exec、批量 agent 控制或远程系统管理。

### 8.4 Forwarding

复用现有 Rust Client forwarding 组件，将其放入本地 Client daemon，不新增 Hub 产品或第二套路由规则；TUI 退出后 daemon 仍保持转发。

- 每个已连接 Server 保持专用双向 forwarding 通道，与终端输出和状态订阅隔离；tab 切换、浏览历史或关闭交互视图不暂停转发。
- 通告真实、已认证且获准中继的直连 Node；断开立即撤回，定期刷新，旧代次事件不能覆盖新连接。一个 Server 可经多个 Client 到达同一目标。
- 根据消息的固定 `target.node_id` 转发；保留 ID、地址、operation、payload 和 `reply_to`。Agent 缓存仅用于提交前定位，不改写已构造的消息。
- request 和 response 都使用同一普通 message 入口；回复是带 `reply_to` 的新 message，可以经过另一 Client。纯中继不保存业务回程表，不独占连接等待结果。
- 转发任务与 TUI 本身发起操作的结果展示分离；由来源 Server 保存和交付业务结果，不从 agent 终端画面解析回复。
- 慢目标使用有界队列和背压，不能阻塞其它 Node 或 UI。明确区分未转发与结果未知，由两端 Server 决定恢复和去重。
- `Nodes` 可查看通道、路由、排队、最后错误与耗时；默认日志只记录必要元数据，不记录消息正文或凭据。

### 8.5 定时任务与 engineering loop

本地 daemon 提供近似 camc cron 的定期/一次性任务，并可给指定 Node/agent 注册 engineering loop。持续更新所有已知 agent 的 status，但只对已注册目标触发 continuation prompt；busy/initializing/stale/offline 时延后，不中断工作、不堆积提示。Goal 是否达成依据明确的 Verify 证据，不能由 idle 推断。

注册记录、触发去重、多 Client 调度权、管理入口及完整验收见 [本地守护进程 Spec](cam-client-daemon-spec.md)。本轮不自动迁移或双跑现有 camc cron/loop。

### 8.6 健康检查

本地 daemon 统一检查自身、tunnel/Server 连接、按需启用的 forwarding、agent 状态同步、调度器、engineering loop 与已打开终端。进程存在、连接可响应和功能端到端可用分别验证；过期/缺少能力显示 stale/unknown 或不适用，不误报健康。`Nodes` 与 `Settings` 提供状态和按需重查，检查不向用户 agent 注入命令、不改变 agent 生命周期。

检查证据、主动 probe、恢复边界和验收见 [本地守护进程 Spec](cam-client-daemon-spec.md#6-分层健康检查)。

## 9. 实施顺序与完成边界

在现有 Rust workspace 中增加 `cam-tui` 与本地 Client daemon，复用共享协议和 forwarding 组件，不另起一个独立 Server 或 Hub。

1. 建立本地 daemon、四区布局、Nodes 连接管理和后台 forwarding；验证两个 TUI 共用一个 daemon，关闭窗口后转发继续。
2. 增加 Server agent 快照/事件能力，接入 daemon 状态缓存、Agent loop、筛选和状态展示；先只读接入 legacy 信息，验证断线与重同步。
3. 接入 agent operation 和交互 tab，验证真实 run/reboot 护航与失败反馈，以及操作只作用于固定目标。
4. 完成终端模型、历史、鼠标、选区、复制粘贴及重连；通过第 7 节的真实终端验收。
5. 完成持久定时任务与 engineering loop 注册/调度，以及分层健康检查；通过 idle gate、调度权、重启、去重与故障注入验收。
6. 通过多 Node、多 Client、双向并发与换路径回复验收；清理全部临时 agent、进程、连接与 allocation。

最初规划时的基础与目标（v0.1 实际完成范围见上面的版本说明）：

| 项目 | 已有基础 | 尚需完成 |
| --- | --- | --- |
| 本地 Client daemon | forwarding 参考组件、camc cron/loop 行为参考 | 独立常驻进程、本机 IPC、持久调度与多窗口共享 |
| 四区界面与交互终端 | Desktop 源码参考、本 Spec | Rust TUI、终端流协议、历史和剪贴板验收 |
| Agent loop | legacy runtime/monitor | Server 快照/事件 API、TUI 订阅与集中监控的受控迁移 |
| Agent operation | Server 原生 SDK 适配的十项操作 | TUI 操作入口、进度与结果展示、真实工具验收 |
| Nodes operation | 参考 Client 的多 endpoint 连接/重连 | TUI 配置管理、状态及诊断面板 |
| Forwarding | Rust Router 与 headless Client 参考组件 | 本地 daemon 集成及多 Client 验收 |
| 定时任务与 engineering loop | camc 的现有调度、idle gate 与 Goal/Checklist/Verify 约定 | daemon 注册/调度、Server 状态与调度权检查、真实 agent 验收 |
| 健康检查 | Router/参考 Client 的连接、路由与错误记录 | daemon 统一检查、协议探测能力、TUI 展示与故障验收 |

Spec 和参考组件完成不等于 CAM TUI 已实现；上述功能及真实交互验收都通过后，才能报告 TUI 可用。
