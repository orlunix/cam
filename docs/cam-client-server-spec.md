# CAM Client / Server 架构与多路径通信 Spec

日期：2026-10-18

状态：目标设计草案；当前先实现 Router，agent 管理随后迁移，实现尚未完成。

范围：术语、职责、连接、跨机器请求与响应、可靠性、旧组件归并及验收。

## 1. 目标与文档边界

CAM 的产品架构只有 **Client** 与 **Server** 两种角色。每台纳入管理的机器运行一个 CAM Server；多个 Client 可以同时连接多个 Server。Client 的连接与中继能力使两个 Server 之间可以存在多条通信路径。

`camc` 是 legacy CAM Client，也是迁移期保留的 fallback 工作方式。它当前承担的 agent 信息管理、本机执行和维护能力，需要逐步实现在 CAM Server 中。用户工作流随后逐步切到 CAM TUI、Desktop、Mobile；不以一次重写或立即停用 camc 为前提。

**当前阶段是 Router 先行。** 新增 CAM Server 先负责连接、路由、请求投递与响应关联。camc 现有的 agent 信息、状态、管理、runtime、monitor 和 launcher 保持原有工作方式；Desktop/Mobile 当前经 SSH 调用 camc 的路径也继续可用。Router 上线不以迁移这些能力为前置条件。

长期目标是把 camc 的 agent 信息与管理能力逐项实现到 CAM Server，CAM TUI/Desktop/Mobile 直接使用 Server API，最终退役 camc。camc 的 standalone 能力作为迁移期 fallback 保留；退役要等新流程、脚本兼容及恢复路径达到验收条件。

本 Spec 规定迁移后的契约，不宣称当前代码已经满足。语言、具体 socket 路径、端口和 wire 编码在实施阶段确定；不得因此改变这里的所有权、投递和去重语义。

既有文档的关系：

- [CAM Client Forwarding Spec](cam-client-forwarding-spec.md)：Client 专用转发通道、可达通告、请求与回程、恢复及逐项验收要求；属于本文 Router 阶段的 Client 侧契约。
- [camc spec](camc-spec.md)：legacy camc 当前行为与兼容约束。
- [Bridge Protocol v1](desktop/bridge-protocol-v1.md)、[camb 实现笔记](desktop/camb-bridge.md)：现有桥接协议与实现，迁移期通过适配器兼容。
- [Fleet Bridge 旧方案](desktop/fleet-bridge-plan.md)：现有 bridge 的背景；目标角色和职责以本文为准。
- [Server/TUI 早期计划](superpowers/plans/2026-10-14-cam-server-and-tui.md)：TUI、终端后端和历史交互的背景；涉及 camc 定位和跨机器职责的部分以本文为准。

## 2. 统一术语

| 名称 | 定义 | 实例边界 |
| --- | --- | --- |
| Node / 机器 | 纳入管理的一台机器；不能只用可变 hostname 或 UI 别名作为身份 | 一个稳定 `node_id` |
| CAM Server | 机器上的常驻服务；当前先做 Router，随后逐步承接 agent 信息与管理 API | 每个 Node 一个 active 实例 |
| CAM Client / 终端 | 用户使用的客户端，包括 Desktop、Mobile、TUI 和 legacy camc | 一个 Client 可连多个 Server，一个 Server 可连多个 Client |
| Hub / 中继 | Client 承担的跨 Server 转发能力 | 是 Client 的内部能力，不是第三种产品角色 |
| Tunnel / 通道 | 承载 Client–Server 通信的连接机制，例如 SSH 转发 | 一个 Server 可以同时拥有多条通道 |
| Agent | 当前由 legacy camc 管理、后续按批次迁到 Server 的工具会话 | 身份为 `(owner_node_id, agent_id)`，另标注管理后端 |
| Tool | Claude、Codex、Cursor、Pi 等实际进程 | 当前 runtime 不变，后续按能力迁入 Server |

