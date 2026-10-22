# camb — 跨 node fleet bridge 设计笔记

状态：v1.1 + tunnel v2 已实现（2026-10-12）。本文是设计契约，不是教程。

总纲与 camc 归并方案见 [`fleet-bridge-plan.md`](./fleet-bridge-plan.md)；
协议规范（schema/语义权威）见 [`bridge-protocol-v1.md`](./bridge-protocol-v1.md)。
本文是实现侧的现状笔记，冲突时以 spec 为准。

## 一句话

bridge 开关打开的 node 上，agent 可以用 `camb`（camc 对齐的薄 CLI）
查看整个舰队并向其它 bridge node 上的 agent 投递操作；所有逻辑在
camui hub（embedded-hub.cjs bridge workers）执行，node 上**零凭据**。

## 文件协议（mailbox 是 source of truth，隧道只是加速器）

```
node:  ~/.cam/camb/outbox/<uuid>.json   ferry 原子写（.tmp → rename）
       ~/.cam/camb/inbox/<uuid>.json    hub writeRemoteFile 写，ferry 读后删
       ~/.cam/camb/hubs/<hub-id>.json   hub 覆盖名片（router，15s 刷新，90s TTL）
       ~/.cam/camb/hubs/<hub-id>.token  hub 隧道令牌（0600）
hub:   每个 bridge-enabled context 一个 worker，2s 轮询
       `~/.cam/camb --hub-drain --bridge-id <id>`
       （原子认领 → 打印 → 删除；claim 孤儿重收 + 死 hub 孤儿收养）
```

请求 `{id, argv, from:{agent}, ts}`（release 回写时带 `attempts`）；
响应 `{id, ok, text?, error?, candidates?}`。`from.agent` 由 ferry 解析
`$TMUX` socket 名（`cam-<id>.sock`）得出，best-effort。

**隧道快路径（v2）**：hub 在既有 SSH exec 连接上 `forwardIn`
（node 的 `127.0.0.1:<port>` 回连 hub），ferry 发送按
本机 → tunnel（名片过滤 + priority）→ mailbox 的优先级链；
单行 JSON `{token, request}` 进、response 出。mailbox 永远在线，
隧道死活不影响正确性。

hub 不可达/未开 bridge：ferry 30s 超时（`CAMB_TIMEOUT_MS` 可调），
exit 2 + 明确报错。worker 失败指数退避 2s→60s，成功复位；
drain 失败会清 camb ready-cache 自愈重部署。

## 接口哲学：camc 是标准，camb 是过渡壳

- ferry 不做任何 verb 校验、目标解析、舰队知识——只把 camc 风格的
  argv 原样打包；唯一客户端逻辑：`send --file/--stdin` → `--text`、
  名片读取、隧道快路径（三者在 camb 与 camc `bridge.py` 间保持
  字节等价）
- 归并已落地（camc 1.2.139 ferry 吸收；1.2.140 v1.1 drain +
  tunnel）：`camc list -a` = fleet list，`camc <verb> <ref>` 本地
  优先、miss 才 ferry。camb 与 camc 双 ferry 长期互通（parity 测试）
- 扩容路径：targeted verbs 是 `_execCamcOnContext` 原样转发，加
  `run/stop/…` = 只扩 allowlist，transport 零改动

## 多 hub：名片路由（v1.1）

每个 hub 有持久 `bridge-hub-id`（存数据目录），每 tick 顺手写自己的
覆盖名片。drain 时 node 侧按名片过滤：**显式 `node:ref` 目标被别的
存活 hub 覆盖 → 跳过留给它**；裸 ref 全舰队 miss → hub 写回 outbox
（attempts+1，上限 3）让覆盖不同的 hub 认领。endpoint key
（`user@host:port`）是跨 hub 身份，context 名只是 hub 本地别名 ——
desktop + mobile 覆盖重叠的场景天然支持，hub 间零通信。

## 为什么轮询为主、隧道只当加速器

勘察结论（ssh-transport.cjs）：exec pool 连接按策略随时被 drop，
隧道会被无关流量静默掐断。v2 的解法不是"把隧道养稳"，而是
**让正确性完全不依赖隧道**：`ensureForwardIn` 绑定当前 pool 连接、
断了就等下个 tick 重挂，ferry 连不上就落 mailbox。seen-id +
in-flight promise 兜底双路径重复。隧道买来的只有延迟（2s → 毫秒级）。

## 幂等与投递语义

- host 对每来源 node 缓存 seen event id（10min/500）：重复 id 重投
  缓存响应，绝不二次执行；dispatch 进行中的重复 id 共享同一
  in-flight promise
