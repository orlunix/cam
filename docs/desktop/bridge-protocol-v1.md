# Bridge Protocol v1（规范）

状态：v1.1（2026-10-12，router + tunnel 落地）。desktop bridge 实现以
本规范为线上协议依据（实现侧笔记见 [`camb-bridge.md`](./camb-bridge.md)，
总纲见 [`fleet-bridge-plan.md`](./fleet-bridge-plan.md)）。

跨 CAM Server / Client（Desktop / TUI / Mobile）的统一行为与合规门槛，
以上位规范 [`../hub-server-spec.md`](../hub-server-spec.md) 为准；本文只定义
迁移期 Bridge 的 **schema 与投递语义**，transport 可插拔。
任何 bridge host（desktop / mobile / python-cli）与任何 ferry
（camb / 归并后的 camc）只要满足本规范即可互通。

## 1. 范围与分层

- 标准化：消息 schema（§2）、解析规则（§4）、consent 模型（§5）、
  verb allowlist（§6）、超时与退出码（§7）、审计格式（§8）、
  投递语义（§9）、兼容性规则（§11）
- 可插拔：transport。v1 = 文件信箱（§3）；v1.1 = 多 hub router
  （§9.2 coverage cards）；v2 = 隧道加速器（§10，已实现）
- 实现分歧提到本规范解决；任何实现不得私自发明字段或语义

## 2. 消息 schema

### 2.1 Request

| 字段 | 类型 | 必须 | 说明 |
| --- | --- | --- | --- |
| `id` | string | ✓ | uuid4 hex；全舰队唯一。host 将其用作 inbox 文件名，必须匹配 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` 且不含 `..` |
| `argv` | string[] | ✓ | camc 风格完整 argv（含 verb）；ferry 不做 verb 校验 |
| `from` | object | ✓ | `{"agent": string\|null}`；ferry 由 `$TMUX` socket 名（`cam-<id>.sock`）best-effort 推断，推断不到为 null |
| `ts` | number | ✓ | unix 秒（float） |
| `proto` | number | 可选 | 协议版本；缺省 = 1 |

ferry 侧唯一的 argv 客户端逻辑：`send --file <path>` 与
`send --stdin` 在落盘前转换为 `--text <内容>`；host 只见 `--text`。

### 2.2 Response

| 字段 | 类型 | 必须 | 说明 |
| --- | --- | --- | --- |
| `id` | string | ✓ | 回显 Request.id |
| `ok` | bool | ✓ | |
| `text` | string | ok 时可选 | 目标 camc 的 stdout（list 的表格/json 也在这里） |
| `error` | string | 失败时 ✓ | 人类可读错误；camc 原生错误文本原样透传 |
| `candidates` | string[] | 歧义时 ✓ | 每项 `"<node>:<id8> <name>"` |
| `proto` | number | 可选 | 缺省 = 1 |

**未知字段：接收方必须忽略**（向前兼容的基础）。

## 3. 文件 transport（v1）

### 3.1 目录布局

```
~/.cam/camb/outbox/<id>.json     ferry 写，host drain 收
~/.cam/camb/inbox/<id>.json      host 写，ferry 读
~/.cam/camb/hubs/<hub-id>.json   host 覆盖名片（router，§9.2）
~/.cam/camb/hubs/<hub-id>.token  host 隧道令牌（0600，§10）
```

### 3.2 规则

- **提交**：ferry 写 `<id>.json.tmp` → `rename` 为 `<id>.json`
  （原子发布）；drain 跳过 `.tmp` 文件
- **drain**：host 在 node 上 exec `~/.cam/camb --hub-drain
  --bridge-id <id>`。node 侧对每个请求先原子认领（rename →
  `<id>.json.claimed-<id>`，先者胜），再把认领到的请求打成 JSON
  数组打印到 stdout（空 = `[]`），随后删除已认领文件；不可读的
  文件留在原地；**恒 exit 0**。无 `--bridge-id` 的调用保持 v1
  语义（打印全部 + 删除）
- **名片**：host 每 ~15s 在自己管理的 node 上刷新
  `hubs/<hub-id>.json`；ts 超过 90s 未刷新视为该 host 已死（§9.2）
- **响应**：host 先 `mkdir -p ~/.cam/camb/inbox`，再写
  `inbox/<id>.json`；ferry 轮询读到后删除
- **ferry 等待**：默认 30s（env `CAMB_TIMEOUT_MS` 可调）；
  轮询间隔 0.1s 量级
- **host 轮询**：建议周期 2s；失败指数退避 ×2、上限 60s、成功复位；
  drain exec 超时 10s；worker 用 chained timer + inflight 防重；
  node 的 bridge 开关关闭或 context 删除时 worker 自终止

### 3.3 时序

```
ferry                     host worker                    node 文件系统
  │ tmp → rename ──────────┼──────────────────────────▶ outbox/<id>.json
  │                        │── exec --hub-drain ───────▶ 打印 JSON 数组
  │                        │◀── [req, …] ─────────────── 随后删除
  │                        │   解析 / consent / dispatch（§4–§6）
  │ 轮询读到后删除 ◀── inbox/<id>.json ◀── writeRemoteFile
