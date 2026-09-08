# Fleet Bridge 总体方案（camc baseline / 架构 / 职责 / 实现指导）

状态：plan v1（2026-09-08）。
v1 实现（camb + desktop bridge worker + UI 开关）已完成、测试全绿、待 commit。
本文是 camc 归并与多 bridge 标准化的总纲。

关联文档：
- 协议规范（schema/语义的唯一权威）：[`bridge-protocol-v1.md`](./bridge-protocol-v1.md)
- v1 实现笔记（desktop 侧现状）：[`camb-bridge.md`](./camb-bridge.md)

## 0. 最高原则：camc 是 baseline，稳定性高于一切

Bridge 是给 camc 加能力，不是改造 camc。以下五条是验收级红线，
任何一期、任何组件都不得突破：

1. **无 bridge = 零变化**：未配置 bridge 的 camc，行为与今天逐字节
   一致。用 golden test 锚定本机 list/run/attach/msg/capture 全路径。
2. **本机操作零网络依赖**：`/tmp` socket 能解析的目标，绝不产生任何
   bridge 流量；bridge 全挂时本机操作不受任何影响。
3. **bridge 故障只降级不扩散**：远程解析失败 → 落 mailbox 或显式
   报错；绝不 hang；绝不改变本机命令的退出码语义。
4. **bridge 代码在 camc 内是纯新增模块**（解析挂钩 / cache / mailbox
   落盘独立成文件），不改既有函数的既有语义。归并 diff 中不允许
   出现对本机路径的改写。
5. **协议版本化**：`proto` 字段从第一天存在；旧 ferry（camb/老 camc）
   与新 bridge host 混跑行为可预期（spec §11）。

camc 侧一切改动归 cam-dev 所有（AGENTS.md 归属划分），本文 §8.1
是给它的交接 spec；Kimi/desktop 侧不直接改 camc。

## 1. 架构总览

```
node A                                   bridge host                            node B
┌────────────────────────┐   ┌────────────────────────────────────┐   ┌────────────────────┐
│ agent → camb | camc     │   │ worker（每个 bridge-enabled node 一）│   │                    │
│          │ 原子写 outbox│──▶│ drain ─▶ 解析 ─▶ consent ─▶ dispatch│──▶│ camc → agent       │
│ ~/.cam/camb/outbox     │SSH│      ─▶ 审计 ─▶ 写回 inbox           │SSH│                    │
│ ~/.cam/camb/inbox      │◀──│                                    │   │                    │
└────────────────────────┘   └────────────────────────────────────┘   └────────────────────┘
```

三个组件：

- **node 侧 ferry**：今天 = camb（薄壳）；camc 归并后 = camc 内置模块，
  camb 退役
- **bridge host**：desktop（已实现，参考实现）；mobile、python-cli
  对着 spec 实现同一份 schema
- **mailbox**：node 上的文件队列（outbox/inbox），是整个方案的语义基础

不变式（所有实现必须维持）：

- node 零凭据；node 之间零直接连接
- 一切跨 node 流量走 bridge host 已有的 SSH 通道
- camc 的 CLI 是接口标准；bridge 不重新实现 camc 的语义，只做
  聚合（list）、解析（目标定位）、转发（argv 原样）

关于"bridge cache / 隧道 / mailbox"的关系（对齐心智模型）：

- 用户方案里 camc 的解析顺序"本地 → bridge → mailbox"，在 v1 文件
  transport 下后两步是**同一个动作**：outbox 即 mailbox，写 outbox
  就是"发给 bridge"，host 解析不到就回 error/candidates，host 不在线
  就留在 mailbox 里等它回来。
- bridge cache（fleet 目录的本地缓存）在 v1 下是**空实现**——host
  永远权威解析，ferry 不需要知道目标在哪。cache 只在 v2 隧道
  （同步快路径）上线后才有意义，且那时也只是 hint。
- 隧道（v2）只是可选加速器；mailbox 永远是 source of truth。

## 2. 组件职责

### 2.1 camc（baseline，归并目标）

职责：agent 生命周期与 io 语义的唯一权威。归并后新增且仅新增：

