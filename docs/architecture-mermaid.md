# CAM Architecture (Mermaid Diagrams)

> Date: 2026-03-27

## 1. System Overview

```mermaid
graph TB
    subgraph UI["User Interfaces"]
        camcli["cam CLI<br/>(Typer + Rich)"]
        camccli["camc CLI<br/>(stdlib, zero-dep)"]
        webpwa["Web PWA<br/>(Vanilla JS)"]
        android["Android APP<br/>(WebView)"]
        teams["Teams Bot<br/>(teaspirit)"]
    end

    subgraph Servers["Servers"]
        camserve["cam serve<br/>FastAPI :8420"]
        relay["Relay Server<br/>WebSocket :8443<br/>(stdlib-only)"]
    end

    subgraph Core["cam serve Internals"]
        manager["AgentManager"]
        delegate["CamcDelegate"]
        poller["CamcPoller<br/>(every 5s)"]
        eventbus["EventBus<br/>(in-memory pub/sub)"]
        relayconn["RelayConnector<br/>(outbound WS)"]
        scheduler["Scheduler<br/>(DAG)"]
    end

    subgraph Execution["Execution Layer (per machine)"]
        camc["camc binary<br/>(Python 3.6+, single file)"]
        monitor["Monitor subprocess<br/>(1 per agent, 1s poll)"]
        tmux["tmux sessions"]
        detect["Detection Engine<br/>(state/completion/confirm)"]
    end

    subgraph Tools["AI Tools (in tmux)"]
        claude["Claude Code"]
        codex["Codex CLI"]
        cursor["Cursor"]
        other["Any Tool<br/>(via TOML config)"]
    end

    subgraph Data["Data Layer"]
        json["~/.cam/<br/>agents.json<br/>machines.json<br/>contexts.json<br/>events.jsonl"]
        sqlite["~/.local/share/cam/<br/>cam.db (SQLite)<br/>⚠️ Phase 5: remove"]
        toml["~/.cam/configs/<br/>claude.toml<br/>codex.toml<br/>cursor.toml"]
        logs["~/.cam/logs/<br/>monitor-*.log"]
        sockets["/tmp/cam-sockets/<br/>*.sock"]
        ssh["/tmp/cam-ssh-*<br/>ControlMaster"]
    end

    %% UI connections
    camcli -->|"HTTP localhost"| camserve
    webpwa -->|"HTTP direct"| camserve
    webpwa -->|"REST-over-WS"| relay
    android -->|"REST-over-WS"| relay
    teams -->|"HTTP API"| camserve
    camccli -->|"direct local"| camc

    %% Server connections
    camserve --- Core
    relayconn -->|"outbound WS"| relay
    relay -->|"proxy frames"| relayconn

    %% Core to execution
    manager --> delegate
    delegate -->|"subprocess / SSH"| camc
    poller -->|"SSH / read JSON"| camc
    poller -->|"sync state"| sqlite
    eventbus -->|"WS events"| relayconn

    %% Execution
    camc --> monitor
    camc --> tmux
    monitor --> detect
    monitor -->|"capture-pane"| tmux
    monitor -->|"auto-confirm"| tmux
    detect -->|"patterns"| toml

    %% Tools in tmux
    tmux --> claude
    tmux --> codex
    tmux --> cursor
    tmux --> other

    %% Data connections
    camc -->|"read/write"| json
    camserve -->|"read/write"| sqlite
    monitor -->|"append"| logs
    tmux --- sockets
    delegate --- ssh

    %% Styling
    classDef ui fill:#4A90D9,stroke:#2C5F8A,color:#fff
    classDef server fill:#E67E22,stroke:#BA6418,color:#fff
    classDef core fill:#9B59B6,stroke:#7D3C98,color:#fff
    classDef exec fill:#27AE60,stroke:#1E8449,color:#fff
    classDef tool fill:#2ECC71,stroke:#27AE60,color:#fff
    classDef data fill:#F39C12,stroke:#D68910,color:#fff
    classDef deprecated fill:#E74C3C,stroke:#C0392B,color:#fff

    class camcli,camccli,webpwa,android,teams ui
    class camserve,relay server
    class manager,delegate,poller,eventbus,relayconn,scheduler core
    class camc,monitor,tmux,detect exec
    class claude,codex,cursor,other tool
    class json,toml,logs,sockets,ssh data
    class sqlite deprecated
```