Desktop 可以在应用后台内置中继。Mobile 的中继由其连接后台承担，浏览器不需要直接实现 SSH；多个页面可以共用同一个 Client 后台。路径数量按独立的连接/中继实例计算，不能按 UI 窗口数量计算。

`TunnelHub` 是当前 Python 实现的类名，`camb` 是现有辅助程序，`camc tunnel` 是现有命令组；三者都不定义新的架构角色。

## 3. 拓扑与不变式

```mermaid
flowchart LR
    CA[camc legacy / 本机请求方] -->|跨机通信| A[Node A: CAM Server Router]
    CA -->|原有本机操作| LA[原有 camc runtime / agents]
    A <-->|双向连接| C1[CAM Client 1: Desktop]
    A <-->|双向连接| C2[CAM Client 2: TUI / Mobile 后台]
    C1 <-->|双向连接| B[Node B: CAM Server Router]
    C2 <-->|双向连接| B
    B <-->|投递 / 回包| RB[已接入的消息接收端]
```

1. 同一 Node 不能因增加 Client、工作区或 agent 再启动一个 active CAM Server。第二个实例必须明确拒绝启动，或连接已有实例。
2. 新流程中 `camc / 新 Client → CAM Server` 是 API 依赖方向；新增 CAM Server 不启动 camc CLI，也不通过 SSH 执行远端 camc CLI。现有 Client 的 legacy SSH/camc 调用不在 Router 首版被强制替换。
3. Client 连接 Server，并通过已建立的双向连接承接转发。Server 可以在这些连接上发出请求，不要求 Server 能独立连到 Client 的监听地址。
4. 目标架构中，每个 agent 只有一个 owner Server。迁移期尚由 legacy camc 管理的 agent 必须明确标记其管理后端；Server 导入的只读信息不代表已经接管。任何时刻，每个 agent 的权威信息与控制操作都只能有一个管理后端。
5. Client 退出不停止 agent；一条通道断开不表示 agent dead。
6. 多条路径共享同一个业务请求身份，不能因重连、换 Client 或换 transport 重新生成 request ID。

本阶段只要求 `Server A → 一个 Client → Server B`，以及 Client 直接调用目标 Server。不引入多个 Client/Server 连锁转发；中继请求不能再交给另一个中继形成环路。

## 4. 职责

### 4.1 CAM Server：当前 Router 与后续后端

Router 首版负责：

- 提供本机通信入口与 Client 连接接口，登记身份、连接能力及可达目标。
- 管理所有 Client 连接和连接健康状态，选择路径、处理断线与重连。
- 接收本机调用方的跨 Node 请求，转发到合适的 Client，再交给目标 Node；跟踪请求并关联响应。
- 目标端校验信封与权限，按 request ID 去重，记录投递及返回结果；Router 状态与 agent 状态分开存储。
- 向已经接入的本机接收端或兼容 mailbox 交付消息。接收端负责业务处理，Server 不为此启动 camc CLI、tool 或新的 agent monitor。
- 提供连接、路由、请求查询及简短日志。尚无可处理业务的接收端时，明确返回不支持或不可达，不能把“已路由”当作“操作已完成”。

首版不读写 agent registry，不接管生命周期、tmux、monitor、环境、launcher 或 tool home，也不要求先实现 agent 列表/状态 API。现有本机操作继续走原有 camc；Router 故障不影响它们。

后续按能力迁入：

- agent 信息、配置、session/launch 绑定、状态、历史及恢复数据的管理。
- agent 生命周期和 runtime 服务，包括 tool、tmux/PTY、环境、launcher、隔离 home、输入和历史读取。
- 带序号的 agent 事件和快照 API；已迁移 agent 的权威状态由 Server 维护。

这些是后续阶段的验收目标。迁入 Server 的能力使用内部模块或共享库，不依赖 argparse、CLI 输出解析或嵌套 camc 命令。

### 4.2 CAM Client

