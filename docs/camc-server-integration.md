# camc 与新 CAM Server 的接入

日期：2026-10-19

当前已在 [components/cam-router](../components/cam-router/README.md) 实现 Rust CAM Server Router、持久队列、去重与独立 Client forwarding 参考组件，并接通 camc 本机 IPC 和原生执行适配层。多 Server/Client 已通过单机独立进程测试；物理跨机器 SSH、Desktop/Electron 与 Mobile 的接入尚未验收。Python 包现有 `cam-server` 入口仍是旧实现；迁移期须用新 Rust 二进制的绝对路径，不能混用。

## 启用与命令

Rust TUI v0.1 的本地 Client daemon 直接复用此 Server/SDK，使用额外的受授权管理通道；范围见 [TUI 版本说明](cam-tui-v0.1.md)。Client grant 的 `management` 默认 false，启用前必须明确配置。Server 只向获准 Client 通告 agent 快照、操作、tmux control 和 loop 能力，Client 未协商到的能力不会发送到旧 Router；既有 forwarding-only 连接继续可用。

Agent 快照只返回必要元数据并过滤其它主机记录；状态仍归 legacy runtime/monitor。TUI 普通 prompt/loop 使用 `agent.send`，同一操作 ID 绑定首次业务信封并复用已保存结果，不因重新生成 expiry 而重复执行。终端控制经独立逻辑帧传输，由 Server 验证 agent/pane 与连接所有权，不解析用户提供的任意远端 shell 命令。

只有设置 `CAM_SERVER_SOCKET`，或存在本用户所有、不可被其它用户写入的 `~/.cam/server/endpoint-<hostname>.json`（`{"socket":"/节点本地路径/server.sock"}`）时，camc 才启用新路径。实际 socket 必须属于当前用户。endpoint 文件用于发现；socket、锁和去重数据库必须由 Server 放到节点本地，并按 Node 隔离，不能让共享 Home 下的不同机器共用同一个运行实例。

以下示例需要先启动新的 Rust Server 并配置 Client 通道；camc 不会自行启动它：

```sh
export CAM_SERVER_SOCKET=/节点本地路径/server.sock
camc capture node-b:agent-id --lines 100
camc --json status node-b:agent-id
camc send node-b:agent-id --text 'hello'
camc key node-b:agent-id --key Enter
camc run --node node-b --tool codex --path /共享工作目录
camc reboot node-b:agent-id
camc stop node-b:agent-id
camc kill node-b:agent-id
camc rm node-b:agent-id
camc msg send node-b:agent-id -t '请检查' --no-wait
camc msg wait message-id --timeout 600
camc msg reply message-id -t '检查结果'
```

白名单只有 msg/send/capture/run/stop/rm/kill/reboot/key/status。新路径不支持远端 list。未带 Node 的已有本机目标继续走原命令；本机未命中才请求远端定位，有歧义则报错。显式 `Node:agent` 固定 Node，不被本机同名 agent 截走。run 必须通过 `--node` 指定远端；未指定时仍在本机启动。

未配置 endpoint 时，原本机、tunnel 和 bridge 行为保留；`run --node` 明确失败，防止误在本机启动。新 Server 路径一旦被选中，错误或超时不会改走旧路径重做操作。结果未知不等于未执行，CLI 会返回非零。

`CAM_SERVER_TIMEOUT` 可覆盖本机连接/结果等待期限，单位为秒，范围大于 0 且不超过 86400；默认 30 秒，阻塞 `msg send` 默认沿用其 `--timeout`（600 秒）。超时不会取消远端已执行的操作。send 的 `--file/--stdin` 在来源机器读成文本；run 的工作目录转为绝对路径，其余目录、工具与 launcher 配置在目标机器解释，原 autorun/launcher 策略不变。

## 本机 IPC 契约

传输使用 AF_UNIX stream，每帧是一个 UTF-8 JSON 对象，以换行结束；单帧上限 4 MiB，拒绝非法 JSON 和非有限数字。Server 必须认证本机调用者及远端来源，检查目标、权限和能力；客户端的 socket 所有者检查不替代 Server 的授权。

每次 CLI 调用有自己的本机连接；Server 可以并发处理它们，不能在 Client forwarding 通道上等待业务结果。

