# Auto-Confirm Flow

camc monitor 每 ~1s 执行一次循环，Auto-Confirm 是其中 Step 3。

## Monitor 完整循环

```mermaid
flowchart TD
    START([每 ~1s 循环]) --> HEALTH

    HEALTH{Step 1: Health Check<br/>tmux session alive?<br/>每 15s 检查一次}
    HEALTH -- dead --> MARK_DEAD[has_worked? → completed / failed<br/>退出 monitor]
    HEALTH -- alive --> CAPTURE

    CAPTURE[Step 2: Capture<br/>tmux capture-pane<br/>strip 最后一行 status bar<br/>计算 MD5 hash]
    CAPTURE --> SIGNALS

    SIGNALS[Step 2b: Auxiliary Signals<br/>last 5 non-empty lines<br/>busy: ing.… → has_worked=True<br/>done: ed for Xs → fast-track idle]
    SIGNALS --> CONFIRM

    CONFIRM{Step 3: Auto-Confirm<br/>详见下方}
    CONFIRM -- 发送了响应 --> SLEEP_CONFIRM[sleep 0.5s<br/>continue 下一轮]
    CONFIRM -- 未发送 --> STATE

    STATE[Step 4: State Detection<br/>regex on last 2000 chars<br/>→ planning / editing<br/>  testing / committing]
    STATE --> CHANGE

    CHANGE{Step 5: Output Change<br/>hash ≠ prev_hash?}
    CHANGE -- changed --> RESET_IDLE[reset idle timer<br/>last_change = now]
    CHANGE -- same --> IDLE
    RESET_IDLE --> IDLE

    IDLE{Step 6: Idle Detection<br/>has_worked = True?}
    IDLE -- no --> SLEEP
    IDLE -- yes --> IDLE_CHECK

    IDLE_CHECK{hash stable 60s<br/>+ prompt visible?}
    IDLE_CHECK -- yes --> IDLE_CONFIRMED[idle_confirmed = True]
    IDLE_CHECK -- no --> FAST_CHECK

    FAST_CHECK{done_signal<br/>+ bare prompt<br/>+ stable 5s?}
    FAST_CHECK -- yes --> IDLE_CONFIRMED
    FAST_CHECK -- no --> STUCK

    STUCK{Step 6b: Stuck Fallback<br/>hash stable 120s<br/>+ prompt NOT visible?}
    STUCK -- yes --> SEND_STUCK[send '1' to unblock]
    STUCK -- no --> AUTO_EXIT

    IDLE_CONFIRMED --> AUTO_EXIT

    AUTO_EXIT{Step 7: Auto-Exit<br/>idle_confirmed<br/>+ not attached<br/>+ auto_exit enabled?}
    AUTO_EXIT -- yes --> KILL[kill tmux session<br/>mark completed]
    AUTO_EXIT -- no --> SLEEP

    SEND_STUCK --> SLEEP
    SLEEP([sleep 1s → 下一轮])
```

## Step 3: Auto-Confirm 详细流程