Client forwarding 的必做清单、通道和验收以 [CAM Client Forwarding Spec](cam-client-forwarding-spec.md) 为准。每个 Server 使用专用双向 forwarding 通道，接收 Server 推送并维护请求回程；该通道与终端 attach/输出分开。

- 提供 Desktop、Mobile、TUI 或 CLI 交互，连接并展示多个 Server。
- 对已迁移能力，汇总 Server 快照和事件，显示 owner Node、更新时间及 stale/offline 状态；Router 首版不要求替换现有 camc 信息读取。
- 在获得授权时，把一个 Server 的请求转发给自己直接连接的目标 Server，并把响应返回请求来源。
- 发布自己当前直接连接的 Node 集合，供来源 Server 选择路径；不把从别处听到的路由再次宣告为直连。
- 负责 UI 布局、终端显示、PageUp/PageDown、历史 viewport、鼠标选区和用户本机剪贴板。UI 状态不成为 agent 运行状态的权威。

新 Server API 已覆盖的能力，由 Client 直接调用；其余保留现有 legacy 操作路径。纯中继不执行 camc CLI、不维护 agent 生命周期，也不因自身断线重新发起结果未知的修改操作。legacy fallback 按下节的管理后端边界保留。

### 4.3 Legacy camc、新 Client 与 fallback

- Router 首版保留 camc 的 legacy standalone 工作方式，只按需接入跨 Node 通信。后续已迁移的能力可以提供 camc API adapter；这不是 Router 上线的前置条件。
- CAM TUI、Desktop、Mobile 使用同一组 Server API，按已验证能力逐步切换。某一个新 Client 的完成不代表所有旧工作流已经迁移。
- 新 UI 不可用但 Server 正常时，已具备对应 API adapter 的 camc 可以作为备用入口；操作仍由同一个 Server 处理。
- 对仍由 legacy camc 管理的 agent，用户可继续使用其原有 standalone 操作；新 Server 暂未实现相应能力，不应使旧流程失效。
- 每个会话必须明确由 legacy runtime 或新 Server runtime 中的一方拥有，禁止同时写同一份 registry、monitor 或 tmux 控制状态。业务操作使用哪个后端必须在执行前确定。
- 不为新 Server 的兼容性要求用户安装或常驻一个独立 camc 接收进程。

### 4.4 Agent 信息管理的渐进迁移

本节属于 Router 之后的阶段。迁移包含信息读取、维护和执行配置，至少覆盖以下信息：

| 信息 | Server 的目标能力 |
| --- | --- |
| 身份与定位 | agent ID、名称、标签、工作区、owner Node，以及当前由 legacy 还是 Server 管理 |
| 工具与启动配置 | tool、tool directory、options、launcher、环境覆盖及隔离 home 策略；支持重启时复用已保存的配置 |
| 会话与运行位置 | tool session、launch 代次、tmux/PTY 关联、submit host、execution host 和 allocation 信息 |
| 运行状态 | alive/dead、idle/running、initializing/started、时间戳及退出或失败原因 |
| 历史与维护 | 历史/日志索引、查询和信息维护 API，以及 heal、归档、清理需要的记录 |

敏感配置保存在受保护的后端存储中；普通列表、状态 API 和默认日志只返回必要的元数据，不回传环境中的 secret。信息迁移要保留恢复操作所需的值，不得为了展示而丢掉它们。

| 阶段 | 信息与控制的权威来源 | Client 的工作方式 |
| --- | --- | --- |
| Legacy 基线 | camc 原有记录与 runtime | 旧 camc 正常使用；新的 Server 可以尚未部署 |
| Router 先行（当前阶段） | agent 信息与控制仍全部由原 camc 管理；Server 只维护通信记录 | 接入连接/路由/请求响应；其它流程保持不变 |
| Server 信息接入 | 对未迁移 agent，camc 仍是权威；Server 只读导入/索引并标注来源与更新时间 | 新 Client 先验证列表、状态、配置与历史查询，旧 camc 继续工作 |
| 按 agent 接管 | 被选中的 agent 及其权威信息和控制切到 Server；其余仍归 legacy | 新 Client 与 camc API adapter 操作 Server-owned agent；legacy 操作其余 agent |
| 新流程为主 | 已迁移能力由 Server 管理 | TUI/Desktop/Mobile 直接使用 Server API，camc 保留经验证的备用路径 |
| 退役 camc | 所需信息与管理能力已迁入 Server，兼容与恢复验收完成 | TUI/Desktop/Mobile 成为主流入口，替代旧 camc 工作流 |