1. camc 发送 `{"type":"hello","version":1,"role":"camc"}`；Server 返回 `{"type":"hello","version":1,"node_id":"node-a"}`。
2. camc 发送 `{"type":"resolve","node":"node-b","agent":"name-or-id"}`；Server 返回 `{"type":"resolved","node_id":"node-b","agent_id":"full-id"}`，或明确错误。run 的 agent 为 null，定位 Node 后由来源 camc 分配一次新 agent ID。
3. 定位发生在构造业务消息前。远端名称查找可使用 Client 已有 `(node,name,id,status)` 缓存；缓存不唯一、过期或无法确认时明确失败，不广播执行业务。不能因缓存显示 dead 就忽略 status/rm/reboot 目标。Client 转发已经构造好的消息时不改内容。
4. camc 发送普通 `type=message`、version=1 信封，含独立 message_id、source、target、operation、payload.args 与 expires_at。具体 agent 操作必须有 Node 和 agent ID；结构化参数来自现有 CLI parser。
5. Server 持久接受并路由请求，保存本机等待关联。收到 `operation.result` 后核对来源 Node/agent 与 reply_to，再把结果帧交给原调用方。payload 的 stdout/stderr 为字符串，exit_code 为 0–255 整数，status 为 ok/error/unknown；pending 可作为中间状态。camc 不包装输出，按返回退出码结束。
6. `msg send` 的投递结果带 `payload.kind=delivery`。阻塞 send 继续等真正业务回复；来源 Server 负责将后到的独立 reply 与本机等待者关联为 kind=reply 的结果，也要在 CLI 已退出时保存回复。投递成功不能充当 agent 回答。
7. `msg reply` 是新消息，沿当前路径独立发送。camc 使用本机控制帧 `{"type":"publish","message":{...}}` 提交异步回复；Server 只有持久接收后才返回 `{"type":"accepted","message_id":"..."}`。这个短确认不要求 Client 等待业务回复。没有来源 agent 的原 CLI 调用，回复投到来源 Node 的 `receiver_id=operation.results`。
8. `msg wait` 使用本机控制帧 `{"type":"wait","message_id":"...","timeout":600}` 查询原消息的持久结果，不重新提交业务。等待期间其它连接继续执行 capture/status 等操作；原来的 CLI 退出或 Client 断线不删除结果。

协议的信封校验、编码和操作名称位于 `src/camc_pkg/server_protocol.py`。客户端位于 `src/camc_pkg/server_client.py`，同时包含在 standalone 构建中。本机 msg 的 thread ID 保持稳定，跨 Node 每个 turn 有独立 message_id/reply_to；私有 context 按 Node 隔离保存映射，reply 通过本机 Server 的真实 Node 身份选取 context，现有账本使用递增 seq，并以线程锁避免共享 Home 重复记入同一 turn。reply 输出 MESSAGE_ID 与 THREAD_ID；`msg read <THREAD_ID>` 读取本机已保存的对话。此映射不是中继 Client 的业务回程表。

当 tool 由 launcher 放到另一台 LSF 执行主机时，tool 内的 camc 回调如何到达管理 Node，仍需 Server 集成阶段接通。AF_UNIX 路径不能跨机器使用；发现新协议 context 但没有对应本机 Server 时，reply 明确失败，不冒充已通过旧本机账本完成跨 Node 投递。当前测试未覆盖 LSF tool 内的跨机器回调。

## Server 的本机执行适配

Server 的可选 backend 配置以绝对 Python 和模块路径启动 `camc_pkg.server_worker`，使用私有 stdio 调用 `CamOperationReceiver(node_id, state_path).execute(message)`。这是 Server 管理的 SDK worker，没有监听端口或独立守护进程；认证、授权、目标定位、持久队列与路由仍属于 Rust Server。业务不经 camc CLI argv。Python spawn worker 的嵌入入口采用正常的 `if __name__ == "__main__"` 启动保护。纯 Router 可完全不安装 Python，agent 操作在未注册本机 backend 时明确返回 UNSUPPORTED_OPERATION。

适配层直接调用现有 cmd_run/cmd_migrate 等后端函数，不经 CLI argv、shell 拼接或 exec camc CLI 派发业务。每个操作使用独立 worker 捕获 stdout/stderr/SystemExit，避免污染并发 Server 输出；runtime 原有的启动 worker、monitor 和 tool 执行保持。run 返回创建/initializing 的现有结果，不冒充工具已 started；后续状态仍由原 runtime/monitor 记录。

SQLite 在执行前持久保留 `(已认证来源 Node,message_id,内容摘要)`；同消息的完成结果可跨适配层重启复用，同 ID 改内容返回 MESSAGE_CONFLICT。并发、超时或进程中断导致结果未知时不重做副作用。run 的新 ID 冲突不覆盖已有 agent，其他操作必须精确匹配本机已有 ID，拒绝共享 registry 中属于其它 hostname 的记录（旧记录缺少 hostname 时沿用本机兼容策略）；payload 不能改写信封中的目标。操作输出超过帧上限明确失败，不静默截断。

测试在隔离 Home、Unix socket 和真实 tmux 上运行，使用可回收的 `/bin/cat` 工具验证十项操作，并覆盖消息映射、去重、并发、超时、退出码及临时 standalone。Router 另有真实独立 Server–Client–Server 进程测试，覆盖双 Client、三 Node、stdio transport、双向多轮 mailbox、换路径回复、崩溃未知状态和路由 TTL。物理跨主机 SSH/Electron 转发仍需按 [Forwarding Spec](cam-client-forwarding-spec.md) 分别验收；本次未修改 Desktop 连接层。