```mermaid
flowchart TD
    ENTER([进入 Auto-Confirm]) --> COOLDOWN

    COOLDOWN{Cooldown elapsed?<br/>now - last_confirm ≥ 5s}
    COOLDOWN -- no --> SKIP_CD[/"log: Confirm cooldown Xs remaining"/]
    SKIP_CD --> EXIT_SKIP

    COOLDOWN -- yes --> BUSY

    BUSY{Busy signal?<br/>last 5 lines match<br/>busy_pattern<br/>Claude: ing.…1,3<br/>e.g. Creating… Editing…}
    BUSY -- yes --> SKIP_BUSY[/"agent 在工作，跳过<br/>has_worked = True"/]
    SKIP_BUSY --> EXIT_SKIP

    BUSY -- no --> BARE

    BARE{Bare prompt?<br/>last 5 lines 中<br/>有一行 == ❯ 或 > 或 ›<br/>单独一个 prompt 字符}
    BARE -- yes --> SKIP_BARE[/"agent 在等输入<br/>确认文字是历史残留"/]
    SKIP_BARE --> EXIT_SKIP

    BARE -- no --> MATCH

    MATCH[Match confirm rules<br/>对 last 8 non-empty lines<br/>按顺序匹配 4 条规则<br/>first match wins]
    MATCH --> RULE1

    RULE1{Rule 1<br/>Do you want to proceed<br/>IGNORECASE}
    RULE1 -- match --> SEND1["send '1' no Enter"]
    RULE1 -- no --> RULE2

    RULE2{Rule 2<br/>1. Yes 或 1. Allow<br/>IGNORECASE}
    RULE2 -- match --> SEND2["send '1' no Enter"]
    RULE2 -- no --> RULE3

    RULE3{Rule 3<br/>Allow once/always<br/>IGNORECASE}
    RULE3 -- match --> SEND3["send '1' no Enter"]
    RULE3 -- no --> RULE4

    RULE4{Rule 4<br/>y/n 或 Y/n 或 y/N}
    RULE4 -- match --> SEND4["send 'y' + Enter"]
    RULE4 -- no --> NO_MATCH[/"无匹配，跳过"/]
    NO_MATCH --> EXIT_SKIP

    SEND1 --> UPDATE
    SEND2 --> UPDATE
    SEND3 --> UPDATE
    SEND4 --> UPDATE

    UPDATE[更新状态<br/>last_confirm = now<br/>last_change = now<br/>has_worked = True<br/>idle_confirmed = False<br/><br/>log: Auto-confirm pattern=... → response<br/>event: auto_confirm]
    UPDATE --> SLEEP_HALF[sleep 0.5s]
    SLEEP_HALF --> EXIT_SEND([continue 下一轮])

    EXIT_SKIP([继续 Step 4: State Detection])
```

## Confirm Rules (Claude Code)

| 优先级 | Pattern | 场景 | 响应 | Enter |
|--------|---------|------|------|-------|
| 1 | `Do\s+you\s+want\s+to\s+proceed` | 权限对话框：Do you want to proceed? ❯ 1. Yes 2. No | `1` | ✗ |
| 2 | `1\.\s*(Yes\|Allow)` | 数字菜单：1. Yes / 1. Allow | `1` | ✗ |
| 3 | `Allow\s+(once\|always)` | Claude 4.x+ Ink 选择菜单 | `1` | ✗ |
| 4 | `\(y/n\)\|\[Y/n\]\|\[y/N\]` | 标准 y/n 确认 | `y` | ✓ |

前 3 条都是 Claude Code 的 Ink TUI 组件——单按键选择，不需要 Enter。
第 4 条是标准终端 y/n 提示，需要 Enter 确认。

## 关键设计参数

| 参数 | 值 | 作用 |
|------|-----|------|
| `confirm_cooldown` | 5s | 两次 confirm 之间最小间隔，防止对同一对话框重复发送 |
| `confirm_sleep` | 0.5s | 发送后等待，让 agent 处理输入 |
| `busy_pattern` | `ing[.…]{1,3}` | 匹配 "Creating…" "Editing…" 等进行时动词 |
| `done_pattern` | `ed\s+for\s+\d+[smh]` | 匹配 "Crunched for 36s" 等完成时动词 |
| Confirm 扫描范围 | last 8 non-empty lines | 只看屏幕底部，避免 agent 输出中的文字误触发 |
| Busy/Done 扫描范围 | last 5 non-empty lines | 状态信号在屏幕最底部 |
| Bare prompt 扫描范围 | last 5 non-empty lines | 检测 agent 是否在等待输入 |

## 三道防线

```
Screen captured
     │
     ├─ 防线 1: Cooldown (5s)     → 同一对话框不连续发送
     │
     ├─ 防线 2: Busy signal       → agent 在工作，不是卡在对话框
     │
     ├─ 防线 3: Bare prompt       → agent 在等输入，确认文字是残留
     │
     └─ 通过全部防线 → 匹配 last 8 lines → 发送响应
```
