# CAM Client 本地守护进程 Spec

日期：2026-10-19。开发分支：`cam-tui`。

状态：完整设计要求；本地 Rust daemon 已有 v0.1 实现，当前采用状态快照轮询，支持基础调度、engineering loop 与连接健康检查。实际范围与限制见 [CAM TUI v0.1](cam-tui-v0.1.md)。本文补充 [Client / Server Spec](cam-client-server-spec.md) 与 [CAM TUI Spec](cam-tui-terminal-spec.md)。

## 1. 组件与生命周期

CAM TUI 需要独立于界面运行的本地常驻后台，暂称 `cam-clientd`，使用 Rust 实现。它属于 **CAM Client 的后台组件**，不增加第三种产品角色，也不替代管理 Node 上的 CAM Server。

```text
一个或多个 cam-tui 窗口
        ↕ 本机 IPC：快照、事件、配置和操作
cam-clientd：每台客户端机器、每个用户一份
  ├─ Nodes 连接、重连与 forwarding
  ├─ 跨 Node agent list / status 缓存
  ├─ 定时任务调度
  ├─ Engineering loop 注册与调度
  └─ 分层健康检查与诊断
        ↕ 每个 Node 的独立认证连接
CAM Server A / B / …
  └─ 各自 agent 的权威状态、runtime、操作与监控
```

- 多个 TUI 连接同一 daemon；增加窗口不再增加一套状态轮询、调度器或 forwarding 路径。不同机器上的 daemon 仍是独立 Client，可形成多路径。
- TUI 退出后 daemon 继续更新状态、运行已启用的任务和 loop、承担 forwarding；用户显式停止 daemon 才撤回其连接和路径。停止 daemon 不停止远端 Server 或 agent。
- 首次启动 TUI 可确保 daemon 已启动并连接已有实例；使用本机单实例锁、启动代次和经过验证的 IPC 身份，不能只信 PID 文件。
- 支持前台运行，服务管理器自启动作为可选部署方式；Linux 首版不强制依赖 systemd 或系统 crond。客户端开机重启后的自动启动需部署自启动服务，不能只靠进程自行后台化。
- 本机 IPC 仅允许同用户访问。身份、注册任务、调度记录持久化；锁、socket 和活跃状态按机器隔离，放节点本地，不由共享 Home 把不同机器混成一个实例。
- 界面焦点、viewport、选区与系统剪贴板仍由 TUI 管理。终端流与调度/forwarding 隔离，关闭终端订阅不关闭后台连接。
- 最近访问顺序由同用户本机 IPC `agent.visit` 更新，按 Node＋完整 agent ID 持久化到 Client 数据库，最多保留 1000 条，随快照供多个 UI 共用。它不转发给 Server，也不修改 agent 的权威状态；后台输出与轮询不记录访问。

## 2. 更新 agent list 与 status

daemon 持续同步所有已配置、已授权连接的 Node 的 agent；不是仅更新当前 tab 或已注册 loop 的目标。

- 连接 Server 后获取 agent 快照，再订阅带序号的事件；Server 代次改变或检测到缺口后重同步。仅在 Server 明确支持时使用有界轮询 fallback，不自行扫描远端 tmux、进程或共享 registry。
- 同一份缓存用于 TUI 列表、filter、tab 状态、操作定位与 forwarding 的提交前 resolve；身份为 `(owner_node_id, agent_id)`，缓存记录版本、更新时间和新鲜度。
- Node 连接状态与 agent 状态分开。失联标记 offline/stale，保留带时间的最后状态，不将全部 agent 改成 dead，也不把缓存冒充实时状态。
- 权威 alive/dead、idle/running 与启动阶段仍由 owner Server/实际管理后端判定。daemon 监测状态变化、过期与启动结果，不成为第二个 tool monitor。
- 多个 TUI 获取 daemon 的快照和事件，不各自连接所有 Node 轮询。daemon 重启后能从 Server 重建缓存，旧缓存不直接用于触发任务。
- 迁移期 legacy monitor 保持唯一权威；Server 对未接管 agent 只读导入，原生 Server 接管后再改用集中 monitor。新状态订阅能力需单独实现，现有 Router 尚不提供。