Server 可直接读取或通过库适配 legacy 数据格式，不能通过运行 camc CLI 导入信息。只读导入不写回 legacy 记录，不增加第二个 monitor，也不重新启动已有 tool。信息与控制权转交时保留 agent 身份、必要配置及历史关联，并验证当前 launch；同一个 agent 不能在列表中变成两个可独立控制的副本。

### 4.5 Fallback 与回退规则

- Server 尚未接管的 agent，可以使用 legacy camc fallback；新 Client 缺少能力时明确显示支持范围，不伪装成操作成功。
- Server-owned agent 仍通过 Server 管理。Server 暂时不可用时，返回不可达或显示标明时间的只读缓存；不能仅凭断线就让 legacy camc 直接接管。
- 已提交而结果未知的操作，继续以原 request ID 查询或恢复；不能在超时后自动切到 legacy backend 再执行一次。
- 若需从 Server-owned 回退到 legacy，必须先确认原 owner 已释放控制权或被可靠隔离，再转交记录、配置与实际会话；交接后只能有一个活跃管理后端。保留可核对的交接结果。
- 不影响既有 standalone 工作流是迁移验收的一部分。旧能力只有在新路径、备用入口、信息兼容和回退都已验证后，才进入删除候选。

## 5. 身份、连接与可达性

| 字段 | 语义 |
| --- | --- |
| `node_id` | Node 的稳定身份；别名、IP、SSH endpoint 和 hostname 可作为连接信息，不能替代它 |
| `server_epoch` | 当前 Server 启动代次；重启后改变，重启本身不改变 `node_id` |
| `client_id` | 独立 Client 后台的稳定身份；不是浏览器 tab ID |
| `request_id` | 一个业务请求的全局唯一 ID；重试保持不变 |

Client–Server 建连时协商协议版本、角色、身份、认证及能力，随后交换可达信息和快照。协议不兼容或未获授权必须明确失败。

Server 为每个 Client 连接分别保存状态、代次、可达目标和最后更新时间。连接断开或通告过期后，该连接的路径立即失效；其他连接仍可使用。不得用单个 `reverse.pid`、一个全局 Hub 名称或一个连接状态文件代表所有 Client。

Server 单实例锁、socket、连接状态及活跃队列必须放节点本地；共享 NFS Home 不能让不同机器覆盖对方的 PID、epoch、端口或锁。持久身份配置和运行记录按 Node 隔离。

注册认证凭据与 endpoint 认证凭据必须语义分开。若注册请求携带 endpoint token，它必须与目标 endpoint 真正使用的 token 一致，不能把注册 Bearer token 默认当作 endpoint token。

## 6. 操作 API 与消息信封

以下是目标协议的逻辑结构，字段不依赖 SSH、WebSocket 或本机 socket 等具体 transport。现有 argv/JSON-line 协议可在边界适配，不作为 Server 内部执行接口。

```json
{
  "version": 1,
  "type": "request",
  "request_id": "7bb96032fd3e4ea99938a6d1e6067d74",
  "source": {"kind": "node", "id": "node-a"},
  "target": {"node_id": "node-b"},
  "operation": "test.echo",
  "params": {"text": "route test"},
  "deadline_at": "2026-10-18T21:00:30Z"
}
```

直接由 Client 发出的请求使用 `source.kind = "client"` 和其 `client_id`。认证后的连接必须约束来源身份；接收方不能无条件相信 payload 自报的来源。

