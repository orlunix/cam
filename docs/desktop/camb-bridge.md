# camb — 跨 node fleet bridge 设计笔记

状态：v1 已实现（2026-09-07）。本文是设计契约，不是教程。

总纲与 camc 归并方案见 [`fleet-bridge-plan.md`](./fleet-bridge-plan.md)；
协议规范（schema/语义权威）见 [`bridge-protocol-v1.md`](./bridge-protocol-v1.md)。
本文是 v1 实现侧的现状笔记，冲突时以 spec 为准。

## 一句话

bridge 开关打开的 node 上，agent 可以用 `camb`（camc 对齐的薄 CLI）
查看整个舰队并向其它 bridge node 上的 agent 投递操作；所有逻辑在
camui hub（embedded-hub.cjs bridge workers）执行，node 上**零凭据**。

## 接口哲学：camc 是标准，camb 是过渡壳

- camb 不做任何 verb 校验、目标解析、舰队知识——只把 camc 风格的
  argv 原样打包进文件请求，打印响应。唯一客户端逻辑：
  `send --file/--stdin` 解析成 `--text`（hub 只见 `--text`）
- 将来归并 camc（spec 交 camc 所有方）：`camc list -a` = 本设计的
  fleet list；`camc <verb> <remote-agent>` 先本地后远程解析 = 本设计
  hub 侧的解析顺序。归并后 camb 可退役
- **归并第一步已落地（camc 1.2.139，2026-09-30）**：
  `src/camc_pkg/bridge.py` 吸收全部 ferry 行为（mailbox-only：
  `--hub-drain`、outbox 原子发布、inbox 轮询、exit 0/1/2、
  `send --file/--stdin` → `--text`），`cli.py` 在 argparse 前做
  本地优先路由（`list -a` / 显式 `node:ref` / 本地 miss 且信箱已部署
  才 ferry）。hub 侧零改动即可继续用 camb drain；把 drain 命令切到
  `camc --hub-drain` 是后续独立的 hub 改动。camb 保留至切换完成
- 第二步扩容路径：targeted verbs 是 `_execCamcOnContext` 原样转发，
  加 `run/stop/…` = 只扩 allowlist，转发通道零改动

## 文件协议（camb ↔ hub 之间没有网络协议）

```
node:  ~/.cam/camb/outbox/<uuid>.json   camb 原子写（.tmp → rename）
       ~/.cam/camb/inbox/<uuid>.json    hub writeRemoteFile 写，camb 读后删
hub:   每个 bridge-enabled context 一个 worker，2s 轮询
       `~/.cam/camb --hub-drain`（原子打印+删除 outbox，空 → []）
```

请求 `{id, argv, from:{agent}, ts}`；响应 `{id, ok, text?, error?,
candidates?}`。`from.agent` 由 camb 解析 `$TMUX` socket 名
（`cam-<id>.sock`）得出，best-effort。

hub 不可达/未开 bridge：camb 30s 超时（`CAMB_TIMEOUT_MS` 可调），
exit 2 + 明确报错。worker 失败指数退避 2s→60s，成功复位；
drain 失败会清 camb ready-cache 自愈重部署。

## 为什么轮询而不是 SSH 反向隧道

勘察结论（ssh-transport.cjs）：exec pool 连接按策略随时被 drop
（op 超时 580 / sftp 错误 741 / 重试路径 666），forwardIn 隧道会被
无关流量静默掐断且无法重建（重连 lazy + 无状态）；term pool 的 drop
波及其他 terminal（1005/1026）。正确做法需要第三专用 pool +
'tcp connection' pipe + 断线重建——连接层手术，风险远大于收益。
异步消息语义下 2-3s 延迟可接受；要实时再说，CLI/文件协议不变。

## 信任模型

- 开关 per-node：context 记录 `machine.bridge`（默认 false），
  UI 在 Nodes 页 Add/Edit Host 表单（nodes-mode.js + desktop.html）
- **看全部，控对称**：`list` 无 consent 聚合全舰队；
  targeted verbs（status/capture/send/key/msg）要求目标 node 也开
  bridge，否则拒绝且不产生任何到目标的流量
- 目标解析 `_bridgeResolveTarget`：`node:ref` 精确；裸 ref 先来源
  node 后全舰队；歧义返回 candidates；转发前剥掉 `node:` 前缀
- 审计：每次 dispatch（含拒绝）一行 JSON 落 `<dataDir>/bus-audit.jsonl`
  （ts/from_ctx/from_agent/argv/target_ctx/ok/error），
  另 pushLog 摘要

## 运维注意

- camb 部署走 `_ensureRemoteCamb`（skillm/camc 同款
  probe→版本比较→writeRemoteFile→chmod/mv→verify→ready-cache），
  仅向 bridge 打开的 node 推送；bridge 关闭不卸载（幂等，重开即用）
- MSI 打包：`package.json` extraResources `resources/camb/camb → camb/camb`；
  容器：`apps/cam-container/Dockerfile` COPY 同名路径
- `msg send` 经 bridge 时 hub 侧 exec 上限 120s（BRIDGE_MSG_TIMEOUT_MS）；
  camc msg 默认等 600s——长等请让发送方加 `--no-wait` 或 `--timeout`
- 测试：`apps/cam-desktop/test/hub-bridge.test.cjs`（69 项，ssh override
  桩）、`tests/test_camb.py`（17 项，pytest 风格 + `__main__` 直跑）