```

## 4. 解析规则（host 权威）

- `node:ref`：精确路由到该 node；node 不存在 →
  `unknown node '<name>'`；agent 不在该 node →
  `no agent '<ref>' on node '<name>'`
- 裸 `ref`：先匹配**来源 node**，再匹配全舰队
- 匹配规则（对一个 node 的 agent 集）：`id` 全等 > `id` 前缀
  （id 比 ref 长且 startsWith）> `task_name` 大小写不敏感全等
- 任一阶段命中 >1 → 返回 `candidates`，**不执行**
- ferry 可以带 hint（bridge cache），host 必须**总是重新解析**，
  hint 只许影响性能不许影响结果

## 5. Consent 模型

- 开关 per-node：context 记录 `machine.bridge`（bool，默认 false），
  UI 在 Nodes 页 Add/Edit Host 表单
- **看全部，控对称**：`list` 免 consent，聚合全舰队（跑 node 即视为
  同意被看见）；targeted verbs 要求**目标 node** 也开 bridge，
  否则拒绝、记审计、且对目标 node 不产生任何流量
- 来源 node 不开 bridge = 没有 worker = outbox 无人 drain，ferry
  超时 exit 2（行为自解释，无需额外门）

## 6. Verb allowlist v1

- `list`：fleet 聚合（camc `list -a` 语义）。flags：`--json`、
  `--status`、`--tag`、`--last/-n`（默认 50）。表格列
  `ID NAME TOOL STATUS CONTEXT`，`updated_at` DESC
- `status` / `capture` / `send` / `key` / `msg`：argv 原样转发目标
  node 的 camc 执行。目标参数位置：status/capture/send/key 在
  `argv[1]`；`msg send` 在 `argv[2]`
- 其它 verb → `unsupported verb '<v>' via bridge (v1: list, status,
  capture, send, key, msg)`
- 扩容路径（第二步 run/stop/…）：只扩 allowlist，transport 与转发
  通道零改动

## 7. 超时与退出码

ferry（camb / camc bridge 路径）：

| exit | 含义 |
| --- | --- |
| 0 | 收到 ok 响应；`text` 已打印到 stdout |
| 1 | 收到失败响应；`error` + 逐行 `candidates` 打印到 stderr；用法错误同码 |
| 2 | 超时 / bridge 不可用（hub 未连接或本 node 未开 bridge）；事件留在 outbox |

host 侧：

| 项 | 值 |
| --- | --- |
| drain exec 超时 | 10s |
| targeted exec 超时（msg） | 120s（`camc msg` 默认等 600s；长等让发送方加 `--no-wait` 或 `--timeout`） |
| worker 退避 | 2s 起步 ×2，上限 60s，成功复位 |

## 8. 审计

每次 dispatch（**含 consent 拒绝**）一行 JSONL，落 host 数据目录
`<dataDir>/bus-audit.jsonl`：

```json
{"ts": "...", "from_ctx": "...", "from_agent": "...", "argv": [...],
 "target_ctx": "...|null", "ok": true, "error": "...|null"}