```json
{
  "version": 1,
  "type": "response",
  "request_id": "7bb96032fd3e4ea99938a6d1e6067d74",
  "owner_node_id": "node-b",
  "server_epoch": "boot-b-2",
  "status": "ok",
  "result": {"text": "route test"}
}
```

- 已知的最终执行结果使用 `status = ok` 或 `error`。错误包含结构化 `error.code` 与 `error.message`；不能靠解析一段 CLI stderr 判断传输是否成功。
- 请求等待路径、排队或执行期间，可以返回 `pending` 确认；这不是最终错误，也不表示 agent 已经 started。
- 执行发生与否不能确认时返回 `unknown`，并允许通过原 request ID 查询；不能把它伪装成普通失败后自动重做。
- 路径选择、尝试次数和使用的 `client_id` 属于传输元数据，不能改变业务请求的来源、目标、操作、参数和 ID。
- Router 首版 API 覆盖请求提交/查询、连接与路由查询，以及接收端的注册/投递。信封中的业务 operation/payload 交给已注册接收端；示例的 `test.echo` 由验收接收端处理，Router 不把它转换成 camc 命令。
- `agent.list/get/start/stop/reboot/kill/remove`、`agent.capture/send/key`、`message.send` 等业务 API 在后续逐项迁移时提供；不能作为 Router 首版的必选能力。能力协商决定可用范围，未接入的操作返回 `UNSUPPORTED_OPERATION`。
- 后续迁移终端 attach/input/resize、历史读取和 agent 事件时，通过独立逻辑通道承载，避免大量输出阻塞管理请求。Router 首版仍需限制帧大小、队列和缓存；大输出分页/截断的契约随业务 API 一起验收。

## 7. 一次跨 Node 操作

```mermaid
sequenceDiagram
    participant CLI as camc / 来源 Client
    participant A as CAM Server A
    participant C as 中继 Client
    participant B as CAM Server B
    participant R as B 的已注册接收端
    CLI->>A: 操作请求，目标 B
    A->>A: 分配/保存 request ID，选择路径
    A->>C: 转发同一请求
    C->>B: 通过已有连接交付
    B->>B: 认证、授权、去重与投递记录
    B->>R: 交付原 operation/payload
    R-->>B: 处理结果
    B->>B: 保存最终结果
    B-->>C: 同 request ID 的响应
    C-->>A: 转发响应
    A-->>CLI: 关联原调用并返回
```

这是 Router 阶段的投递流程；接收端沿用已接入的业务处理方式或测试 handler，不要求迁移 agent runtime。若来源就是一个直接连接 B 的 Client，可省略 A 和中继跳转；本机消息直接交付对应接收端。

既有 `camc → legacy runtime → tool` 和 `Desktop/Mobile → SSH → camc` 不因部署 Router 而改变。后续某项业务 API 迁入 Server 后，该项才改为 `Client → Server API → 内部 runtime`。

Router 只解析并固定 `target.node_id`，不查询 agent registry 或解析 agent 名称。重试不能改换目标 Node。后续接入 agent 业务 API 时，由对应接收端或原生 API 固定 owner Node 与 agent 身份；名称或短 ID 有歧义时返回候选，不猜测、不广播执行。

目标结果已保存但原路径断开时，来源可以通过另一条路径以原 request ID 查询或重投同一个请求。B 返回已保存结果；不要求响应只能走已经失效的原路径。

## 8. 多路径路由

来源 Server 保存形如 `目标 node_id → 可用 Client 连接集合` 的路由信息。本机优先；跨 Node 请求只选择宣告可直接到达目标且获得授权的 Client。