## 2. Agent Output Data Flow

```mermaid
sequenceDiagram
    participant App as Mobile APP
    participant Relay as Relay Server
    participant Serve as cam serve
    participant Cache as Output Cache
    participant Client as cam-client
    participant SSH as SSH/camc
    participant Tmux as tmux session

    App->>Relay: GET /agents/{id}/output?hash=abc
    Relay->>Serve: forward (proxy frame)

    alt Tier 1: cam-client push (10s TTL)
        Serve->>Client: check client_output cache
        Client-->>Serve: fresh output + hash
        Serve-->>Relay: return output (~0ms)
    else Tier 2: output cache hit (2s TTL)
        Serve->>Cache: check _output_cache
        alt hash matches
            Cache-->>Serve: hash match
            Serve-->>Relay: {"unchanged": true} (50 bytes)
        else hash differs
            Cache-->>Serve: cached output
            Serve-->>Relay: return output
        end
    else Tier 3: cache miss → SSH capture
        Serve->>SSH: CamcDelegate.capture()
        SSH->>Tmux: tmux capture-pane -p -S -100
        Tmux-->>SSH: raw terminal text
        SSH-->>Serve: stripped output + hash
        Serve->>Cache: store (2s TTL)
        Serve-->>Relay: return output
    end

    Relay-->>App: response (50B-7KB)
```

## 3. Agent Startup Flow

```mermaid
sequenceDiagram
    participant API as cam serve API
    participant AM as AgentManager
    participant CD as CamcDelegate
    participant CAMC as camc (target machine)
    participant Mon as Monitor subprocess
    participant Tmux as tmux session
    participant Tool as Claude/Codex/Cursor
    participant Store as agents.json
    participant SQL as SQLite

    API->>AM: run_agent(task, context)
    AM->>CD: run_agent(tool, prompt, path)
    CD->>CAMC: ssh camc run --tool claude ...

    CAMC->>Tmux: create_tmux_session(cam-{id})
    Note over Tmux: socket: /tmp/cam-sockets/cam-{id}.sock<br/>history: 50000 lines<br/>screen: 220x50

    CAMC->>Tmux: send-keys (startup auto-confirm)
    CAMC->>Tmux: send-keys (prompt)
    Tmux->>Tool: launch tool with prompt

    CAMC->>Store: save agent record
    CAMC->>Mon: spawn monitor subprocess
    Note over Mon: PID: ~/.cam/pids/{id}.pid<br/>Log: ~/.cam/logs/monitor-{id}.log

    CAMC-->>CD: return agent JSON
    CD-->>AM: agent dict
    AM->>SQL: save agent to SQLite
    AM-->>API: return Agent response

    loop Every 1s
        Mon->>Tmux: capture-pane
        Mon->>Mon: detect_state()
        Mon->>Mon: should_auto_confirm()
        Mon->>Mon: detect_completion()
        Mon->>Store: update state/status
    end
```

## 4. State Sync (camc → cam serve)

```mermaid
sequenceDiagram
    participant Mon as Monitor<br/>(per agent)
    participant JSON as ~/.cam/<br/>agents.json
    participant JSONL as ~/.cam/<br/>events.jsonl
    participant Poller as CamcPoller<br/>(in cam serve)
    participant SQL as SQLite<br/>(cam.db)
    participant Bus as EventBus
    participant WS as WebSocket
    participant Relay as Relay
    participant App as Mobile APP

    Mon->>JSON: update status/state
    Mon->>JSONL: append event

    loop Every 5s
        Poller->>JSON: read agents.json (local)
        Note over Poller: or SSH → camc --json list (remote)

        alt Status changed
            Poller->>SQL: update_status()
            Poller->>SQL: add_event()
            Poller->>Bus: publish(status_change)
            Bus->>WS: send to direct clients
            Bus->>Relay: send via RelayConnector
            Relay->>App: forward event
        end

        Poller->>JSONL: read events since last_ts
        loop Each new event
            Poller->>Bus: publish(event)
        end
    end
```

## 5. Relay NAT Traversal

