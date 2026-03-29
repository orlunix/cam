# Monitor State Machine & Decision Flow

> Date: 2026-03-27 | Covers: unified "1"+BSpace confirm/probe, state detection, auto-exit

## 1. Core Idea: Unified "1" + BSpace

Auto-confirm and idle probe share the same atomic operation: **send "1", observe, BSpace**.

```mermaid
flowchart LR
    SEND["Send '1'<br/>(no Enter)"] --> WAIT[Wait 0.3s] --> RECAP[Recapture<br/>output]

    RECAP --> CMP{Compare with<br/>baseline}

    CMP -->|"'1' echoed<br/>on last line"| COMPLETED["completed<br/>Agent at prompt<br/>(echo on)"]
    CMP -->|"Output changed<br/>'1' not visible"| CONFIRMED["confirmed<br/>Dialog consumed it<br/>(auto-confirm worked)"]
    CMP -->|Output unchanged| BUSY["busy<br/>Agent working<br/>(raw mode, no echo)"]

    COMPLETED --> BS["BSpace<br/>(remove '1')"]
    CONFIRMED --> BS
    BUSY --> BS

    style SEND fill:#2196F3,color:#fff
    style BS fill:#9C27B0,color:#fff
    style COMPLETED fill:#4CAF50,color:#fff
    style CONFIRMED fill:#FF9800,color:#fff
    style BUSY fill:#607D8B,color:#fff
```

**Why "1"?** Claude's permission menus show `1. Yes` / `1. Allow` as option 1. Sending "1" selects it. If no menu is showing and agent is at prompt, "1" just echoes and gets cleaned up by BSpace. If agent is busy in raw mode, "1" is silently dropped.

## 2. Monitor Main Loop (1s cycle)

```mermaid
flowchart TD
    START([Monitor Start]) --> CAPTURE[Capture tmux output]

    CAPTURE --> EMPTY{Output empty?}
    EMPTY -->|Yes| EMPTY_COUNT[empty_count++]
    EMPTY_COUNT --> EMPTY_CHECK{count >= 3<br/>AND session dead?}
    EMPTY_CHECK -->|Yes| EXIT_DONE([Exit: completed/failed])
    EMPTY_CHECK -->|No| SLEEP[sleep 1s]

    EMPTY -->|No| HEALTH{Health check due?<br/>every 15s}
    HEALTH -->|Yes| SESSION_ALIVE{tmux session<br/>exists?}
    SESSION_ALIVE -->|No| EXIT_GONE([Exit: Session exited])
    SESSION_ALIVE -->|Yes| HASH

    HEALTH -->|No| HASH[Compute output MD5]
    HASH --> CHANGED{Output changed?}
    CHANGED -->|Yes| RESET["probe_idle_count = 0<br/>last_change = now"]
    CHANGED -->|No| UNCHANGED[" "]

    RESET --> CONFIRM_CHECK
    UNCHANGED --> CONFIRM_CHECK

    CONFIRM_CHECK{Confirm pattern<br/>matched AND<br/>cooldown 5s passed?}
    CONFIRM_CHECK -->|Yes| SEND_ONE_CONFIRM["_send_one()<br/>Send '1' → observe → BSpace"]
    SEND_ONE_CONFIRM --> CONFIRM_RESULT{Result?}
    CONFIRM_RESULT -->|confirmed| CONFIRM_OK["Dialog consumed '1'<br/>Confirmation succeeded"]
    CONFIRM_RESULT -->|completed| CONFIRM_ECHO["'1' echoed — not a dialog<br/>(false positive pattern match)"]
    CONFIRM_RESULT -->|busy/error| CONFIRM_MISS["Agent busy, try later"]
    CONFIRM_OK --> SLEEP
    CONFIRM_ECHO --> SLEEP
    CONFIRM_MISS --> SLEEP

    CONFIRM_CHECK -->|No| STATE_DETECT

    STATE_DETECT[Detect state from<br/>output patterns] --> STATE_CHANGED{State changed?}
    STATE_CHANGED -->|Yes| UPDATE_STATE[Update: planning/<br/>editing/testing/...]
    STATE_CHANGED -->|No| COMPLETION

    UPDATE_STATE --> COMPLETION

    COMPLETION{Output stable >= 3s<br/>AND completion<br/>detected?}
    COMPLETION -->|No| SLEEP
    COMPLETION -->|Yes| PROBE_GATE

    PROBE_GATE{Stable >= 10s<br/>AND probe cooldown<br/>20s passed?}
    PROBE_GATE -->|No| SLEEP
    PROBE_GATE -->|Yes| SEND_ONE_PROBE["_send_one()<br/>Send '1' → observe → BSpace"]

    SEND_ONE_PROBE --> PROBE_RESULT{Result?}
    PROBE_RESULT -->|completed| INC["probe_idle_count++"]
    PROBE_RESULT -->|confirmed| RESET_PROBE["probe_idle_count = 0<br/>last_change = now<br/>(dialog was hiding!)"]
    PROBE_RESULT -->|busy/error| RESET_IDLE["probe_idle_count = 0"]

    RESET_PROBE --> SLEEP
    RESET_IDLE --> SLEEP

    INC --> THRESHOLD{probe_idle_count<br/>>= 2?}
    THRESHOLD -->|No| SLEEP
    THRESHOLD -->|Yes| IDLE_CONFIRMED["Agent truly idle"]

    IDLE_CONFIRMED --> AUTO_EXIT{auto_exit?}
    AUTO_EXIT -->|No| SET_IDLE[Set state = idle]
    SET_IDLE --> SLEEP
    AUTO_EXIT -->|Yes| DO_EXIT[Kill or /exit]
    DO_EXIT --> EXIT_DONE

    SLEEP --> CAPTURE

    style START fill:#4CAF50,color:#fff
    style EXIT_DONE fill:#FF5722,color:#fff
    style EXIT_GONE fill:#FF5722,color:#fff
    style SEND_ONE_CONFIRM fill:#2196F3,color:#fff
    style SEND_ONE_PROBE fill:#2196F3,color:#fff
    style IDLE_CONFIRMED fill:#9C27B0,color:#fff
    style CONFIRM_OK fill:#FF9800,color:#fff
```