- 目标解析顺序：本地 `/tmp` socket → bridge（落 outbox 等响应）
- bridge 配置项（开关、超时）；无配置 = 不启用，行为不变

绝不做：本机路径任何行为变化；本机命令等待网络；在 camc 里放
consent/审计/fleet 知识（那是 host 的职责）。

### 2.2 camb（过渡壳，v1 已实现）

职责：argv 原样打包（唯一客户端逻辑：`send --file/--stdin` →
`--text`）、outbox 原子写、inbox 等响应（默认 30s，超时 exit 2）、
`--hub-drain`（恒 exit 0）、`$TMUX` 推断 `from.agent`。

绝不做：verb 校验、目标解析、fleet 知识、持有凭据。

退役条件：camc 归并完成且 spec v1 冻结。

### 2.3 bridge host

七项职责：

1. drain worker（轮询 + inflight 防重 + 指数退避 + 自愈）
2. 权威解析（`node:ref` / 裸 ref 来源优先 / 歧义 candidates）
3. consent 门（看全部、控对称，spec §5）
4. dispatch（argv 原样转发目标 node 的 camc）
5. 审计（每次 dispatch 含拒绝，一行 JSONL）
6. 响应写回 inbox
7. ferry 部署（probe → 版本比较 → 写入 → chmod/mv → verify →
   ready-cache，skillm 同款）

绝不做：改写 event 内容；绕过 consent；在日志/审计里记 secrets。

实现者：desktop = `apps/cam-desktop/electron/embedded-hub.cjs`
bridge 段（参考实现）；mobile / python-cli 按 spec 实现。

### 2.4 mailbox（node 文件系统）

`~/.cam/camb/outbox|inbox`。持久队列：bridge 挂了、desktop 重启、
mobile 离线，事件都留在 node 上等下次 drain，不丢。多 bridge 认领
语义见 spec §9。

## 3. 路由与解析（host 权威）

spec §4 的摘要：

- `node:ref` 精确路由；裸 ref 先来源 node 后全舰队；歧义回
  candidates，绝不猜
- 匹配规则：id 全等 > id 前缀 > task_name 大小写不敏感全等
- consent：list 免 consent 聚合全舰队；targeted verbs
  （status/capture/send/key/msg）要求目标 node 也开 bridge，
  否则拒绝、记审计、对目标零流量

## 4. 可靠性模型与投递语义

- mailbox 是语义基础（持久队列）；轮询 transport 失败显式、退避自愈
- 隧道（v2）只是可选加速器，best-effort；v1 不上。理由见
  camb-bridge.md「为什么轮询而不是 SSH 反向隧道」
- 失败矩阵：

| 故障 | 行为 |
| --- | --- |
| bridge 不可达（node 视角） | ferry 超时 exit 2；事件留在 outbox 不丢 |
| node 不可达（host 视角） | worker 退避 2s→60s；`bridge-status` 可见 lastError |
| 目标不存在 / 歧义 / 未 consent | 显式 error 或 candidates；对目标零流量 |
| drain 传输中断 | at-most-once（spec §9）；发送方显式超时；v1.1 加 host 幂等 |
| 响应写回失败 | 操作可能已执行；以 host 审计为准 |

## 5. 多 bridge

- 同一 node 可被多个 bridge host drain；认领 = 原子 rename 先者胜
  （spec §9.2，v1.1 落地）
- event id 全舰队唯一（uuid4）；host 侧 seen-id 幂等兜底（v1.1）
- 每个 bridge 独立 consent 配置、独立审计文件
- 目标不变式：同一事件全舰队至多执行一次

## 6. 安全模型

- v1 零凭据成立的理由：ferry 只读写自己的 `~/.cam/camb`；权限边界
  = SSH 访问本身，与 camc 今天一致
- v2 隧道是**新增攻击面**：node 上任意进程可经隧道调 host API 驱动
  整个舰队 → 必须 per-node token（ferry 部署时落盘、0600）+ host
  只暴露 bridge 子集端点。spec §10 仅预留；实现前单独安全评审
- 审计是强制件，任何实现不得省略

## 7. 分期与验收

