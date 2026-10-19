# CAM Client Forwarding Spec

日期：2026-10-19

状态：设计草案；已按统一 message 路由简化，实现与验收尚未完成。

范围：Desktop、TUI、Mobile 连接后台的专用 forwarding 通道。角色、Server 单实例和 Router 先行原则以 [CAM Client / Server Spec](cam-client-server-spec.md) 为准。

## 1. 核心规则

**request 与 response 都是一条普通 message，使用同一套发送、路由和投递流程。** 回复具有新的 `message_id`，通过 `reply_to` 关联被回复的消息。Client 对两者执行相同的转发，不等待业务结果，也不维护业务回程表。首版通信操作白名单为 msg、send、capture、run、stop、rm、kill、reboot、key、status，全部面向具体 agent，目标操作的既有本机语义不变。

```text
请求：camc A → Server A → Client C1 → Server B → agent B
回复：camc B → Server B → Client C2 → Server A → agent A
```

C1 和 C2 可以是同一个 Client，也可以不同。每条消息独立选路；不要求回复经过原 Client、原连接或原端口。请求发出后，连接立即继续承载其它消息；回复可以乱序到达。

## 2. 本机优先与端到端职责

| 步骤/组件 | 必须完成的工作 |
| --- | --- |
| camc 发送 | 定向 agent 操作先按现有规则处理本机目标；本机不存在目标或显式指定远端时交本机 Server；run 指定目标 Node 与预分配的新 agent ID |
| 本机 Server | 保存待发送消息，根据目标 Node 选择可直达它的 Client |
| Client | 根据 `target.node_id` 找到连接，转发完整 message |
| 目标 Server | 认证、去重并交给已注册的本机消息接收端 |
| 本机消息适配层 | 沿用 camc 的 mailbox/通知机制，找到目标 agent 并完成本机投递 |
| agent 回复 | camc 创建新 message，收发地址对调并填写 `reply_to`，重新走上述流程 |

显式指定远端 `Node:agent` 时，直接交给本机 Server，不能命中本机同名 agent。已找到本机 agent 但投递失败时返回本机错误，不能把相同名称的远端 agent 当作替代目标。

远端转发前必须确定目标 Node；只有 agent 名而尚未定位时返回 `TARGET_UNRESOLVED`，不广播执行业务。Router 首版不增加全网 agent 目录；Client 不做 agent 查找。目标定位能力与 agent 信息后续迁移分开接入。

Router 先行：现有本机管理、launcher、monitor、attach 和 legacy Desktop/Mobile 路径保持。Server 使用本机接收端或共享消息模块，不启动 camc CLI 或第二个 camc 接收守护进程。camc 消息接入必须单独实现和验证。

## 3. Client 首版必做清单

| 编号 | 必须做的事 |
| --- | --- |
| CF-01 | 维护每个 Server 的专用双向 forwarding 通道；认证真实 Node 与 Client 身份，复用连接层的认证、心跳和重连 |
| CF-02 | 通告获准中继且当前直连的目标 Node；连接变化立即更新，定期刷新，断线或过期即失效 |
| CF-03 | 解析协议帧及信封，检查来源、目标、消息 ID、期限和转发权限；正文按原内容转发 |
| CF-04 | 按目标 Node 选择已就绪连接；request、reply、投递状态使用相同转发入口，不另做业务回程协议 |
| CF-05 | 一条通道并发承载多个消息，允许不同目标和相反方向流量；单帧写入排队，不锁住连接等待回复 |
| CF-06 | 报告路径失败及交付是否确定，撤回失效路径；消息保存、去重、重试和业务关联归两端 Server/camc |
| CF-07 | 限制帧大小、发送队列和写入并发，隔离慢目标，明确背压，不静默丢消息 |
| CF-08 | forwarding 在后台运行；提供通道、可达目标、队列、错误与耗时状态，默认日志不记录正文或凭据 |

