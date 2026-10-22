# CAM Server / Client 跨接口行为标准（规范）

状态：v1.1（2026-10-19）。本规范是 CAM TUI、CAM Desktop、CAM Mobile、
`camc` adapter 与 CAM Server 共同遵守的跨接口行为标准。

上位架构规范是
[`cam-client-server-spec.md`](./cam-client-server-spec.md)。角色、所有权、
迁移阶段与验收目标发生冲突时，以上位规范为准；本文不定义新的产品角色，
也不要求当前代码已经具备全部能力。

下位规范：

- [`camc-spec.md`](./camc-spec.md)：legacy camc 当前行为与兼容约束。
- [`desktop/bridge-protocol-v1.md`](./desktop/bridge-protocol-v1.md)：
  迁移期 Bridge 请求/响应 schema、投递和兼容性规则。

本文中的“必须 / 必须不”是目标合规要求；“当前阶段”只说明实施顺序，
不代表已经完成。

## 1. 架构边界

CAM 的长期产品架构只有两类角色：

1. **CAM Server**：每个 Node 一个 active 实例。当前阶段先做 Router，
   负责连接、路由、请求投递和响应关联；后续逐步承接 tunnel、agent
   信息、生命周期与 runtime API。
2. **CAM Client**：TUI、Desktop、Mobile 以及必要的 CLI/camc adapter。
   Client 连接多个 Server，提供交互、展示和经授权的一跳中继。

**Hub 不是第三种产品角色。** Desktop Embedded Hub、Mobile 连接后台以及
代码中的 `TunnelHub` 都只是 Client 连接层或迁移期实现名称。跨 Hub 行为
必须落在上述 Client/Server 语义内。

`camc` 是 legacy CAM Client 和迁移期 fallback。它当前仍拥有 agent
registry、runtime、monitor 和 launcher，但这些能力将逐步迁入 CAM
Server，最终退役。新 Server 路径不得通过执行远端 camc CLI 补齐业务
能力。

## 2. 分阶段行为

| 阶段 | CAM Server | Client | camc |
| --- | --- | --- | --- |
| Router 先行 | 连接、路由、请求/响应记录；不改 agent registry | 接入 Server；保留必要 legacy 操作 | 继续拥有全部 agent 信息与本机 runtime |
| Agent 信息接入 | 只读导入/索引并标注来源与更新时间 | 验证统一列表、状态和历史查询 | 仍是未迁移 agent 的权威 |
| 按能力接管 | 管理已迁移 agent 的信息、生命周期和 runtime | 对已迁移能力使用同一 Server API | 只管理未迁移 agent |
| camc 退役 | 承接所需 agent info、tunnel 与管理能力 | TUI/Desktop/Mobile 成为主要入口 | 仅保留经验证的恢复/兼容路径，最终删除 |

一个 agent 在任一时刻只能有一个管理后端。Router 部署本身不改变任何
agent 的 owner，也不能把 Server 的连接状态写成 agent runtime 状态。

## 3. 身份与状态

- Node 的长期身份是稳定 `node_id`。`user@host:port`、context 名和 UI
  别名只能作为连接信息或迁移期索引，不能替代 `node_id`。
- Client 身份是独立后台实例的 `client_id`，不是浏览器 tab 或 UI 窗口。
- Router 阶段只要求把请求固定到目标 `node_id`。后续接入 agent API 后，
  完整 agent 身份必须包含 owner Node、agent ID 和管理后端。
- Server-owned agent 的权威状态属于 CAM Server；legacy-owned agent 的
  权威状态仍属于目标机器上的 camc。Client 缓存、Server 路由记录和
  Bridge 索引都不是 runtime 真相。
- 同一 Node 不得因多个 Client、工作区或 agent 启动第二个 active
  Server；第二个实例必须拒绝启动或连接已有实例。

## 4. Add Node / Connect / Sync 行为

所有前端必须把这些操作用户可见语义统一到同一 Client/Server 契约：

1. 校验 endpoint、身份、认证信息和协议版本。
2. 建立 Client–Server 连接，协商能力并交换快照。
3. 记录连接状态、可达目标、最近更新时间和结构化错误。
4. 重复执行必须收敛到同一终态；并发执行不得互相覆盖连接状态。

**Sync 不等于部署 runtime。** 它不得把远端 agent 变成本地 agent，不得
改变 agent owner，也不得隐式启动第二个 CAM Server。

legacy `camc sync` 只是迁移期部署 camc/配置的工具；`camc tunnel serve`
只是 legacy 轻量执行端点。它们不是“启动完整 CAM Server”，也不能成为
新 Client 的统一 Sync 语义。

## 5. 路由行为

路由必须先确定逻辑目标，再选择 transport：

- 目标是本机 Node 时，由本机 Server 或既有本机 legacy 路径处理，不
  经过中继。
- 目标是远端 Node 时，Client 可以直接调用目标 Server；来源 Server
  也可以选择一个已授权、直接连接目标的 Client 做一跳中继。
- 第一版只允许 `Server A → 一个 Client → Server B`。Client 不得把
  中继请求继续交给另一个中继，不得形成 A → B → A 环路。
- 目标 Node 一旦确定，重试、换路径或 Client 重连都不得改换目标。
- 显式指定远端 Node 的请求不得在失败后静默改成本机执行。
- Server 负责选择路径；Client 只宣告自己真实直连的 Node 并执行已授权
  转发，不把间接听到的路由当作直连。

legacy camc 的 machines.json、Fleet Bridge、mailbox 和 tunnel 规则属于
迁移期适配路径。它们可以继续服务旧工作流，但不得改变新 API 的目标、
request ID、权限或响应语义。

