# TeaSpirit Group Chat — 权限与认证设计

## 背景

TeaSpirit 运行在一台机器上，使用宿主用户（如 hren）的 CLI token 访问 Microsoft Graph API（日历、邮件、Teams）和 NVIDIA 内部服务（NVBugs、Helios、Confluence、Glean）。

在 **Personal Chat** 中，所有请求都是宿主用户自己的，没有权限问题。

在 **Group Chat** 中，不同用户会提问，但 TeaSpirit 只有宿主用户的 token。这造成了权限边界。

## 消息格式

- Personal Chat: `text`（直接是内容）
- Group Chat: `[Name] text`（名字前缀标识发言人）

TeaSpirit 需要解析 `[Name]` 识别发言人，按人维护状态。

## 权限矩阵

### 组织级数据（任何人都能查）

| 服务 | CLI 工具 | Auth 类型 | 说明 |
|------|---------|-----------|------|
| NVBugs | nvbugs-cli | API token | 公开查询，不限用户 |
| Helios 员工目录 | helios-cli | API token | 组织架构、manager chain、group membership |
| Confluence | confluence-cli | PAT | 组织级 wiki 内容 |
| Glean | glean-cli | MaaS OAuth | 跨数据源企业搜索（P4、GitLab、SharePoint、Confluence、Google Drive） |
| SharePoint | sharepoint-cli | Entra ID | 文件搜索（受文档权限限制） |
| Google Drive | gdrive-cli | MCP | 文件搜索（受文档权限限制） |
| Redmine | redmine-cli | API token | Issue/ticket 追踪，公开查询 |
| Atlassian (Jira) | atlassian-cli | PAT | Jira issue、sprint、board 管理 |
| Smartsheet | smartsheet-cli | API token | 项目管理数据（sheet、row、workspace） |
| nSpect | nspect-cli | API (public) | 产品安全合规检查 |
| Salesforce | sf-cli (via skill) | — | CRM 数据：accounts、leads、opportunities、cases、campaigns |
| Databricks | databricks-cli | MCP | Unity Catalog、SQL 查询、Genie AI |
| PagerDuty | pagerduty-cli | MCP | 事件管理、on-call 排班、escalation |
| Starfleet | starfleet-cli | SCIM API | 身份管理、OAuth 客户端、角色分配 |
| ITSS | itss-cli | API token | VM、DNS、存储、LDAP、service account 管理 |
| Distribution Lists | dl-cli | Entra ID | Microsoft 365 邮件组成员查询 |

**结论**：Group chat 中任何人问这类问题，都可以正常回答。

### 个人数据（受 token 限制）

| 服务 | CLI 工具 | Auth 类型 | 宿主用户 | 其他用户 | 备注 |
|------|---------|-----------|---------|---------|------|
| 日历 | calendar-cli | Entra ID | ✅ 完全访问 | ⚠️ 仅公开日历 | `--user <email>` 查共享日历，需对方开启 Calendars.Read.Shared |
| 邮件 | outlook-cli | Entra ID | ✅ 完全访问 | ❌ 无权限 | Graph API 不支持跨用户邮件读取 |
| Teams 消息 | teams-cli | Entra ID | ⚠️ blocked | ❌ 无权限 | 只能读宿主加入的 chat/channel |
| OneDrive | onedrive-cli | Entra ID | ⚠️ blocked | ❌ 无权限 | 个人网盘，Admin consent pending |
| OneNote | onenote-cli | Entra ID | ✅ 完全访问 | ❌ 无权限 | 个人笔记本 |
| 会议录音 | transcript-cli | Entra ID | ✅ 完全访问 | ⚠️ 仅共同参加的会议 | 宿主必须是参会者 |
| SAP Concur 报销 | concur-cli | API token | ⚠️ blocked | ❌ 无权限 | Auth pending，个人报销数据 |
| 空闲/忙碌查询 | calendar-cli schedule | Entra ID | ✅ 完全访问 | ⚠️ 有限 | 可查其他人 free/busy（不含会议详情） |

### 当前不可用的工具（Auth blocked）

| 工具 | 状态 | 替代方案 |
|------|------|---------|
| teams-cli | Auth pending | 直接用 Teams app |
| onedrive-cli | Admin consent pending | 用 OneDrive web |
| concur-cli | Auth pending | 用 SAP Concur web portal |

### CAM/CAMC 操作

| 操作 | 权限 | 备注 |
|------|------|------|
| 列出所有 agent | ✅ 所有人可看 | `camc --json list` 返回所有 agent |
| Attach/capture agent | ✅ 所有人可用 | 按 agent ID，不区分用户 |
| Send input 到 agent | ✅ 所有人可用 | 需要注意并发冲突 |
| Run 新 agent | ✅ 所有人可用 | 以宿主用户身份在远程机器执行 |
| Stop/kill agent | ⚠️ 需谨慎 | 建议只允许 agent 创建者或宿主用户操作 |
| Heal/upgrade | ✅ 宿主用户权限 | 管理操作 |

## 应对策略

### 当其他用户问个人数据时

```
[Jason Xiong] 我明天有什么会？
```

1. **先尝试** `calendar-cli find --user jasonx@nvidia.com`
2. **如果成功** → 返回结果
3. **如果失败**（权限不足）→ 回复：
   > "我没有权限访问你的日历。你可以让 IT 开启日历共享，或者我可以帮你查组织级的信息（bug、文档、员工目录等）。"

### 当其他用户问组织级数据时

```
[Jason Xiong] FN-519 是什么 feature？
```

正常回答，不涉及个人权限。

### 当其他用户要操作 agent 时

```
[Jason Xiong] 帮我看一下 agent aicli 的输出
```

正常操作 — agent 访问不区分用户身份。

```
[Jason Xiong] stop agent falcon
```

建议确认：
> "falcon 是 Huailu 创建的 agent，确认要停止吗？"

## 未来扩展：多用户 Token

如果需要完整支持多用户个人数据访问：

1. **每个用户各自授权** — 每人在 TeaSpirit 里绑定自己的 Microsoft Graph token
2. **Token 存储** — `~/.cam/teaspirit/tokens/<user>.json`
3. **请求路由** — 根据 `[Name]` 匹配 token，用对应 token 调用 CLI
4. **Fallback** — 没有 token 的用户退回到宿主 token（只能查公开数据）

这需要实现一个 token 管理模块，类似 `token-sync` 但面向多用户场景。

## 安全注意事项

- 宿主用户的 token **不应该**被用来访问其他人的私人数据（即使技术上 Graph API 可能允许 delegate access）
- Group chat 中的所有操作日志应记录发言人身份
- Stop/kill agent 等破坏性操作应有确认机制
- 避免在 group chat 中展示宿主用户的敏感邮件内容