Client 只需维护 `node_id → 当前连接`、通道状态和有界发送队列。Server 保存 `目标 Node → 可用 Client 集合`；Client 不保存等待 agent 回复的 pending 表、对话历史或权威消息数据库。

操作的支持/不支持范围、名称、目标和返回约定见总 Spec 的 [远端操作范围](cam-client-server-spec.md#61-远端操作范围)。第一步十项业务操作均须明确 `target.node_id` 与 `target.agent_id`；run 创建其预分配的新 ID，其余操作针对已有 agent。远端 list、跨节点列表聚合、空目标/通配符、批量及 Node 级业务操作本轮不开放；现有本机 list 保持。目标机器负责登记与业务白名单检查，Client 不解析每个命令的参数，也不能把整套 CLI 作为兜底。

## 4. 专用通道与可达通告

每个 Client–Server 对建立独立的双向 forwarding 逻辑通道，与终端 PTY/attach 输出分开。可以复用已有 SSH transport 的独立 channel，或使用 WebSocket；具体地址与 wire 编码在实现时确定。握手协商版本、Node/Client 身份及可中继范围，不为每条消息新建 SSH 或启动命令。

Client 只有在与 B 认证成功且 forwarding 就绪时，才向 A 宣告可直达 B。配置项、过期缓存和其它节点的路由通告都不能当作直连证据。

逻辑快照示例：

```json
{
  "type": "routes.snapshot",
  "session_id": "client-c-to-a-7",
  "seq": 12,
  "ttl_seconds": 30,
  "reachable": ["node-b", "node-c"]
}
```

- A 根据当前已认证连接识别 Client；快照是完整集合，省略目标视为撤回，不宣告 A 自身或间接路由。
- 建连及连接变化时立即更新，周期刷新间隔小于协商 TTL。示例 30 秒不固定为默认值。
- A 按本机接收时间计算有效期并设置上限；Client 通道断开立即使该路径失效。
- `session_id`/`seq` 区分连接代次及先后，旧连接事件和旧快照不能覆盖新状态。
- 同一 B 可同时经 C1、C2 到达；每条消息由来源 Server 选一条路径。首版不做 Client 连锁中继或路径评分。

## 5. 统一消息与 Client 转发步骤

业务消息统一使用 `type=message`。以下为必需语义的示例，最终 wire schema 由共享协议落实：

```json
{
  "version": 1,
  "type": "message",
  "message_id": "msg-1",
  "source": {"node_id": "node-a", "agent_id": "agent-a"},
  "target": {"node_id": "node-b", "agent_id": "agent-b"},
  "operation": "message.send",
  "payload": {"text": "请检查这个问题"},
  "expires_at": "2026-10-19T21:00:30Z"
}
```

回复仍使用同一种 message：

```json
{
  "version": 1,
  "type": "message",
  "message_id": "msg-2",
  "reply_to": "msg-1",
  "source": {"node_id": "node-b", "agent_id": "agent-b"},
  "target": {"node_id": "node-a", "agent_id": "agent-a"},
  "operation": "message.send",
  "payload": {"text": "检查结果如下"},
  "expires_at": "2026-10-19T22:00:30Z"
}
```

同一条消息重试保持 ID、地址和内容；回复是新消息，有自己的期限。`reply_to`/可选 thread 关联保存在两端消息记录中，Client 不靠它们决定路径。Router 测试可以用 `receiver_id=test.echo` 的已注册接收端替代 agent，回复仍是新消息。

send/capture/run/stop/rm/kill/reboot/key/status 使用该 message 信封：`operation` 标识结构化操作，目标指定具体 agent，参数放在 payload。接收端返回新的 `operation.result` message，其 `reply_to` 指向原操作的 message ID，并携带明确结果/错误；Client 沿相同入口转发。具体业务能力逐项接入，不代表当前已支持全部 camc 命令。协议控制帧、内部投递状态和 echo 测试接收端与这些业务操作分开，不新增 Node 级业务入口。

capture/status 等同步命令由来源 camc 等待本机 Server 的结果。来源 Server 保存原消息与本机调用的对应关系，收到普通 `operation.result` 后交还 stdout/stderr/exit_code；CLI 按现有格式输出并退出。关联与等待不放在 Client，返回可经不同 Client，不阻塞通道处理其它消息。结果投到来源 Server 的内部接收端，不要求原调用方一定也是 agent。详见总 Spec 的 [stdout 返回约定](cam-client-server-spec.md#62-capturestatus-等命令的-stdout-返回)。

Client 对每条 message 只执行以下步骤：

1. 校验入站连接、帧格式/大小、来源授权及期限。来源 Node 必须匹配已认证入站 Server；目标 Server 仍独立校验授权。
2. 按 `target.node_id` 查找自己直接连接且允许转发的目标；没有连接就报告路径错误，不递归交给其它 Client。
3. 将原消息放入该目标的有界发送队列，保持地址、ID、`reply_to`、operation 和 payload 内容不变。
4. 按消息边界写入目标通道，随后继续处理下一条。短期传输确认可由连接层处理；不等待业务回复。

隧道连接和分帧由传输库处理。Client 解析信封用于选路、权限和容量检查，不解释 payload 中的 agent 对话或 tool 选项，也不从终端画面提取回复。

## 6. 投递、回复与恢复边界

**投递状态与 agent 的业务回复分开。** 写 socket 成功只能证明本次写入；目标 mailbox 的持久接收由目标接收端确认，不能据此声称 agent 已阅读或回答。

如需投递回执，目标 Server 可产生普通状态 message，携带新 ID 和 `reply_to`，经同一路由入口发给来源 Server 的状态接收端。回执不触发更多回执，不注入为 agent 的回答。Client 不需要单独的 `forward.request/query/response/result_ack` 业务协议。

| 情况 | 处理责任 |
| --- | --- |
| 明确未向目标写入 | Client 报告 `not_forwarded`；来源 Server 决定重试或换路径 |
| 已写出/部分写出但没有目标确认 | Client 报告 `unknown`；来源 Server 按原 ID 查询或恢复，不新建业务副本 |
| B 或 Client 断开 | 路径失效；来源 Server 保留待发消息，不依赖 Client 内存恢复 |
| 原 C1 消失后 agent 才回复 | B 保存新的回复消息，重新选可达 A 的 C2，正常投递 |
| A 暂时离线 | B 的待发队列保留回复，按消息期限恢复投递；不能因原请求已投递就丢回复 |
| agent 没有回复或回复超时 | 由 camc/业务调用方记录等待状态，不占 Client 连接，不重发原任务催出副作用 |

两端 Server 按认证来源和 `message_id` 去重，并核对目标与内容摘要；同键不同内容拒绝。去重与待发队列必须持久化，保留期覆盖重试窗口；接收端未知是否已经处理时返回不确定状态，不盲目再次投递。状态查询属于 Server 接口，Client 不维护端到端业务等待记录。

camc 适配层维护本机 thread/seq 与跨 Node `message_id`、`reply_to`、收发地址的稳定映射。现有 camc 的 thread ID 不能直接作为每个 turn 的消息 ID。本机 `msg reply` 接入该映射后创建独立回复，复用本机优先发送流程；不能因 Router 连接成功就宣称该适配已完成。

## 7. 容量、后台与诊断

每个连接与整个 Client 都限制帧大小、排队字节数和写入并发。尚未接收的消息可明确拒绝；已接收消息不能静默丢弃。一个慢目标不能阻塞其它目标，队列不能无限增长。

UI 刷新、切换 agent、关闭终端标签不影响后台 forwarding；后台退出或系统暂停会使路径失效，但不停止 agent。状态至少显示实际 Node、通道状态、可达目标、队列大小、最后错误和最近转发耗时。日志仅记录来源/目标 Node、message ID、连接及错误元数据，不记录正文、token 或完整环境。

## 8. 必须通过的验收

至少使用 A、B 两个独立 Server 和 C1、C2 两个独立 Client 后台；再增加第三个 Server 验证并发多目标。两个 UI 页面共用后台不算双路径。

| 场景 | 必须观察到的结果 | 对应清单 |
| --- | --- | --- |
| C 连接 A、B，B 随后断开/重连 | 只通告真实直连；及时撤回；旧快照不覆盖新连接 | CF-01、02 |
| 本机 agent 存在、本机未命中、显式远端同名目标 | 分别本机投递、交本机 Server、精确远端；本机投递错误不换目标 | CF-03、04 |
| A→B、B→A、A→第三节点同时发送多条消息 | 各到目标，允许乱序，等待回复不占通道 | CF-03、04、05 |
| 请求经 C1，业务回复经 C2 | 回复有新 ID 和正确 reply_to；来源 agent 收到，C2 没有旧请求记录也能转发 | CF-04、05、06 |
| msg/send/capture/run/stop/rm/kill/reboot/key/status 的目标能力已注册 | 明确目标 Node/agent，参数保持不变，结果/错误使用新 message 返回；Client 不执行命令或等待结果 | CF-03、04、05 |
| 空 agent、通配符、批量或仅指定 Node 的业务操作 | 明确拒绝；run 使用固定的新 agent ID，status 只查指定 agent | CF-03、04 |
| reboot 或 key 重复投递 | 目标只重建一次 launch 或注入一次按键，结果返回不依赖原 Client | CF-04、06 |
| capture/status 并发等待，结果乱序或重复 | 来源 CLI 各自得到正确 stdout/stderr/退出码；无协议包装或重复输出，其它消息继续转发 | CF-04、05、06 |
| 远端 list 等未开放操作，或目标未注册对应能力 | 明确不支持/未授权，不执行任意 CLI 兜底；现有本机 list 不受影响 | CF-03、04 |
| 请求投递后 C1 退出，agent 很久后才回复 | B 独立保存并发送回复，无业务回程表依赖 | CF-05、06 |
| A 暂时离线，稍后恢复 | B 按回复自己的 ID/期限恢复投递，不重新执行原任务 | CF-06 |
| 消息重试、多 Client 重复送达、同 ID 改正文 | 相同消息目标只投递一次；内容冲突被拒绝 | CF-03、06 |
| 收到投递回执，尚无 agent 回答 | 回执与业务回复分开呈现，无回执循环或假回答 | CF-04、06 |
| 部分写入后断线 | 报告 unknown；Server 保存恢复依据，不静默丢失或新建副本 | CF-06 |
| 目标无连接、来源伪造、目标无授权、递归中继 | 明确拒绝，无错误目标投递 | CF-01、03、04 |
| 帧过大、队列满、一个目标阻塞 | 有界背压与错误，其它目标继续工作 | CF-07 |
| 页面刷新/关终端标签、退出后台 | 前者不中断 forwarding；后者路径失效且 agent 继续运行 | CF-08 |

协议、真实 transport 和本机消息适配分开验证。Router 先用独立 echo 接收端验通，再接真实 agent 的 mailbox 与显式回复。Desktop 连接层必须在打包的 Electron runtime 验证。专为测试创建的 agent、Server、连接与 allocation 全部回收，不回收复用的用户会话。

## 9. 实施顺序与完成定义

1. 共享协议落实统一 message 信封、可达快照、连接层错误和容量/时间参数；Server 保存/去重并提供本机接收端入口。
2. Client 完成 CF-01～08，通过多 Server/多 Client 的普通消息转发验收，不增加业务回程表或长时间 RPC 等待。
3. 接入 camc 本机优先发送和跨 Node 消息映射，验证显式回复、换 Client 返回及离线恢复；原有管理流程保持。

完成报告分别说明 Client 转发、Server 配套、camc 消息适配及各平台验收状态。首版按统一 message 模型验收；未验证的能力不能标记完成。
