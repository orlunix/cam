# Bridge Protocol v1（规范）

状态：v1 草案（2026-09-08）。desktop 实现与本规范一致
（实现侧笔记见 [`camb-bridge.md`](./camb-bridge.md)，总纲见
[`fleet-bridge-plan.md`](./fleet-bridge-plan.md)）。

本规范标准化的对象是 **schema 与语义**；transport 可插拔。
任何 bridge host（desktop / mobile / python-cli）与任何 ferry
（camb / 归并后的 camc）只要满足本规范即可互通。

## 1. 范围与分层

- 标准化：消息 schema（§2）、解析规则（§4）、consent 模型（§5）、
  verb allowlist（§6）、超时与退出码（§7）、审计格式（§8）、
  投递语义（§9）、兼容性规则（§11）
- 可插拔：transport。v1 = 文件信箱（§3，唯一已实现）；
  v2 = 隧道（§10，预留未实现）
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
```

### 3.2 规则

- **提交**：ferry 写 `<id>.json.tmp` → `rename` 为 `<id>.json`
  （原子发布）；drain 跳过 `.tmp` 文件
- **drain**：host 在 node 上 exec `~/.cam/camb --hub-drain`。
  node 侧把全部已 finalize 的请求打成 JSON 数组打印到 stdout
  （空 = `[]`），随后删除已读文件；不可读的文件留在原地；
  **恒 exit 0**
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

### 9.1 单 bridge（v1 已实现的语义，如实陈述）

- 正常路径：exactly-once（outbox 原子发布 → drain 打印+删除 →
  dispatch → inbox 写回）
- drain 传输中断：node 侧先打印后删除（camb `_hub_drain`），连接
  断开时已打印的条目**可能已删（丢失）或未删（下一 tick 重 drain →
  可能重复 dispatch）**。v1 接受 at-most-once + 偶发重复：
  发送方超时 exit 2 是显式失败，可安全重发；审计可查
- 响应写回失败：操作可能已执行而发送方只见超时——不确定态，
  以 host 审计为准
- **v1.1 硬化项（已知债）**：host 侧 per-context seen-id 短窗去重
  （幂等）；可选两阶段 drain（drain + ack）升级为 at-least-once

### 9.2 多 bridge（v1.1，协议扩展）

同一 node 挂多个 bridge host 时：

- **认领**：drain 扩展为 `--hub-drain --bridge-id=<id>`，node 侧先
  原子 `rename outbox/<id>.json → outbox/<id>.claimed-<bridgeid>`，
  先者胜；只打印/删除自己认领到的文件
- **幂等**：每个 host 对 seen event id 去重（9.1 的硬化项此时变强制）
- **响应**：由认领者写 inbox
- 每个 host 独立 consent 配置、独立审计文件
- v1 规范只保证单 bridge per node；多 bridge 部署前认领 + 幂等
  两条必须已落地

## 10. Transport v2：隧道（预留，未实现）

同一 schema 跑在 node→host 的 reverse tunnel 上，同步
request/response，目的仅是降低延迟。规则：

- **best-effort**：隧道只是加速器；mailbox 是 source of truth。
  发送时隧道健康才走隧道，否则落 mailbox；每个 event 只走一条路，
  event id 幂等兜底
- host 尽最大努力保活（断线检测 / 重连 / 重发 forwardIn），但
  正确性不依赖保活成功
- **新增安全面**：node 上任意进程可经隧道调 host 端点 → 必须
  per-node token（ferry 部署时落盘、0600）+ host 只暴露 bridge
  子集端点（不得暴露完整 hub API）
- 实现前单独安全评审；本节届时升级为正式章节

## 11. 兼容性规则

- `proto` 缺省 = 1；host 收到高于自己支持的 proto → 回
  `unsupported proto <n>`（ok=false）
- 接收方必须忽略未知字段；新增**可选**字段不算破版本；
  新增必须字段或改语义 → proto +1
- 混跑矩阵：旧 ferry + 新 host = 正常（旧 ferry 不发 proto，按 1
  处理）；新 ferry + 旧 host = ferry 负责在收到
  `unsupported proto` 时降级重试 proto 1 或显式报错
- ferry 与 host 的版本探测走各自既有通道（ferry 部署时的版本
  比较；host 的 bridge-status），不在协议消息里握手