## 3. Auto-Confirm Trigger Patterns

These patterns trigger `_send_one()` (send "1" + BSpace):

```mermaid
flowchart LR
    OUTPUT["Last 500 chars<br/>of output"] --> RULES

    subgraph RULES ["Confirm Rules — checked in order"]
        R1["Trust dialog<br/>/Enter to confirm|Select.*to/"]
        R2["Do you want to proceed?"]
        R3["1. Yes / 1. Allow"]
        R4["Allow once/always"]
        R5["(y/n) / [Y/n]"]
    end

    R1 -->|match| SEND["_send_one()<br/>'1' + BSpace"]
    R2 -->|match| SEND
    R3 -->|match| SEND
    R4 -->|match| SEND
    R5 -->|match| SEND

    R1 -->|no match| R2 --> |no match| R3 --> |no match| R4 --> |no match| R5
    R5 -->|no match| NONE[No confirm needed]

    style SEND fill:#2196F3,color:#fff
```

## 4. Agent State Detection

```mermaid
stateDiagram-v2
    [*] --> initializing: Agent launched

    initializing --> planning: "thinking|analyzing|reading"
    initializing --> editing: "Write|Edit|creating file"

    planning --> editing: "Write|Edit|creating file"
    planning --> testing: "Compiling|Building|Running tests"
    planning --> committing: "git commit|Committing"

    editing --> testing: "Compiling|Building|Running tests"
    editing --> planning: "thinking|analyzing|reading"
    editing --> committing: "git commit|Committing"

    testing --> editing: "Write|Edit|creating file"
    testing --> planning: "thinking|analyzing|reading"
    testing --> committing: "git commit|Committing"

    committing --> planning: "thinking|analyzing|reading"
    committing --> editing: "Write|Edit|creating file"

    planning --> idle: Probe confirmed x2
    editing --> idle: Probe confirmed x2
    testing --> idle: Probe confirmed x2
    committing --> idle: Probe confirmed x2
    initializing --> idle: Probe confirmed x2

    note right of idle
        Agent at prompt.
        auto_exit may kill session.
    end note
```

## 5. Completion Detection (prompt_count strategy)

