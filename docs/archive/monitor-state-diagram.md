# Monitor State Machine & Decision Flow

> Date: 2026-04-01 | v2: Single-probe design with attachment awareness

## 1. Core Idea: Unified "1" + BSpace

Auto-confirm and idle probe share the same atomic operation: **send "1", observe, BSpace**.

```mermaid
flowchart LR
    SEND["Send '1'<br/>(no Enter)"] --> WAIT[Wait 0.3s] --> RECAP[Recapture<br/>output]

    RECAP --> CMP{Compare with<br/>baseline}

    CMP -->|"'1' echoed<br/>on screen"| IDLE["idle<br/>Agent at prompt<br/>(echo on)"]
    CMP -->|"Output changed<br/>'1' not visible"| CONFIRMED["confirmed<br/>Dialog consumed it<br/>(auto-confirm worked)"]
    CMP -->|Output unchanged| BUSY["busy<br/>Agent working<br/>(raw mode, no echo)"]

    IDLE --> BS["BSpace<br/>(remove '1')"]
    CONFIRMED --> NOBS["No BSpace needed<br/>(dialog consumed '1')"]
    BUSY --> BS

    style SEND fill:#2196F3,color:#fff
    style BS fill:#9C27B0,color:#fff
    style NOBS fill:#9C27B0,color:#fff
    style IDLE fill:#4CAF50,color:#fff
    style CONFIRMED fill:#FF9800,color:#fff
    style BUSY fill:#607D8B,color:#fff
```

**Why "1"?** Claude's permission menus show `1. Yes` / `1. Allow` as option 1. Sending "1" selects it. If no menu is showing and agent is at prompt, "1" just echoes and gets cleaned up by BSpace. If agent is busy in raw mode, "1" is silently dropped.

## 2. Monitor Main Loop (1s cycle)

```mermaid
flowchart TD
    START([Monitor Start]) --> CAPTURE[Capture tmux output]

    CAPTURE --> ALIVE{Session alive?}
    ALIVE -->|No| EXIT_DONE([Mark completed, exit monitor])

    ALIVE -->|Yes| HASH[Compute output hash]
    HASH --> CHANGED{Hash changed?}

    CHANGED -->|Yes| PROBE_CAUSED{"Within 2.3s<br/>of last probe?"}
    PROBE_CAUSED -->|Yes| UPDATE_HASH_ONLY["Update hash only<br/>(don't reset state)"]
    PROBE_CAUSED -->|No| REAL_CHANGE["REAL CHANGE:<br/>Reset idle timer<br/>idle_confirmed = false"]

    CHANGED -->|No| IDLE_ACCUM[Idle time accumulates]

    UPDATE_HASH_ONLY --> PHASE1
    REAL_CHANGE --> PHASE1
    IDLE_ACCUM --> PHASE1

    subgraph PHASE1 ["PHASE 1: AUTO-CONFIRM (always runs, even if attached)"]
        CONFIRM_COOL{Confirm cooldown<br/>5s elapsed?}
        CONFIRM_COOL -->|No| SKIP_CONFIRM[Skip]
        CONFIRM_COOL -->|Yes| CONFIRM_CHECK{"Confirm pattern<br/>in last 32 lines?"}
        CONFIRM_CHECK -->|No match| SKIP_CONFIRM

        CONFIRM_CHECK -->|Match| DEDUP{"Same pattern+text+hash<br/>as last confirm?"}
        DEDUP -->|"Yes (stale prose)"| SKIP_CONFIRM

        DEDUP -->|No| SEND_RESP["Send response<br/>'1' / Enter / 'y'+Enter"]
        SEND_RESP --> WAIT_CONFIRM["Wait 0.5s<br/>Recapture screen"]
        WAIT_CONFIRM --> VERIFY{"Output hash<br/>changed?"}

        VERIFY -->|"Yes (dialog consumed)"| CONFIRM_OK["Real dialog ✓<br/>Agent resumes work"]
        VERIFY -->|"No + response='1'"| FALSE_POS["False positive!<br/>Send BSpace cleanup<br/>Save dedup key"]
        VERIFY -->|"No + Enter-based"| CONFIRM_OK
    end

    PHASE1 --> PHASE2

    subgraph PHASE2 ["PHASE 2: STATE DETECTION"]
        STATE_MATCH["Pattern match on output<br/>→ planning/editing/testing/committing"]
    end

    PHASE2 --> PHASE3

    subgraph PHASE3 ["PHASE 3: IDLE PROBE"]
        ALREADY_IDLE{idle_confirmed?}
        ALREADY_IDLE -->|Yes| SKIP_PROBE[Skip]
        ALREADY_IDLE -->|No| STABLE{"Screen stable<br/>≥ 5s?"}
        STABLE -->|No| SKIP_PROBE
        STABLE -->|Yes| PROBE_COOL{"Probe cooldown<br/>5s elapsed?"}
        PROBE_COOL -->|No| SKIP_PROBE
        PROBE_COOL -->|Yes| DO_PROBE["PROBE:<br/>Send '1', wait 0.3s<br/>Recapture screen"]
        DO_PROBE --> ECHO{"'1' echoed<br/>on screen?"}
        ECHO -->|Yes| IDLE_YES["BSpace (cleanup)<br/>idle_confirmed = true<br/>state = 'idle'"]
        ECHO -->|No| CONSUMED["Hidden dialog consumed '1'<br/>Agent resumes work<br/>Reset idle timer"]
    end

    PHASE3 --> PHASE4

    subgraph PHASE4 ["PHASE 4: AUTO-EXIT DECISION"]
        IS_IDLE{idle_confirmed?}
        IS_IDLE -->|Yes| ATTACHED{"User attached?<br/>(session_attached > 0)"}
        ATTACHED -->|Yes| WAIT_CHANGE["Skip auto-exit<br/>(don't kill while user watching)<br/>Wait for detach or new work"]
        ATTACHED -->|No| AE1{"auto_exit<br/>enabled?"}
        AE1 -->|Yes| KILL["Kill session<br/>Mark completed<br/>Exit monitor"]
        AE1 -->|No| WAIT_CHANGE

        IS_IDLE -->|No| FALLBACK{"Screen stable ≥ 30s?"}
        FALLBACK -->|Yes| FALLBACK_IDLE["idle_confirmed = true<br/>state = 'idle'"]
        FALLBACK -->|No| CONTINUE[Continue loop]
    end

    SKIP_PROBE --> PHASE4
    WAIT_CHANGE --> SLEEP
    FALLBACK_IDLE --> SLEEP
    CONTINUE --> SLEEP
    KILL --> EXIT_DONE

    SLEEP[Sleep 1s] --> CAPTURE

    style START fill:#4CAF50,color:#fff
    style EXIT_DONE fill:#FF5722,color:#fff
    style DO_PROBE fill:#2196F3,color:#fff
    style IDLE_YES fill:#4CAF50,color:#fff
    style CONSUMED fill:#FF9800,color:#fff
    style SEND_1 fill:#2196F3,color:#fff
    style SEND_ENTER fill:#2196F3,color:#fff
    style SEND_Y fill:#2196F3,color:#fff
    style KILL fill:#FF5722,color:#fff
```