1. 正常情况选择一条健康路径；第一版不并行向多条路径发送修改操作。
2. 传输失败后可以选择另一条健康路径，保留原请求身份；收到目标的最终业务错误后不能换路径再次执行业务操作。
3. 路由通告是带有效期的可达提示；目标 Server 仍需独立校验权限和请求。
4. Client 转发时核对目标确为自己的直连 Node；不再做递归路由，不把请求送回来源。
5. 没有路径时，来源 Server 保留可重试请求至其 deadline，并报告 `pending`。deadline 到达时，尚未开始执行的请求终结为 `ROUTE_UNAVAILABLE` 或 `DEADLINE_EXCEEDED`。

第一版选择简单、稳定、可解释的顺序；路径质量评分等优化不作为前置条件。路径变化不转移 agent ownership，也不改变 launcher 的执行位置。

## 9. 去重、结果与故障恢复

**目标 CAM Server 统一维护请求去重与结果记录。** Client 或来源 Server 的缓存只能加速，不能代替目标端记录；不同 Client 可能先后送达同一个请求。Router 阶段的记录覆盖投递给哪个接收端以及返回结果，业务副作用仍由接收端产生。

- 去重键为认证后的来源身份加 `request_id`；记录目标、operation 和 params 的摘要。同一键带不同内容必须返回 `REQUEST_CONFLICT`。
- 第一次交付前持久化请求/投递记录；同一请求处理期间再次到达时复用记录，已有结果时返回原结果。Router 不把接收端未确认的请求再次当作全新业务操作交付。
- 临时断线、路径不可用和 release/requeue 不是最终业务结果，不能进入终态结果缓存，更不能提前覆盖最终响应。
- 请求不能在简单 dequeue/打印后就失去恢复依据。来源保存未决请求，目标保存执行记录和结果；中继退出不应使请求与结果的唯一副本一起消失。
- 最终结果在来源确认接收前保持可恢复。去重记录覆盖协议允许的重试期限；清理结果后，过期请求仍必须被拒绝，不能当作新操作执行。
- Server 重启后恢复未完成记录，并向原接收端查询结果；只有后续接管 runtime 的阶段才由 Server 直接核验 agent。无法区分“尚未处理”和“已处理但未记录结果”时，标记 `unknown`，禁止盲目重放。
- 通信 deadline 限制尚未执行请求的等待时间。操作已被接受并开始执行后，调用方超时或断线不自动杀掉 agent，也不证明操作失败；调用方按原 ID 查询结果。

Router 单独上线不代表现有 camc 业务已经具备端到端幂等。接入修改操作前，接收端必须配合保留 request ID、查询处理结果或实现去重；无法确认交付结果时返回 `unknown`，不自动重新执行。验收接收端及后续原生 Server API 需要证明正常重试和多路径投递不产生重复副作用，不宣称所有故障下的 exactly-once。

## 10. Agent 状态与终端边界

Router 阶段不修改 agent 状态、monitor 或 attach 流程；下述状态模型由现有 camc 继续维护，后续管理能力迁入 Server 时沿用：

| 生命周期 | state | phase |
| --- | --- | --- |
| `alive` | `running` | `initializing` 或 `started` |
| `alive` | `idle` | `started` |
| `dead` | `stopped` / `failed` / `killed` | 无 |

请求的 pending/ok/error/unknown 与 agent 状态是两回事。连接 offline、列表 stale 或请求超时，不能直接把 agent 写成 dead。

后续实现 Server 的 `agent.start` / reboot 时，可以先返回新 agent/launch 身份和 initializing 阶段，让 UI 立即提供终端；接管管理权的 Server 必须护航到 started，或给出明确的 failed 原因。Router 首版不增加启动或 reboot 的执行职责。

owner Node、submit host 和 tool 实际 execution host 分别记录。LSF/launcher 把工具放到另一台机器执行，不自动改变现有管理后端；后续已迁移 agent 的 owner Server 也不因此改变。Router 连接不等于 runtime ownership。现有 tool resolve、环境、autorun、launcher 和隔离 home 策略在迁移中保持独立，不由中继改写。

UI 关闭或浏览历史不影响 tool；选区与 copy/paste 属于用户端。管理 API、终端会话和完整工具对话记录需要区分，不能把 capture 输出当成完整会话历史。