```mermaid
graph LR
    subgraph Private["Private Network"]
        serve["cam serve<br/>(FastAPI)"]
        rc["RelayConnector"]
    end

    subgraph Public["Public Internet"]
        relay["Relay Server<br/>:8443"]
    end

    subgraph Clients["Any Network"]
        mobile["Mobile APP"]
        web["Web PWA"]
    end

    serve --> rc
    rc -->|"outbound WS<br/>/server?sid=X"| relay
    mobile -->|"WS /client?token=T"| relay
    web -->|"WS /client?token=T"| relay

    relay -->|"forward request"| rc
    rc -->|"ASGI in-process dispatch"| serve
    serve -->|"response"| rc
    rc -->|"response frame"| relay
    relay -->|"broadcast"| mobile
    relay -->|"broadcast"| web
```

```mermaid
sequenceDiagram
    participant M as Mobile
    participant R as Relay
    participant S as cam serve

    Note over S,R: Server connects outbound (NAT-safe)
    S->>R: WS connect /server?sid=UUID&token=T

    Note over M,R: Client connects to public relay
    M->>R: WS connect /client?token=T

    M->>R: {"id":"r1","method":"GET","path":"/api/agents"}
    R->>S: forward frame
    S->>S: ASGI dispatch → FastAPI
    S->>R: {"id":"r1","status":200,"body":"[...]"}
    R->>M: forward response

    Note over M,S: Event stream
    M->>R: {"id":"ws1","method":"WS","path":"/api/ws"}
    R->>S: forward
    S-->>R: {"id":"ws1","event":{...}}
    R-->>M: forward event
```

## 6. Transport Backends

```mermaid
graph TB
    AM["AgentManager"]

    subgraph Transports["Transport Layer"]
        local["LocalTransport<br/>subprocess → tmux"]
        ssh["SSHTransport<br/>ControlMaster → tmux"]
        agent["AgentTransport<br/>SSH → cam-agent (Go)"]
        client["ClientTransport<br/>HTTP push mode"]
        docker["DockerTransport<br/>docker exec → tmux"]
    end

    subgraph Delegation["Delegation Layer"]
        delegate["CamcDelegate<br/>(wraps camc CLI)"]
    end

    AM --> local
    AM --> ssh
    AM --> agent
    AM --> client
    AM --> docker
    AM --> delegate

    subgraph Targets["Target Machines"]
        ltmux["Local tmux<br/>/tmp/cam-sockets/*.sock"]
        rtmux["Remote tmux<br/>/tmp/cam-sockets/*.sock"]
        goagent["cam-agent binary<br/>/tmp/cam-agent-sockets/*.sock"]
        camcbin["camc binary<br/>~/.cam/camc"]
    end

    local --> ltmux
    ssh -->|"SSH ControlMaster<br/>/tmp/cam-ssh-*"| rtmux
    agent -->|"SSH"| goagent
    delegate -->|"subprocess / SSH"| camcbin

    subgraph SSHPool["SSH Connection Pool"]
        cm["ControlMaster socket<br/>/tmp/cam-ssh-{hash}<br/>shared by SSH + Delegate<br/>persist 600s"]
    end

    ssh --- cm
    delegate --- cm
```

## 7. camc Internal Architecture

```mermaid
graph TB
    subgraph CLI["camc CLI (single-file, stdlib-only)"]
        run["run"]
        list["list"]
        stop["stop/kill"]
        heal["heal"]
        capture["capture"]
        send["send/key"]
        machine["machine<br/>list/add/rm/edit/ping"]
        context["context<br/>list/add/rm"]
        sync["sync"]
        migrate["migrate"]
        apply["apply (DAG)"]
    end

    subgraph Monitor["Monitor Subprocess (per agent)"]
        health["Health Check<br/>tmux_session_exists()"]
        cap["Capture<br/>capture_tmux()"]
        state["State Detection<br/>detect_state()"]
        confirm["Auto-Confirm<br/>should_auto_confirm()"]
        complete["Completion Detection<br/>detect_completion()"]
        autoexit["Auto-Exit<br/>kill_session / send /exit"]
    end

    subgraph Storage["Storage (JSON + fcntl)"]
        agents["AgentStore<br/>~/.cam/agents.json"]
        events["EventStore<br/>~/.cam/events.jsonl"]
        machines["MachineStore<br/>~/.cam/machines.json"]
        contexts["ContextStore<br/>~/.cam/contexts.json"]
    end

    subgraph Transport["Transport"]
        tmux["tmux sessions<br/>/tmp/cam-sockets/*.sock"]
        remote["Remote SSH<br/>(for sync/ping)"]
    end

    subgraph Config["Adapter Configs"]
        toml["~/.cam/configs/*.toml<br/>claude.toml<br/>codex.toml<br/>cursor.toml"]
    end

    run --> tmux
    run --> agents
    run -->|"spawn"| Monitor
    list --> agents
    stop --> tmux
    stop --> agents
    heal --> agents
    heal --> tmux
    heal --> events
    capture --> tmux
    send --> tmux
    machine --> machines
    context --> contexts
    sync --> remote
    sync --> machines
    migrate -->|"SQLite → JSON"| agents

    cap --> tmux
    state --> toml
    confirm --> toml
    confirm -->|"send-keys"| tmux
    complete --> toml
    health --> tmux
    autoexit --> tmux

    Monitor --> agents
    Monitor --> events
```