## 3. State Transitions

```mermaid
stateDiagram-v2
    [*] --> ACTIVE: Agent launched

    state ACTIVE {
        initializing --> planning: "thinking|analyzing"
        initializing --> editing: "Write|Edit"
        planning --> editing: "Write|Edit"
        planning --> testing: "Compiling|Running tests"
        editing --> testing: "Compiling|Running tests"
        editing --> committing: "git commit"
        testing --> editing: "Write|Edit"
        testing --> committing: "git commit"
        committing --> planning: "thinking|analyzing"
        committing --> editing: "Write|Edit"
    }

    ACTIVE --> IDLE: Probe "1" echoed (single probe)
    ACTIVE --> IDLE: Fallback (stable 30s, no probe)

    IDLE --> ACTIVE: Real output change (new work)
    IDLE --> EXIT: auto_exit enabled

    state EXIT {
        completed: session killed
    }

    note right of IDLE
        Monitor keeps running.
        Auto-confirm still active.
        Probes stop.
        Any real output change → back to ACTIVE.
    end note
```

## 4. Auto-Confirm Patterns

```mermaid
flowchart LR
    OUTPUT["Last 32 non-empty<br/>lines of output"] --> RULES

    subgraph RULES ["Confirm Rules — checked in order"]
        R1["Trust dialog<br/>/Enter to confirm|Select/"]
        R2["Do you want to proceed?"]
        R3["1. Yes / 1. Allow"]
        R4["Allow once/always"]
        R5["(y/n) / [Y/n]"]
    end

    R1 -->|match| ENTER["Send Enter"]
    R2 -->|match| ONE["Send '1'"]
    R3 -->|match| ONE
    R4 -->|match| ENTER
    R5 -->|match| YN["Send 'y' + Enter"]

    R1 -->|no match| R2 -->|no match| R3 -->|no match| R4 -->|no match| R5
    R5 -->|no match| NONE[No confirm needed]

    style ONE fill:#2196F3,color:#fff
    style ENTER fill:#2196F3,color:#fff
    style YN fill:#2196F3,color:#fff
```