```mermaid
flowchart TD
    OUTPUT[Terminal output] --> STRIP[Strip ANSI codes]
    STRIP --> COUNT["Count prompt lines<br/>matching /^[❯>]/"]

    COUNT --> CHECK_CONFIRM{Any confirm<br/>pattern in output?}
    CHECK_CONFIRM -->|Yes| NOT_DONE["Not completed<br/>(still in dialog)"]

    CHECK_CONFIRM -->|No| THRESHOLD{count >= 2?}
    THRESHOLD -->|Yes| DONE[Completion detected]
    THRESHOLD -->|No| FALLBACK{"count == 1 AND<br/>summary line?<br/>(✻ verb for time)"}
    FALLBACK -->|Yes| DONE
    FALLBACK -->|No| NOT_DONE

    DONE --> STABLE{"Output stable<br/>>= 3s?"}
    STABLE -->|No| WAIT[Wait...]
    STABLE -->|Yes| PROBE["Enter Probe Phase<br/>_send_one() x2"]

    style DONE fill:#4CAF50,color:#fff
    style NOT_DONE fill:#607D8B,color:#fff
    style PROBE fill:#2196F3,color:#fff
```

## 6. Full Lifecycle Timeline

```mermaid
sequenceDiagram
    participant M as Monitor (1s loop)
    participant T as tmux session
    participant A as Agent (Claude)

    Note over M,A: === Agent Working ===

    loop Every 1s
        M->>T: capture-pane
        T-->>M: output (hash changed)
        M->>M: detect_state → editing
        M->>M: should_auto_confirm → no match
    end

    Note over M,A: === Permission Dialog Appears ===

    M->>T: capture-pane
    T-->>M: "1. Allow once  2. Deny"
    M->>M: should_auto_confirm → match!

    rect rgb(33, 150, 243, 0.1)
        Note over M,T: _send_one()
        M->>T: send-keys "1" (no Enter)
        Note over M: Wait 0.3s
        M->>T: capture-pane
        T-->>M: output changed (dialog dismissed)
        M->>M: Result: confirmed ✓
        M->>T: send-keys BSpace (cleanup)
    end

    Note over M,A: === Agent Finishes, Returns to Prompt ===

    loop Output stable 3s
        M->>T: capture-pane
        T-->>M: "❯ " (hash unchanged)
        M->>M: detect_completion → 2 prompts → done!
    end

    Note over M,A: === Probe Phase (after 10s stable) ===

    rect rgb(33, 150, 243, 0.1)
        Note over M,T: _send_one() — Probe #1
        M->>T: send-keys "1" (no Enter)
        Note over M: Wait 0.3s
        M->>T: capture-pane
        T-->>M: "❯ 1" (echoed!)
        M->>M: Result: completed → idle_count=1
        M->>T: send-keys BSpace (cleanup)
    end

    Note over M: Wait 20s (probe_cooldown)

    rect rgb(33, 150, 243, 0.1)
        Note over M,T: _send_one() — Probe #2
        M->>T: send-keys "1" (no Enter)
        Note over M: Wait 0.3s
        M->>T: capture-pane
        T-->>M: "❯ 1" (echoed again!)
        M->>M: Result: completed → idle_count=2 ≥ threshold
        M->>T: send-keys BSpace (cleanup)
    end

    Note over M,A: === Idle Confirmed ===

    alt auto_exit = true
        M->>T: kill-session
        M->>M: status=completed, reason=auto-exit
    else auto_exit = false
        M->>M: state=idle (keep running)
    end
```

## 7. Timing Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `confirm_cooldown` | 5s | Min interval between auto-confirm attempts |
| `confirm_sleep` | 0.5s | Sleep after confirm before next loop |
| `completion_stable` | 3s | Output must be stable before checking completion |
| `probe_stable` | 10s | Stable time before first probe |
| `probe_cooldown` | 20s | Min interval between probes |
| `probe_wait` | 0.3s | Wait after sending "1" before recapture |
| `probe_idle_threshold` | 2 | Consecutive "completed" probes to confirm idle |
| `health_check_interval` | 15s | Session-alive check interval |
| `empty_threshold` | 3 | Consecutive empty captures before death check |

### Time from task done to idle confirmed (worst case)

```
Task finishes, output stabilizes              0s
  ├─ completion_stable (3s)                   3s   ← completion detected
  ├─ probe_stable (10s)                      10s   ← first probe eligible
  ├─ _send_one() #1 → completed             10.5s
  ├─ probe_cooldown (20s)                    30.5s  ← second probe eligible
  ├─ _send_one() #2 → completed             31.0s  ← idle confirmed!
  └─ auto_exit (if enabled)                  ~31s
```