## 8. Data Layer

```mermaid
graph TB
    subgraph JSON["~/.cam/ (Source of Truth)"]
        aj["agents.json<br/>Agent records<br/>(fcntl locked)"]
        mj["machines.json<br/>Machine definitions"]
        cj["contexts.json<br/>Context/workspace defs"]
        ej["events.jsonl<br/>Event log<br/>(30-day auto-rotate)"]
        toml["configs/*.toml<br/>Adapter configs"]
        logs["logs/monitor-*.log<br/>Monitor logs"]
        pids["pids/*.pid<br/>Monitor PIDs"]
    end

    subgraph SQLite["~/.local/share/cam/ (Cache — Phase 5 removes)"]
        db["cam.db"]
        tbl_agents["agents table<br/>(21 rows)"]
        tbl_contexts["contexts table<br/>(25 rows)"]
        tbl_events["agent_events table<br/>(811K rows, 142MB)"]
    end

    subgraph Tmp["/tmp/ (Ephemeral)"]
        socks["cam-sockets/*.sock<br/>tmux Unix sockets"]
        sshsocks["cam-ssh-*<br/>SSH ControlMaster"]
    end

    subgraph Memory["In-Memory (cam serve)"]
        outcache["Output Cache<br/>(2s TTL per agent)"]
        clientout["Client Output<br/>(10s TTL, push mode)"]
        eventbus["EventBus<br/>(pub/sub)"]
        statuscache["Status Cache<br/>(WS polling)"]
    end

    aj -->|"CamcPoller 5s"| tbl_agents
    ej -->|"CamcPoller 5s"| eventbus
    aj -->|"direct read"| outcache

    camc["camc"] -->|"write"| aj
    camc -->|"append"| ej
    monitor["monitor"] -->|"write"| aj
    monitor -->|"append"| ej
    monitor -->|"write"| logs
    monitor -->|"write"| pids

    camserve["cam serve"] -->|"read/write"| db
    camserve -->|"read"| outcache
    camserve -->|"read"| clientout
```

## 9. Authentication Flow

```mermaid
graph LR
    subgraph Auth["Authentication"]
        token["Server Auth Token<br/>config.server.auth_token"]
        relaytoken["Relay Token<br/>config.server.relay_token"]
        sshkey["SSH Key/Kerberos<br/>via ssh-agent"]
    end

    camcli["cam CLI"] -->|"Bearer token"| camserve["cam serve"]
    webpwa["Web/Mobile"] -->|"Bearer token"| camserve
    webpwa -->|"relay_token"| relay["Relay"]
    camserve -->|"relay_token"| relay
    camserve -->|"SSH ControlMaster"| remote["Remote machines"]

    token --- camserve
    relaytoken --- relay
    sshkey --- remote
```

## Latency Summary

```mermaid
graph LR
    subgraph Best["Best Case ~100ms"]
        b1["App"] --> b2["Relay<br/>50ms"] --> b3["Serve<br/>hash match<br/>5ms"] --> b4["Relay<br/>50ms"] --> b5["App"]
    end

    subgraph Typical["Typical ~160ms"]
        t1["App"] --> t2["Relay<br/>50ms"] --> t3["Serve<br/>cache hit<br/>10ms"] --> t4["Relay<br/>50ms"] --> t5["App"]
    end

    subgraph Worst["Worst Case ~1400ms"]
        w1["App"] --> w2["Relay<br/>50ms"] --> w3["Serve<br/>SSH capture<br/>500ms"] --> w4["Relay<br/>50ms"] --> w5["App"]
    end
```