**Key: only check last 32 lines.** Permission dialogs always appear at the bottom. This prevents false positives when agent output contains text like "1. Yes" in a table or explanation.

## 4a. False Positive Protection

Auto-confirm can match agent **prose** (e.g. Claude writes "Do you want to proceed?" in its response), not just real permission dialogs. Two defenses prevent `1` accumulation:

```mermaid
flowchart TD
    MATCH["Pattern matched<br/>e.g. 'Do you want to proceed'"] --> DEDUP

    DEDUP{"confirm_key ==<br/>last_confirm_matched?"}
    DEDUP -->|"Yes (same pattern+text+hash)"| SUPPRESS["Suppressed<br/>(stale prose, don't resend)"]

    DEDUP -->|"No (new match)"| SAVE_HASH["Save baseline hash<br/>Send response"]
    SAVE_HASH --> WAIT["Wait 0.5s"]
    WAIT --> RECAP["Recapture screen"]
    RECAP --> CMP{"Output hash<br/>changed?"}

    CMP -->|"Changed"| REAL["Real dialog consumed it ✓<br/>Agent resumes work"]
    CMP -->|"Unchanged + response='1'"| FALSE["False positive!<br/>Agent prose, not dialog"]
    CMP -->|"Unchanged + Enter-based"| OK["OK (Enter doesn't<br/>leave residue)"]

    FALSE --> BSPACE["Send BSpace<br/>(clean up echoed '1')"]
    BSPACE --> SAVE_KEY["Save confirm_key<br/>(prevents re-match)"]

    REAL --> RESET["Reset confirm_key<br/>(output changed → new state)"]

    style SUPPRESS fill:#607D8B,color:#fff
    style FALSE fill:#FF9800,color:#fff
    style BSPACE fill:#9C27B0,color:#fff
    style REAL fill:#4CAF50,color:#fff
```

### How `1` accumulation happened (old behavior)

```
Agent writes: "Do you want to proceed with this plan?"     ← prose, not dialog
Monitor matches "Do you want to proceed" pattern
Monitor sends "1" (no Enter, no BSpace)                    ← echoes at prompt: ❯ 1
5s cooldown...
Same text still on screen → matches again
Monitor sends "1" again                                    ← ❯ 11
5s cooldown...                                             ← ❯ 111
...repeats indefinitely                                    ← ❯ 111111111111111
```

### How it's prevented now

```
Agent writes: "Do you want to proceed with this plan?"     ← prose, not dialog
Monitor matches pattern → sends "1"                        ← echoes at prompt: ❯ 1
Monitor recaptures: hash unchanged (prose didn't consume)
Monitor sends BSpace → cleaned up                          ← ❯
Monitor saves confirm_key = "pattern:matched:hash"
5s cooldown...
Same text still on screen → same confirm_key → SUPPRESSED
No "1" sent. Clean terminal.
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
    STABLE -->|Yes| PROBE["Enter Probe Phase"]

    style DONE fill:#4CAF50,color:#fff
    style NOT_DONE fill:#607D8B,color:#fff
    style PROBE fill:#2196F3,color:#fff
```

## 6. Attachment Awareness

```mermaid
flowchart TD
    IDLE["idle_confirmed = true"]

    IDLE --> ATT{"tmux session_attached > 0?"}

    ATT -->|"YES (user watching)"| NO_EXIT["Skip auto-exit<br/>(don't kill while user is in session)<br/>Auto-confirm + probes still run"]
    ATT -->|"NO (unattended)"| AE{"auto_exit enabled?"}

    AE -->|Yes| KILL["Kill session<br/>Mark completed"]
    AE -->|No| WAIT["Stay idle<br/>Wait for new work"]

    style KILL fill:#FF5722,color:#fff
    style NO_EXIT fill:#607D8B,color:#fff
    style IDLE fill:#4CAF50,color:#fff
```

**Attachment only blocks auto-exit, nothing else.** Probes are safe when the screen is stable — if the user is actively typing, the screen won't be stable for 5s, so probes won't fire. Auto-confirm always runs because the user watching doesn't mean they want to manually handle permission dialogs. The only dangerous action is killing a session while a user is sitting in it.

## 7. Full Lifecycle Timeline