## 3. 定时任务：近似 camc cron

由 daemon 自身调度，不在每个 TUI 中放计时器，也不定期启动 `camc cron tick`。

首版保留现有 camc 常用调度形式：间隔、每日定时、指定时间与延迟一次。每日定时保存明确时区，界面显示下一次触发时间；夏令时和时钟变化须有确定的测试行为。完整 crontab 表达式可另行扩展，不宣称已经支持。

| 动作 | 执行位置与边界 |
| --- | --- |
| 给指定 agent 投递 prompt 或执行已开放操作 | 固定 Node/agent，通过对应 Server 的能力与授权检查执行 |
| 用户显式注册的本机命令 | 仅在 daemon 所在客户端机器执行，保存明确 argv、cwd 和环境覆盖；shell 解释须显式选择 |
| 任意远端 shell 或 Node 级命令 | 不作为现有 agent 白名单的隐式扩展；未单独登记与授权的能力明确不支持 |

任务最少保存稳定 job ID、名称、启用状态、调度、动作、执行位置、超时/重试限制、下次触发和最近结果。支持新增、查看、编辑、启用/暂停、删除，并可查看执行记录、退出码与错误。

调度器只排队，实际执行有超时与并发上限；单个长任务不能阻塞状态更新、其它任务、forwarding 或 TUI。默认同一 job 不重叠运行；离线/重启错过的多轮触发合并处理，恢复时不一次性补发全部积压。

每轮使用稳定的 `(job_id, scheduled_at)` 与操作 ID：先保存触发记录，再提交执行。提交结果未知时查询原操作，不换 ID 重做副作用。本机命令中途崩溃且无法确认结果时记录 unknown，不承诺外部命令恰好执行一次。定时任务的 unknown 与 agent 状态分开。

既有 camc cron 不自动复制或双跑；需要迁移时按任务显式交接执行权，确认原调度已停止再启用新任务。

## 4. 注册 agent loop 与 loop engineering

这里的 engineering loop 与第 2 节的状态同步循环分开：它是用户注册的持续工程任务，参考现有 camc 的 idle-gated prompt loop 与 Goal / Checklist / Verify 工作方式。

### 注册与管理

- 注册 loop ID、名称、固定的目标 Node/agent、负责调度的 daemon 身份、周期、启用状态和 continuation prompt。可关联任务文件/Goal、Checklist 与 Verify；任务内容不解析成一套新的工作流 DSL。
- 支持注册、查看、修改、暂停、恢复、移除，显示下次检查、最近触发、延后原因、失败次数与最近结果。
- daemon 检查全部已知 agent 的状态，对已注册 loop 的目标决定是否触发；未注册 agent 不自动接收 continuation prompt。
- 任务文件和验证证据由 agent 更新；idle、定时器触发或 prompt 成功投递都不代表 Goal 已完成。完成/阻塞必须有明确报告和证据；loop 完成可禁用或移除，不给 agent 增加 completed 状态。

### 调度规则

| 目标情况 | 处理 |
| --- | --- |
| 当前且已确认的 alive / idle，loop 到期且无待处理触发 | 向固定目标提交一次 continuation prompt，记录稳定触发与消息 ID |
| running / initializing 或上一轮尚未明确交付 | 延后，不重复投递，不累积多轮 prompt；单纯忙碌不计为失败 |
| Node offline、状态 stale 或结果未知 | 等待重同步或查询原触发，不假定空闲，不创建新消息重试 |
| dead、目标不存在或无权限 | 显示原因，保留注册供处理；不自动 reboot/创建替代 agent，除非另有明确启用的恢复策略 |
| 明确投递失败 | 按限制重试，达到上限禁用并显示原因；失败与正常忙碌分别计数 |
| 用户暂停/移除 | 停止后续触发，保留可核对的执行记录；不停止 agent |