- **v1（已完成，待 commit）**：camb + desktop worker + UI 开关 +
  测试（hub-bridge 69 项 / camb 17 项）。
  验收：bridge node 间 list/status/capture/send/key/msg 全通；
  consent 拒绝路径有审计；无 bridge 的 node 不受影响
- **v1.1**：本文 + spec 冻结；camc 归并（cam-dev）；host 侧 seen-id
  幂等 + 多 bridge claim；python-cli bridge 参考实现。
  验收：camc 无 bridge golden test 全绿；归并后 camb 可退役；
  两个 host 同时挂同一 node 不重复执行
- **v2（可选）**：隧道加速器（best-effort + per-node token）；
  mobile bridge。
  验收：隧道全断时功能语义与 v1.1 完全一致，仅延迟变差

## 8. 实现指导

### 8.1 给 cam-dev：camc 归并 spec

模块边界（建议）：

- 新增单文件模块（如 `camc_bridge.py` 或同等），包含：outbox/inbox
  读写（直接复用 camb 的文件协议）、`from.agent` 推断、bridge 配置
  读取、超时控制
- 入口只挂钩一处：**本机目标解析失败处**。本机命中的路径一行不改

解析伪码：

```python
def resolve_and_run(verb, ref, rest):
    agent = find_local(ref)            # 今天的 /tmp socket 逻辑，不动
    if agent:
        return run_local(verb, agent, rest)
    if not bridge_enabled():           # 无配置 → 今天的报错，逐字不动
        return no_such_agent(ref)
    return bridge_call([verb, ref] + rest)   # 写 outbox，等 inbox
    # bridge_call 内部：超时（默认 30s，CAMB_TIMEOUT_MS 同款 env）→
    # 事件留在 outbox，exit 2；响应 ok=false → 原样打印 error/
    # candidates，exit 1；ok → 打印 text，exit 0
```

配置建议：`~/.cam/camc.conf`（或既有配置位置）加 `bridge = on|off`，
默认 off；或显式 `camc --bridge ...`。拍板权在 cam-dev，红线是
§0——默认配置下零行为变化。

测试 bar：

- 现有 camc 测试全绿（golden）
- 新增：bridge off 时行为与旧版逐字节一致；本地优先（bridge on 但
  目标在本机时不产生 outbox 文件）；mailbox 落盘与超时 exit code；
  `from.agent` 推断；`--file/--stdin` → `--text` 转换
- 参考实现：`apps/cam-desktop/resources/camb/camb` +
  `tests/test_camb.py`（17 项，可直接搬）

回滚：bridge 模块可整体剔除而不影响本机路径（文件级隔离，无交叉
import 进既有模块的函数体）。

### 8.2 给 bridge 实现者（mobile / python-cli）

最小实现清单 = §2.3 七项职责。参考实现指针（desktop）：

- worker/drain/写回：`embedded-hub.cjs` bridge workers 段
  （`_bridgeTick`：chained timer、inflight、退避 2s→60s、
  drain 失败清 ferry ready-cache 自愈）
- 解析：`_bridgeResolveTarget` / `_bridgeMatchAgent`
- list 聚合：`_bridgeFleetList`
- consent + dispatch + 审计：`_bridgeDispatch` / `_bridgeAudit`
- ferry 部署：`_ensureRemoteCamb`
- 测试脚手架：`apps/cam-desktop/test/hub-bridge.test.cjs`
  （69 项，ssh override 桩，可对照写自己平台的桩）

schema/语义一律以 spec 为准；实现分歧提到 spec 里解决，不要各自
发明字段。

### 8.3 测试与回归要求

- desktop：`apps/cam-desktop` 全部 test/*.test.cjs + lint 绿
  （hub-bridge 是 bridge 的守门套件）
- ferry：`tests/test_camb.py`（pytest 风格 + `__main__` 直跑）
- camc 归并：cam-dev 侧 golden + 新增（§8.1）
- 跨端联调（v1.1 手工 checklist）：两 node 一 host，list/status/
  capture/send/msg 双向；consent 关闭一侧验证拒绝 + 审计；
  host 重启后 outbox 积压事件被收走