```mermaid
sequenceDiagram
    participant U as User
    participant M as Monitor (1s loop)
    participant T as tmux session
    participant A as Agent (Claude)

    Note over M,A: === Agent Working ===

    loop Every 1s
        M->>T: capture-pane
        T-->>M: output (hash changed)
        M->>M: detect_state → editing
        M->>M: confirm pattern? → no match
    end

    Note over M,A: === Permission Dialog Appears ===

    M->>T: capture-pane
    T-->>M: "1. Allow once  2. Deny"
    M->>M: confirm pattern → match!
    M->>T: send-keys "1" (no Enter)
    Note over M: Dialog consumed "1", agent resumes

    Note over M,A: === Agent Finishes, Output Stabilizes ===

    loop Output stable 3s
        M->>T: capture-pane
        T-->>M: "❯ " (hash unchanged)
        M->>M: detect_completion → 2 prompts → done!
    end

    Note over M,A: === Probe Phase (after 5s stable) ===

    rect rgb(33, 150, 243, 0.1)
        Note over M,T: Single Probe
        M->>T: send-keys "1" (no Enter)
        Note over M: Wait 0.3s
        M->>T: capture-pane
        T-->>M: "❯ 1" (echoed!)
        M->>M: idle_confirmed = true
        M->>T: send-keys BSpace (cleanup)
    end

    Note over M,A: === Idle Confirmed ===

    M->>T: display-message session_attached
    T-->>M: 0 (no user attached)

    alt auto_exit = true AND not attached
        M->>T: kill-session
        M->>M: status=completed, reason=auto-exit
    else auto_exit = false OR user attached
        M->>M: state=idle (keep running)
    end

    Note over M,A: === User Gives New Prompt ===

    U->>T: attach + type prompt
    A->>T: starts working (output changes)
    M->>T: capture-pane
    T-->>M: hash changed (real change)
    M->>M: idle_confirmed = false → back to ACTIVE

    loop Agent working again
        M->>T: capture-pane
        M->>M: detect_state, check confirms
        Note over M: Screen changing → probes won't fire (not stable 5s)
    end

    Note over M,A: === Agent finishes again, user still attached ===
    M->>M: idle_confirmed = true (probe echoed)
    M->>T: display-message session_attached
    T-->>M: 1 (user attached)
    Note over M: auto-exit blocked — user in session

    U->>T: detach (Ctrl+B, D)
    Note over M: Next loop: session_attached → 0, auto-exit proceeds
```

## 8. Key Design Decisions

### Single probe vs consecutive probes

**Old design:** Required 2+ consecutive idle probes (threshold=2) with 20s cooldown between them. ~31s from completion to idle confirmed.

**New design:** Single probe is sufficient. If `1` echoes at the prompt, the agent is idle — period. The echo IS the proof. ~8s from completion to idle confirmed.

### Attachment awareness

| Phase | User attached | User not attached |
|-------|:---:|:---:|
| Auto-confirm | Runs | Runs |
| State detection | Runs | Runs |
| Idle probe | Runs | Runs |
| Auto-exit | **Blocked** | Runs (if enabled) |

Everything runs normally regardless of attachment. The only thing blocked is auto-exit — never kill a session a user is sitting in. Probes are safe because the "screen stable 5s" gate naturally prevents them while the user is actively typing.

### Idle → Active revival

Idle is never permanent. Any real output change (not probe-caused) sets `idle_confirmed = false` and resumes the full monitor cycle. This handles:
- User gives new prompt via attached tmux
- Auto-confirm accidentally resumes agent
- Agent wakes up on its own (file watcher, timer)

## 9. Timing Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `confirm_cooldown` | 5s | Min interval between auto-confirm attempts |
| `completion_stable` | 3s | Output must be stable before checking completion |
| `probe_stable` | 5s | Stable time before probe |
| `probe_cooldown` | 5s | Min interval between probes (if needed) |
| `probe_wait` | 0.3s | Wait after sending "1" before recapture |
| `health_check_interval` | 15s | Session-alive check interval |
| `fallback_stable` | 30s | Long-stable fallback for idle when probes skipped |

### Time from task done to idle confirmed

```
Task finishes, output stabilizes              0s
  ├─ completion_stable (3s)                   3s   ← completion detected
  ├─ probe_stable (5s)                        5s   ← probe eligible
  ├─ check attached → no                      5s
  ├─ send "1", wait 0.3s, recapture           5.3s
  ├─ "1" echoed → BSpace                      5.5s ← idle confirmed!
  └─ auto_exit (if enabled)                   ~6s
```

~6 seconds from completion to auto-exit (down from ~31s in old design).