```

另 host 日志一行摘要。审计不得记录 secrets（token/key/密码）。
**审计是强制件**：任何 bridge host 实现不得省略。

## 9. 投递语义与多 bridge

### 9.1 单 bridge（v1.1 已硬化）

- 正常路径：exactly-once（outbox 原子发布 → 认领+drain → dispatch →
  inbox 写回）
- **seen-id 幂等（已实现）**：host 对每来源 node 缓存近期 dispatch
  过的 event id（10min / 500 条）；重复 id 直接重投缓存响应，绝不
  二次执行；dispatch **进行中**到达的重复 id 共享同一 in-flight
  promise，不会并发双跑
- **claim 孤儿重收（已实现）**：drain 在打印前原子认领
  （`.claimed-<id>`）；传输中断时已认领的文件由同一 host 下次
  drain 重新收集（无桥接 id 的调用也会收养无存活名片的孤儿）。
  配合 seen-id，实际语义 = at-least-once 传输 + 幂等执行
- 响应写回失败：操作可能已执行而发送方只见超时——不确定态，以
  host 审计为准

### 9.2 多 bridge（v1.1 已实现：coverage cards router）

同一 node 挂多个 bridge host 时，**node 上的名片目录充当路由表**
——hub 间不需要任何通信（它们可能互相不可达，但都够得着交集
node）：

- **名片**：每个 host 每 ~15s 在自己管理的每个 node 上原子写
  `hubs/<hub-id>.json`：`{hub_id, ts, covers:[{name, key}], tunnel?,
  proto}`。`key` 是跨 hub 的规范节点身份（`user@host:port`），
  `name` 是 host 本地的 context 名（两个 host 可以给同一机器起
  不同名字）。ts 超过 90s 未刷新 = 该 host 已死，其名片失效
- **认领**：drain 携带 `--bridge-id`；node 侧原子 rename 先者胜，
  只打印/删除自己认领到的
- **路由规则**：显式 `node:ref` 目标（支持 name 或 endpoint key
  形式，含内部冒号）若被**其它**存活 host 的名片覆盖 → 跳过不
  认领；自己覆盖 / 无人覆盖 / 裸 ref / list → 认领
- **释放**：host 认领后发现无法路由（裸 ref 全舰队 miss）→ 把
  event 写回 outbox（attempts+1），让覆盖不同的其它 host 下轮
  认领；attempts ≥ 3 才产出最终错误（此时记审计）
- **收养**：claimer 无存活名片的 `.claimed-*` 孤儿由下一个 drain
  的 host 收养
- **幂等**：seen-id（§9.1）在多 bridge 下是强制件
- **响应**：由认领者写 inbox
- 每个 host 独立 consent 配置、独立审计文件。**覆盖重叠是常态**
  （desktop + mobile 管同一批 node）；consent 分歧属部署纪律，
  双方审计可查

## 10. Transport v2：隧道（已实现）

同一 schema 跑在 node→host 的 reverse forward 上（host 在既有 SSH
exec 连接上 `forwardIn`，node 上的 `127.0.0.1:<port>` 回到 host），
同步 request/response，唯一目的是降低延迟（mailbox 最坏要等一个
poll 周期）。规则：

- **best-effort**：隧道只是加速器；mailbox 是 source of truth。
  ferry 发送优先级：**本机 → tunnel A → tunnel B …**（存活名片里
  advertised 的隧道；显式 node 目标按 covers 过滤；priority 降序）
  **→ 全失败落 mailbox**。应用层响应（含错误）是终态；只有传输
  失败与 `not_my_fleet` 沿链下降
- **帧**：单行 JSON `{token, request}` 入，单行 JSON response 出，
  一次连接一个请求
- **保活**：host 的 drain worker 每个 tick 顺手体检/重修隧道
  （forwardIn 绑定当前 pool 连接，连接更换即重挂）；正确性不依赖
  保活成功，隧道死亡最坏表现为本 tick 无快路径
- **安全**：per-node token —— host 侧由持久 hub secret 派生
  （`sha256(secret/nodeKey)`，secret 0600 存于 host 数据目录），
  部署到 `hubs/<hub-id>.token`（0600）。隧道端点只接受 bridge
  request schema（天然子集，完整 hub API 不暴露），timing-safe
  比较，错误令牌只回 `unauthorized`
- **幂等**：event id 全程不变；隧道半途失败落 mailbox 产生的重复
  由 host seen-id / in-flight promise 去重（§9.1）

### 10.1 反向隧道与 cam serve hub（2026-10-14,camb 0.3.0 起）

除了 hub 发起的 forwardIn 隧道，host 也可以**主动**向一个 cam
server 建立反向隧道（`camb reverse up`,ssh `-R`),cam serve 侧由
`tunnel_hub` worker(`server.tunnel_hub=true`）注册表管理多条反隧、
发布自己的 hub 名片 + 本地 §10 listener，并 drain mailbox。dispatch
路径优先级：活反隧 > 一次性 ssh > 放回 outbox。注册接口
`POST /api/tunnels/register`(Bearer = master token 或派生
`sha256(master "/" key)`);host 心跳每 60s 重注册，registry 条目
300s 过期。schema 与 §10 帧格式不变——反隧只是 hub→host 方向的一条
更快 transport，正确性依然只依赖 mailbox。

## 11. 兼容性规则

- `proto` 缺省 = 1；host 收到高于自己支持的 proto → 回
  `unsupported proto <n>`（ok=false）
- 接收方必须忽略未知字段；新增**可选**字段不算破版本；
  新增必须字段或改语义 → proto +1
- 混跑矩阵：旧 ferry + 新 host = 正常（旧 ferry 不发 proto，按 1
  处理）；新 ferry + 旧 host = ferry 负责在收到
  `unsupported proto` 时降级重试 proto 1 或显式报错
- v1.1/v2 混跑：旧 host（drain 不带 `--bridge-id`）+ 新 ferry →
  v1 语义（打印全部+删除，孤儿收养但不按名片过滤）；新 host +
  旧 ferry（无名片目录/无隧道）→ 自动退回 mailbox-only；隧道不可
  达的 ferry 落 mailbox —— 所有组合退化为 v1 行为，无硬断
- ferry 与 host 的版本探测走各自既有通道（ferry 部署时的版本
  比较；host 的 bridge-status），不在协议消息里握手