## 11. 访问与可观测性

- 本机 IPC 与远端通道都需绑定明确身份和授权范围。Client 仅能中继其获得授权的 Node/操作；目标 Server 始终重新校验，不能只相信中继的声明。
- Server 接受结构化信封与 payload；中继不把用户内容拼成 shell 命令。Router 只验证通信契约和权限；具体业务参数由接收端校验，自定义 tool launcher 仍由既有 runtime 处理。
- 日志简短记录 request ID、来源/目标 Node、所用 Client、路径切换原因、结果和耗时；token、完整环境值、凭据和敏感请求正文不进入默认日志。
- 每个通道分别展示 connected/disconnected、更新时间及错误原因。Router 首版可查询连接、路由和未决请求；agent 查询 API 随信息管理后续接入，避免把通信状态和 agent 状态混在一起。

## 12. 旧组件归并与删除边界

| 当前组件 | 目标归属 | 删除条件 |
| --- | --- | --- |
| camc 的参数解析、文本输出和脚本入口 | 当前保留原工作方式，后续过渡为 legacy adapter/fallback | 新 Client/API、脚本兼容、备用与恢复路径通过验收后完成退役 |
| camc 的 agent 信息管理、registry、runtime、monitor、launcher 等后端能力 | 逐步实现在 CAM Server 内部服务或共享库中 | 按能力和 agent 迁移；只删除已无 legacy fallback 依赖的重复实现，不因 Server 新增能力就立即删旧路径 |
| `camc tunnel` 的 endpoint、keeper、通道状态和请求队列 | CAM Server 连接与请求管理 | 等价路径通过验收后删除独立命令组及重复守护进程 |
| `camb` 的请求封装、serve、reverse maintainer | CAM Server 通信实现；必要的 legacy 协议 adapter | 旧部署和调用方迁移后删除独立程序及自动部署逻辑 |
| Python `TunnelHub` | 路由/结果跟踪归 Server；跨 Server 转发归 Client 连接层 | 对应职责迁移后删除独立类和 Hub 模式，不保留第二套 router |
| Desktop embedded hub 的跨机器连接与中继 | CAM Client 的后台连接层 | 保留必要能力，收口到统一 Server 协议；原 camc SSH 执行 adapter 随迁移删除 |

最终不需要独立的 Hub 产品、每台机器上的 camb 服务，或 Server 调用 CLI 的执行链。SSH tunnel、认证、队列、重连和去重作为内部能力继续存在。

迁移期间，协议 adapter 必须明确版本、能力及会话 owner；不能先删除旧路径再让用户承担不可用窗口。新旧 runtime 的权威写入状态和临时状态隔离；Server 可以按 §4.4 只读索引 legacy 信息。切换 ownership 必须验证已有会话、保存配置、回收责任和回退办法。

## 13. 分阶段验收

### 13.1 当前 Router 阶段

使用至少两个独立 Node（A、B）和两个独立中继 Client（C1、C2），两者都连接 A、B。只开两个 UI 页面、共用同一个后台，不能算双路径测试。

| 场景 | 必须观察到的结果 |
| --- | --- |
| 同 Node 启动第二个 Server | 明确拒绝或连接已有实例；只有一个 Router 实例 |
| 所有远端 Client 离线或 Router 停止 | 旧 camc 的本机管理和既有 Desktop/Mobile 操作仍按原方式工作 |
| A → C1 → B，C1 → B 直接调用 | 已注册验收接收端收到原消息，原 request ID 的响应返回来源 |
| C1 断开，C2 保持在线 | 同 ID 改走 C2；连接错误不污染 agent 状态 |
| 两条路径送达同一测试请求 | 验收接收端的处理计数为一次，返回同一个最终结果 |
| 接收端已处理但回包丢失 | 经 C2 查询得到原结果；不确定时返回 unknown，不再次交付业务 |
| 第一次暂不可达，随后恢复 | 请求实际重试并成功，不命中旧的临时失败终态缓存 |
| 注册成功后实际收发消息 | 认证、接收端交付和响应返回均成功；不能只验端口或注册成功 |
| 中继重启、来源重启、目标重启 | 分别验证队列/结果恢复；不确定执行返回 unknown |
| 同 ID 不同参数、过期请求、歧义目标、递归中继 | 明确拒绝，不执行副作用 |
| 目标接收端未注册/未实现操作 | 明确返回不支持或不可达，不启动 camc CLI 或 tool |
| 仅部署 Router | agent registry、状态、monitor、launcher 与 attach 原流程保持不变 |