- claim 孤儿重收 + seen-id = at-least-once 传输 + 幂等执行
- 审计：每次 dispatch（含拒绝与 release 终败）一行 JSON 落
  `<dataDir>/bus-audit.jsonl`

## 信任模型

- 开关 per-node：context 记录 `machine.bridge`（默认 false），
  UI 在 Nodes 页 Add/Edit Host 表单（nodes-mode.js + desktop.html）
- **看全部，控对称**：`list` 无 consent 聚合全舰队；
  targeted verbs（status/capture/send/key/msg）要求目标 node 也开
  bridge，否则拒绝且不产生任何到目标的流量
- 目标解析 `_bridgeResolveTarget`：`node:ref` 精确（name 或
  endpoint key 前缀）；裸 ref 先来源 node 后全舰队；歧义返回
  candidates；转发前剥掉 node 前缀
- 隧道端点只接受 bridge request schema + per-node token
  （timing-safe），完整 hub API 不暴露

## 运维注意

- camb 部署走 `_ensureRemoteCamb`（skillm/camc 同款
  probe→版本比较→writeRemoteFile→chmod/mv→verify→ready-cache），
  仅向 bridge 打开的 node 推送；bridge 关闭不卸载（幂等，重开即用）。
  v0.2.0 起 drain 支持 `--bridge-id`、ferry 支持隧道快路径；
  camc 1.2.140 的 `bridge.py` 与 camb 保持字节等价
- MSI 打包：`package.json` extraResources `resources/camb/camb → camb/camb`；
  容器：`apps/cam-container/Dockerfile` COPY 同名路径
- `msg send` 经 bridge 时 hub 侧 exec 上限 120s（BRIDGE_MSG_TIMEOUT_MS）；
  camc msg 默认等 600s——长等请让发送方加 `--no-wait` 或 `--timeout`
- 测试：`apps/cam-desktop/test/hub-bridge.test.cjs`（98 项，ssh override
  桩 + 假隧道流）、`tests/test_camb.py`、`tests/test_camc_bridge.py`（双
  ferry parity + 真 TCP 假隧道）

## Reverse tunnel + legacy cam serve hub（v0.3.0 / cam serve tunnel_hub）

> 2026-10-19：本节描述迁移期旧实现。`cam serve` 与 `TunnelHub` 是
> legacy 组件；目标架构中的连接、路由和请求记录归 CAM Server，跨
> Server 转发归 Client 连接层，以上位
> [`../cam-client-server-spec.md`](../cam-client-server-spec.md) 为准。

camb 除 ferry 外新增两个 host 侧 daemon，让 legacy cam serve 成为第二个
bridge host（与 desktop hub 并存，§9.2 名片路由天然兼容）：

```
camb --serve [--port N] [--ensure]   # 127.0.0.1:42277,token 认证,
                                     # §10 帧,执行本机 camc argv
camb reverse up <user@server> --http http://server:8080 \
     [--name N] [--token T] [--key-file F] [--ssh-port N]
     [--remote-port N] [--serve-port N]
camb reverse down | status
```

- **reverse maintainer**:`ssh -N -R <port>:127.0.0.1:<serve_port>` 常驻
  （专用 ControlPath `/tmp/camb-ssh-rev-<hash>`，退避 5s→60s)，成功后
  POST `/api/tunnels/register` 注册 server 侧端口，之后每 60s 心跳
  重注册（registry 条目 300s 过期）；状态卡 `~/.cam/camb/reverse.json`。
- **cam serve 侧**(`server.tunnel_hub=true`):`TunnelRegistry`
  （持久化 `tunnels.json`)+ `TunnelHub` worker——发布
  `hubs/cam-serve.json` 名片（covers=活 host）与本地 §10 listener
  （127.0.0.1:42377,ferry 快路径）,2s 轮询 drain mailbox;dispatch
  路径优先级：**活反隧 > 一次性 ssh > 放回 outbox**(attempts+1，
  上限 3);seen-id 幂等 + in-flight 去重；审计 `tunnel-hub-audit.jsonl`。
- **onboard**(`server.tunnel_onboard=true`)：对 machines.json 里
  `tunnel: true` 的 ssh machine 自动部署 camb 到 `~/.cam/bin/camb`、
  装 per-host 反登 key、拉起 `reverse up`。
- camc 侧零改动：`camc <verb> <node>:<ref>` 的 ferry 优先级链自动把
  cam serve 名片当最快路径。
- 注意：host 上 camb 二进制部署到 `~/.cam/bin/camb`,**不是**
  `~/.cam/camb`——后者是 mailbox 目录，与二进制文件路径冲突。
- 测试：`tests/test_camb.py`(serve/reverse 新增)、
  `tests/test_tunnel_hub.py`(registry + dispatch 路径选择，纯 stdlib)。