每个 agent 的待触发 loop 排队串行提交；一次 prompt 后要等待新的后端状态和交付信息，不能用旧的 idle 缓存连续灌入消息。owner Server 在最终交付时需要核验当前状态和调度权，避免缓存检查后 agent 已开始工作仍注入 prompt；这是需要新增并协商的能力，不能把现有普通 message 投递宣称为已具备原子 idle gate。

多 Client 场景中，一个 loop 只能有一个有效调度 owner。Server 需要登记/验证 loop 调度权与稳定触发 ID，显式交接才转移；daemon 重连不能各自宣称接管并同时触发。不具备该能力的 Server 必须明确报告限制，不能承诺跨 daemon 的互斥保证。

Engineering loop 是 daemon 主动发起的业务，允许持久记录自己的计划与触发结果；它与纯 forwarding 分离。纯中继仍不保存等待 agent 回复的业务回程表，消息去重和结果关联仍由两端 Server 负责。

## 5. TUI 入口与本机接口

保持既定四区主界面：

- Agent list/status 订阅本地 daemon；agent 行或详情页提供 loop 的查看与注册入口，显示当前 loop 标记。
- Settings 提供定时任务和 engineering loop 的管理入口，以及 daemon 状态；不新增常驻主界面分区。
- Nodes 的配置与连接操作交给 daemon 执行，多个 TUI 看到一致结果。
- 普通 SSH 新增先在前台 TUI 通过原生 OpenSSH 完成认证，再向 daemon 提交 `node.ssh_add`。复用私有 ControlMaster，后台只用 BatchMode；密码/口令不进 IPC。daemon 自动发现 Server 并配对，验证真实 Node 身份后保存配置。旧 raw/socket 通过 `node.upsert` 保留。
- `terminal.list/create/close/attach` 明确绑定 Node、agent、launch ID 和稳定 pane ID。流缓存按 Node＋agent＋pane 区分，主 pane 固定，辅助 shell 单独关闭。输入权按 pane 租约分配；tmux session 的共享尺寸由主 pane writer 视图控制，避免多个窗口互相 resize。退出 UI 只释放视图，不关闭辅助 shell。
- Agent 操作经 daemon 选定 Node/agent 后交 Server；不能因 daemon 有本机命令调度功能就把远端 agent 操作转成 shell。

本机 IPC 最少覆盖：daemon 状态、Node 配置/连接、agent 缓存快照/事件、具体 agent 操作、job/loop 管理与执行记录、健康快照/事件和按需检查。请求有稳定 ID，事件有 daemon 代次与序号；多个 UI 同时修改配置需要版本检查，避免后写覆盖未知的新配置。

## 6. 分层健康检查

daemon 持续检查后台功能是否可用，向 TUI 提供统一健康快照、变化事件和按需检查入口。每项分别记录范围/Node、是否启用与适用、健康程度、检查时间、最近成功时间、原因与建议动作。健康程度使用 healthy/degraded/unhealthy/unknown，与 agent 的 alive/dead 状态分开；未启用或不适用的功能明确标记，不视为失败。

| 检查项 | 必须检查的内容 |
| --- | --- |
| 本地 daemon | 单实例、IPC 可响应、事件循环/后台任务心跳、持久存储能正常访问；TUI 应能单独显示 daemon 已退出或不可达 |
| Tunnel alive / Server 连接 | SSH 等 transport 的进程/通道状态、协议握手与已验证 Node 身份、最近成功通信和超时；进程存在或端口可连不单独证明功能正常 |
| Forwarding（已启用时） | 两端连接与授权、当前路由代次/TTL、队列增长/背压、最近投递/错误；具备专用 probe 能力时验证端到端消息往返 |
| Agent 状态同步 | 最近快照/事件时间、序号连续性、缓存新鲜度、重同步是否成功；后端提供监控健康能力时检查其 monitor，而非自行启动另一套 |
| 调度器 | tick/任务队列是否推进、worker 是否超时或卡住、存储和触发记录是否可用；当前没有到期任务不算故障 |
| Engineering loop | 注册有效、调度 owner 有效、目标状态新鲜、到期处理与投递记录可追踪；busy 导致的正常延后与投递失败/状态未知分开 |
| 已打开的交互终端 | attach/输入/resize 通道是否存在、连接代次与错误；工具没有新输出不单独视为失联 |