### 13.2 后续 agent 信息与管理迁移

| 场景 | 必须观察到的结果 |
| --- | --- |
| 两个 Client 同时观察已迁移 agent | owner 与生命周期一致；各自历史 viewport 独立 |
| 已迁移的 start/reboot 出现确认或错误 | 护航到 started 或明确 failed，不停留在假就绪状态 |
| Server 只读导入 legacy agent 信息 | ID、配置、状态和历史关联可核对，标明来源/更新时间；不改旧记录，不新增控制进程 |
| 单个 agent 转交给 Server | 身份和必要配置保留；Client 列表中只有一个可控制对象；其他 legacy agent 不受影响 |
| 新 Client 故障，Server 正常 | 对应的 camc API adapter 可完成同一操作，仍由原 Server 执行 |
| Server 断线或修改请求结果未知 | 不自动用 legacy backend 重放；恢复后可按原 request ID 查询 |
| 显式回退某个 agent 到 legacy | 原控制权已释放或被可靠隔离，记录与会话可核对，只有一个管理后端 |

Router 验收以独立接收端验证路由与回包，并核对 legacy 流程未变化，不要求先迁移工具管理。后续接入真实业务 API 时，至少对 Claude、Codex 各跑一次受控启动、状态读取、消息/输入、stop/reboot 及回收。自动测试分别覆盖协议语义与底层 transport；临时 agent、测试 Server/连接和测试创建的 allocation 全部回收，复用的用户会话或 allocation 不属于测试回收范围。

当前的单元测试通过、端口可连、HTTP 注册成功，都不能代替上述端到端验收。未验证的平台和路径必须明确记录。

## 14. 实施顺序

1. **Router 先行（当前范围）**：建立单实例 Server、本机入口、Client 连接、路由、请求与响应记录；用独立接收端打通 A → C1 → B，再接 C2 验证切换、去重与故障恢复。其它现有流程不变。
2. **通信接入**：逐条接入需要跨 Node 通信的调用方和接收端。保留 camc 的原有 agent/runtime 以及 Desktop/Mobile 的 legacy 管理入口；不能通过让 Server exec camc 来补业务功能。
3. **Agent 信息接入**：实现 Server 的只读 agent 信息 API，核对 legacy 信息并标注来源与更新时间；当前信息权威与控制权仍由 legacy 保持。
4. **管理能力迁移**：逐项实现生命周期、状态、终端、环境、launcher、自动化等 Server 服务。所需能力与回退路径通过验收后，再按 agent 转交信息及对应管理权。
5. **新 Client 为主**：TUI/Desktop/Mobile 按能力直接使用 Server API；需要时提供临时 camc API adapter，camc standalone 继续承担未迁移部分的 fallback。
6. **清理与退役**：删除已无依赖的 camb、camc tunnel、TunnelHub 重复实现；所有必需能力、旧脚本兼容和恢复流程验证后，最终退役 camc，以 TUI/Desktop/Mobile 为主流入口。

实施前需要落定的协议细节是 wire 编码、地址发现/部署、认证凭据分发、API 能力版本、持久化布局与容量/保留期限。首版范围是一跳 Client 中继；跨多个 Client/中间 Server 的传递路由留作后续范围决定。这些选择不得放宽单实例、目标端去重和明确返回不确定结果的要求。