## 6. 请求投递与幂等

新路径使用稳定 `request_id` 贯穿来源 Server、中继 Client、目标 Server
和响应。传输可以重试，目标 Server 必须维护持久投递记录与结果记录。

- 第一次交付前记录请求摘要；重复 ID 带不同内容必须返回
  `REQUEST_CONFLICT`。
- 同一请求处理中再次到达时复用原记录；已有最终结果时返回原结果。
- transport 超时、断线、排队和 release 都不是业务终态；不能据此自动
  重新产生副作用。
- 无法区分“尚未处理”和“已处理但结果丢失”时，必须返回 `unknown`，
  允许调用方用原 ID 查询，禁止伪装成失败并重放。
- Router 阶段只保证通信投递记录；接入某项修改操作前，对应接收端也
  必须支持 request ID、结果查询或等值去重。
- 不承诺跨所有崩溃场景的 exactly-once；承诺正常重试和多路径重复投递
  不产生重复副作用，不确定结果必须显式暴露。

## 7. Agent 信息与管理边界

Router 阶段：

- CAM Server 不读写 agent registry，不接管 tmux/PTY、monitor、环境、
  launcher 或 tool home。
- 既有 `camc → runtime` 与 Desktop/Mobile 的 legacy camc 管理路径保持
  可用。
- 没有已注册接收端或业务 API 时，Server 必须返回明确的
  `UNSUPPORTED_OPERATION`/不可达，不能启动 camc CLI 或 tool。

后续迁移阶段：

- Server 可以只读导入 legacy agent 信息，但必须标注来源、更新时间和
  管理后端；只读导入不代表接管。
- 接管某个 agent 时必须保留身份、启动配置、session/launch 关联和必要
  历史，并验证旧后端已释放控制权。
- Server-owned agent 不得因 Server 暂时不可达而自动交给 legacy camc
  重放修改操作；legacy-owned agent 也不得被 Server 静默写入。

## 8. API 与前端一致性

- TUI、Desktop、Mobile、CLI 和 `camc` adapter 对已迁移能力必须调用同
  一组 Server API，不得各自实现私有 route、重试或副作用规则。
- HTTP、WebSocket、本机 IPC 和 UI 后端必须复用同一 service/capability
  层；不同入口不得得到不同目标或不同错误含义。
- queued/pending、application error、transport error、unsupported 和
  ambiguous target 必须保持可区分。
- UI 可以做缓存、渲染、分页和交互优化，但必须显示 stale/offline，不
  得把缓存当成 runtime 真相。
- Client 退出不得停止 agent；连接断开不得直接改写 agent 生命周期。
- 终端 attach、input/resize、状态事件和历史通道可以优化，但不得绕过
  Server 的权限、身份和 request 语义。

## 9. 安全与审计

- 本机 IPC 和远端连接都必须认证，并按 Client/Server 身份限制能力；
  目标 Server 必须重新校验，不只相信中继声明。
- token、私钥、密码、完整环境值和敏感 payload 不得进入默认日志。
- token 比较必须 timing-safe；监听地址默认 loopback，远程暴露必须显式
  配置。
- 审计至少记录 request ID、来源/目标 Node、使用的 Client、路径切换、
  接收端、结果、错误和耗时。
- 迁移期 adapter 必须声明协议版本与能力；未知可选字段可忽略，改变
  目标、所有权或响应语义必须提升协议版本。

## 10. 合规验收

Desktop、TUI、Mobile、camc adapter 与 CAM Server 必须通过同一组
conformance 场景，不能用各前端局部单元测试替代：

- 同一 Node 启动第二个 Server：拒绝或连接已有实例。
- Router 离线：legacy camc 本机管理流程不受影响。
- Client 直连目标 Server，以及 A → C1 → B：目标接收端收到原请求，
  来源收到同 ID 响应。
- C1 断开、C2 在线：同 ID 切换路径，不生成新业务请求。
- 两条路径送达同一请求：目标只产生一次业务副作用；不能确认时返回
  `unknown`。
- 目标已处理但响应丢失：换路径查询返回原结果，不重复处理。
- 无接收端、过期请求、同 ID 不同参数、递归中继和显式远端目标失败：
  明确拒绝，不静默降级或改换目标。
- 同一 agent 从 Server-owned/legacy-owned 两侧观察：管理后端和 owner
  一致，不出现两个可独立控制的副本。
- Desktop、TUI、Mobile 对同一能力返回相同目标、响应类别和错误语义。

## 11. 当前差距

截至 2026-10-19，以下内容必须视为未完成或迁移期行为：

1. 新 CAM Server Router 尚按上位规范实施，不能宣称已完成全链路
   Client/Server 迁移。
2. agent registry、runtime、monitor 和 launcher 仍由 legacy camc 拥有。
3. legacy `camc tunnel serve` 还缺少完整的目标侧 seen-id/in-flight
   去重，keeper 与人工 flush 也需要互斥。
4. 多 Client 中继、目标端持久结果恢复和跨重启 `unknown` 语义需要按
   上位规范验收。
5. Relay 是 legacy/unsupported，不属于当前合规路径。
6. Desktop Embedded Hub、Mobile Direct、TUI 与 CAM Server 尚未通过同一
   套 conformance 测试。

在上述差距关闭前，可以说明当前存在 legacy bridge/tunnel 和分阶段迁移
实现，但不得声明“所有前端已完全统一”或“新 CAM Server 已接管 agent
管理”。