### Tunnel 与 forwarding 的验证边界

- transport 存活、Server 协议可响应和端到端 forwarding 是三种不同证据，分别展示；不能从前一项推断后两项成功。
- 转发检查仅对已启用、已授权且当前需要中继的路径执行。只有一个 Node 或未启用 forwarding 时显示不适用，不为了检查额外建立节点或路由。
- 主动往返 probe 使用协商且已授权的专用 health/echo 接收端，携带 probe ID 与超时，不发送到用户 agent、不向工具注入按键，也不运行任意远端 shell。生产环境不能假定测试用 `test.echo` 已启用。
- 不支持 probe 时显示能力限制，以及最近实际转发的时间/结果；未发生过转发时显示未验证/unknown，不能把配置存在或 socket 写入成功当作端到端健康。
- 周期检查有超时、并发上限和失败退避；多个 TUI 共用 daemon 的检查结果，不按窗口重复探测。按需重查不能绕过权限或形成无界队列。

### 展示与恢复

- `Nodes` 显示每个 Node 的连接/tunnel、路由与 forwarding 健康；`Settings` 显示 daemon、状态同步与调度器健康；agent/loop 详情显示相关原因和最近检查时间。
- 汇总不得隐藏局部故障：一个 Node 断线时其它 Node 继续使用；某项 unknown 不能汇总成全部健康。离线 Node 下的 agent 保留最后状态并标 stale。
- 可自动做有界重连、撤回过期路径、重新订阅与快照重同步；只处理 daemon 自己拥有的连接资源。重复失败给出具体原因与手动重试入口。
- 检查本身不 reboot/kill agent，不清空队列、重装工具或修改配置，不重放结果未知的任务。需要恢复业务时使用已有操作能力和明确策略。
- 默认日志记录组件、Node、时间、错误码和耗时，不记录 token、环境 secret 或消息正文。心跳/健康协议需协商实现，不能宣称当前 Router 已支持所有检查。

## 7. 实施与验收

1. Rust 本地 daemon、单实例与同用户 IPC；两个 TUI 共用一个进程，退出界面后后台保持工作。
2. Nodes 连接/重连、forwarding 和 agent 状态缓存；覆盖断线、过期、重同步与 Server 重启。
3. 定时任务持久化、调度与执行记录；覆盖长任务隔离、同 job 不重叠、进程崩溃未知状态和不补发积压。
4. Loop 注册、idle gate、调度权和触发去重；覆盖 busy、initializing、dead、stale、断线、双 Client 与重复提交。
5. 覆盖健康检查：SSH 进程尚在但通道失效、Server 不响应、路由过期、forwarding 受阻、状态缓存过期和调度 tick 停滞；检测结果不能误报健康，也不能停止 agent。
6. 使用临时真实 agent 验证 continuation prompt 被正确处理，Goal 未达成不误报完成；关闭 TUI 后仍能调度，重开可查看结果。验证健康 probe 不触碰用户 agent，缺少 probe 能力和不适用场景显示正确。
7. 回收专用 daemon、Server、agent、任务、loop 和连接；不修改用户原有 camc cron、loop、agent 或系统 crontab。

v0.1 使用独立状态目录，不自动迁移既有 camc cron/loop。开发测试仅创建并回收临时 daemon、任务与 agent，不修改用户原有调度器。

## SSH Node 版本管理

Add/Edit/显式 Connect 的版本检查、配套 binary＋SDK 同步与启动遵循
[CAM Server 生命周期 spec](cam-server-lifecycle-spec.md)。够新且健康时直接连接，
缺失或过旧时校验并更新；后台断线重连不反复部署。更新保留 Node 身份和 agent。
