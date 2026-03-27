# CAM Architecture (Mermaid Diagrams)

> Date: 2026-03-27

## 1. Deployment Topology

```mermaid
graph TB
    subgraph Users["👤 Users"]
        user_local["Local User<br/>(SSH terminal)"]
        user_mobile["Mobile User"]
        user_web["Web User"]
        user_teams["Teams User"]
    end

    subgraph Local_Machine["🖥️ Local Machine (hren@local)"]
        subgraph cam_server["cam serve (FastAPI :8420)"]
            api["REST API"]
            ws["WebSocket Events"]
            poller["CamcPoller<br/>polls all machines<br/>every 5s"]
            relayconn["RelayConnector<br/>(outbound WS)"]
        end

        subgraph cam_cli["cam CLI"]
            cam_list["cam list"]
            cam_serve_cmd["cam serve"]
            cam_sync["cam sync"]
            cam_heal["cam heal"]
            cam_doctor["cam doctor"]
        end

        subgraph camc_local["camc (local instance)"]
            camc_l_run["camc run"]
            camc_l_list["camc list"]
            camc_l_stop["camc stop/kill"]
            camc_l_heal["camc heal"]
            camc_l_attach["camc attach"]
            camc_l_machine["camc machine list/add/rm"]
            camc_l_context["camc context list/add/rm"]
            camc_l_sync["camc sync"]
            camc_l_mon["monitors<br/>(1 per agent)"]
        end

        subgraph local_tmux["tmux sessions"]
            l_agent1["🤖 Claude Code<br/>cam-abc12345"]
            l_agent2["🤖 Claude Code<br/>cam-def67890"]
        end

        subgraph local_data["~/.cam/ (JSON)"]
            l_agents["agents.json"]
            l_machines["machines.json"]
            l_contexts["contexts.json"]
            l_events["events.jsonl"]
            l_configs["configs/*.toml"]
        end

        sqlite["cam.db (SQLite)<br/>⚠️ Phase 5: remove"]
    end

    subgraph Remote_1["🖥️ Remote Machine (hren@pdx-110)"]
        subgraph camc_r1["camc (remote instance)"]
            camc_r1_run["camc run"]
            camc_r1_list["camc list"]
            camc_r1_stop["camc stop/kill"]
            camc_r1_heal["camc heal (cron)"]
            camc_r1_attach["camc attach"]
            camc_r1_capture["camc capture"]
            camc_r1_mon["monitors<br/>(1 per agent)"]
        end

        subgraph r1_tmux["tmux sessions"]
            r1_agent1["🤖 Claude Code<br/>cam-11223344"]
            r1_agent2["🤖 Claude Code<br/>cam-55667788"]
            r1_agent3["🤖 Codex<br/>cam-aabbccdd"]
        end

        r1_data["~/.cam/<br/>agents.json<br/>events.jsonl<br/>configs/*.toml"]
    end

    subgraph Remote_2["🖥️ Remote Machine (hren@bpmpfw)"]
        subgraph camc_r2["camc (remote instance)"]
            camc_r2_run["camc run"]
            camc_r2_list["camc list"]
            camc_r2_heal["camc heal (cron)"]
            camc_r2_mon["monitors<br/>(1 per agent)"]
        end

        subgraph r2_tmux["tmux sessions"]
            r2_agent1["🤖 Claude Code<br/>cam-eeff0011"]
        end

        r2_data["~/.cam/<br/>agents.json<br/>events.jsonl"]
    end

    subgraph Cloud["☁️ Public Cloud"]
        relay["Relay Server<br/>WebSocket :8443<br/>(stdlib-only, zero-dep)"]
    end

    subgraph Mobile["📱 Clients"]
        app["Android APP<br/>(WebView + CamBridge)"]
        webui["Web PWA<br/>(Service Worker)"]
        teaspirit["teaspirit<br/>(Teams Bot)"]
    end

    %% User connections
    user_local --> cam_cli
    user_local --> camc_local
    user_mobile --> app
    user_web --> webui
    user_teams --> teaspirit

    %% cam CLI → cam serve
    cam_cli -->|"HTTP localhost"| api

    %% cam serve → relay
    relayconn -->|"outbound WS"| relay

    %% Clients → relay
    app -->|"REST-over-WS"| relay
    webui -->|"REST-over-WS"| relay
    webui -->|"HTTP direct<br/>(if reachable)"| api
    teaspirit -->|"HTTP API"| api

    %% cam serve → remote camc (SSH)
    poller -->|"SSH ControlMaster"| camc_r1
    poller -->|"SSH ControlMaster"| camc_r2
    poller -->|"read JSON"| camc_local
    api -->|"CamcDelegate<br/>SSH"| camc_r1
    api -->|"CamcDelegate<br/>SSH"| camc_r2
    api -->|"CamcDelegate<br/>subprocess"| camc_local

    %% camc sync deploys to remotes
    camc_l_sync -->|"SSH scp"| camc_r1
    camc_l_sync -->|"SSH scp"| camc_r2

    %% camc → tmux
    camc_local --> local_tmux
    camc_r1 --> r1_tmux
    camc_r2 --> r2_tmux

    %% camc → data
    camc_local --> local_data
    camc_r1 --> r1_data
    camc_r2 --> r2_data
    poller -->|"sync to"| sqlite

    %% Styling
    classDef machine fill:#1a1a2e,stroke:#16213e,color:#e0e0e0
    classDef server fill:#e67e22,stroke:#d35400,color:#fff
    classDef cli fill:#3498db,stroke:#2980b9,color:#fff
    classDef camc fill:#27ae60,stroke:#1e8449,color:#fff
    classDef tmux fill:#2ecc71,stroke:#27ae60,color:#000
    classDef data fill:#f39c12,stroke:#e67e22,color:#000
    classDef cloud fill:#9b59b6,stroke:#8e44ad,color:#fff
    classDef client fill:#3498db,stroke:#2c3e50,color:#fff
    classDef deprecated fill:#e74c3c,stroke:#c0392b,color:#fff

    class cam_server server
    class cam_cli,cam_list,cam_serve_cmd,cam_sync,cam_heal,cam_doctor cli
    class camc_local,camc_r1,camc_r2 camc
    class local_tmux,r1_tmux,r2_tmux,l_agent1,l_agent2,r1_agent1,r1_agent2,r1_agent3,r2_agent1 tmux
    class local_data,l_agents,l_machines,l_contexts,l_events,l_configs,r1_data,r2_data data
    class relay cloud
    class app,webui,teaspirit client
    class sqlite deprecated
```

## 2. cam serve Internal Architecture

```mermaid
graph TB
    subgraph API["REST API Routes"]
        agents_ep["POST/GET /agents<br/>GET/PATCH/DELETE /agents/{id}"]
        output_ep["GET /agents/{id}/output<br/>GET /agents/{id}/fulloutput"]
        input_ep["POST /agents/{id}/input<br/>POST /agents/{id}/key"]
        client_ep["POST /client/{id}/sync<br/>(cam-client push)"]
        ctx_ep["GET/POST /contexts<br/>GET /contexts/{id}/files"]
        sys_ep["GET /system/health<br/>GET /system/apk/info"]
        ws_ep["WS /api/ws<br/>(event stream)"]
    end

    subgraph Core["Core Services"]
        manager["AgentManager<br/>orchestrates lifecycle"]
        delegate["CamcDelegate<br/>wraps camc CLI"]
        poller["CamcPoller<br/>5s poll loop"]
        eventbus["EventBus<br/>pub/sub"]
        scheduler["Scheduler<br/>DAG execution"]
    end

    subgraph Cache["In-Memory Cache"]
        out_cache["Output Cache<br/>2s TTL per agent"]
        client_cache["Client Output<br/>10s TTL (push mode)"]
        status_cache["Status Cache<br/>(WS polling)"]
    end

    subgraph Storage["Storage"]
        sqlite["SQLite cam.db"]
        json["~/.cam/agents.json<br/>(read for local)"]
    end

    subgraph Outbound["Outbound Connections"]
        relay_out["RelayConnector<br/>→ Relay WS"]
        ssh_out["SSH ControlMaster<br/>→ Remote machines"]
        local_out["Subprocess<br/>→ Local camc"]
    end

    agents_ep --> manager
    output_ep --> out_cache
    output_ep --> client_cache
    output_ep --> delegate
    input_ep --> delegate
    client_ep --> client_cache
    client_ep --> sqlite
    ws_ep --> eventbus

    manager --> delegate
    delegate --> ssh_out
    delegate --> local_out
    poller --> ssh_out
    poller --> json
    poller --> sqlite
    poller --> eventbus
    eventbus --> ws_ep
    eventbus --> relay_out
```

## 3. camc Instance Detail

```mermaid
graph TB
    subgraph CLI["camc CLI Commands"]
        direction LR
        run["run<br/>start agent"]
        list["list<br/>show agents"]
        stop["stop/kill"]
        attach["attach<br/>tmux attach"]
        add["add<br/>adopt session"]
        logs["logs<br/>follow output"]
        capture["capture"]
        send["send/key"]
        status["status --json"]
        heal["heal<br/>(+ cron)"]
        apply["apply<br/>DAG scheduler"]
        history["history<br/>event log"]
        machine_cmd["machine<br/>list/add/rm/edit/ping"]
        context_cmd["context<br/>list/add/rm"]
        sync_cmd["sync<br/>deploy to remotes"]
        migrate_cmd["migrate<br/>SQLite→JSON"]
    end

    subgraph Monitor["Monitor (1 per agent, 1s loop)"]
        m_health["1. health check<br/>tmux_session_exists()"]
        m_capture["2. capture<br/>capture_tmux()"]
        m_state["3. state detection<br/>pattern → planning/editing/<br/>testing/committing/idle"]
        m_confirm["4. auto-confirm<br/>pattern → send Enter"]
        m_complete["5. completion detection<br/>prompt_count ≥ 2"]
        m_exit["6. auto-exit<br/>kill_session or /exit"]
    end

    subgraph Data["Storage"]
        agents["agents.json<br/>(fcntl locked)"]
        events["events.jsonl<br/>(30-day rotate)"]
        machines["machines.json"]
        contexts["contexts.json"]
        configs["configs/*.toml<br/>claude/codex/cursor"]
        pidfiles["pids/*.pid"]
        logfiles["logs/monitor-*.log"]
    end

    subgraph Sessions["tmux Sessions"]
        s1["cam-{id1}<br/>/tmp/cam-sockets/{id1}.sock"]
        s2["cam-{id2}<br/>/tmp/cam-sockets/{id2}.sock"]
        s3["cam-{id3}<br/>/tmp/cam-sockets/{id3}.sock"]
    end

    run --> Sessions
    run --> agents
    run -->|"spawn"| Monitor
    list --> agents
    stop --> Sessions
    stop --> agents
    attach --> Sessions
    capture --> Sessions
    send --> Sessions
    heal --> agents
    heal --> Sessions
    heal -->|"rotate"| events
    machine_cmd --> machines
    context_cmd --> contexts
    sync_cmd -->|"SSH scp"| machines

    Monitor --> Sessions
    Monitor --> agents
    Monitor --> events
    Monitor --> configs
    Monitor --> logfiles
    Monitor --> pidfiles

    m_health --> m_capture
    m_capture --> m_state
    m_state --> m_confirm
    m_confirm --> m_complete
    m_complete --> m_exit
```

## 4. Output Capture Path

```mermaid
sequenceDiagram
    participant App as 📱 Mobile APP
    participant Relay as ☁️ Relay
    participant Serve as 🖥️ cam serve
    participant Cache as 💾 Output Cache
    participant SSH as 🔗 SSH
    participant CAMC as 🟢 camc (remote)
    participant Tmux as 📟 tmux session

    App->>Relay: GET /agents/{id}/output?hash=abc
    Relay->>Serve: proxy frame

    alt Tier 1: cam-client push (10s TTL)
        Note over Serve: client_output[id] fresh?
        Serve-->>Relay: output (~0ms)
    else Tier 2: cache hit (2s TTL)
        Note over Serve: _output_cache[id] fresh?
        alt hash match
            Serve-->>Relay: {"unchanged":true} (50B)
        else changed
            Serve-->>Relay: cached output (7KB)
        end
    else Tier 3: cache miss
        Serve->>SSH: CamcDelegate.capture()
        SSH->>CAMC: camc capture {id} --lines 100
        CAMC->>Tmux: tmux capture-pane -p -S -100
        Tmux-->>CAMC: terminal text
        CAMC-->>SSH: stripped output
        SSH-->>Serve: output + MD5 hash
        Note over Serve: cache for 2s
        Serve-->>Relay: output (7KB)
    end

    Relay-->>App: response
```

## 5. Agent Startup Path

```mermaid
sequenceDiagram
    participant App as 📱 APP / CLI
    participant Serve as cam serve
    participant CD as CamcDelegate
    participant SSH as SSH
    participant CAMC as camc (target)
    participant Tmux as tmux
    participant Mon as Monitor
    participant Tool as Claude Code

    App->>Serve: POST /agents {tool, prompt, context}
    Serve->>CD: run_agent()
    CD->>SSH: ssh user@host camc run --tool claude ...
    SSH->>CAMC: execute

    CAMC->>Tmux: create session cam-{id}
    Note over Tmux: socket /tmp/cam-sockets/cam-{id}.sock<br/>history 50000, screen 220x50

    CAMC->>Tmux: send-keys (auto-confirm trust dialog)
    CAMC->>Tmux: send-keys (user prompt)
    Tmux->>Tool: Claude Code starts working

    CAMC->>CAMC: save to agents.json
    CAMC->>Mon: spawn (start_new_session=True)
    Note over Mon: PID → ~/.cam/pids/{id}.pid<br/>Log → ~/.cam/logs/monitor-{id}.log

    CAMC-->>SSH: return agent JSON
    SSH-->>CD: agent dict
    CD-->>Serve: Agent model
    Serve->>Serve: save to SQLite
    Serve-->>App: 200 OK {agent}

    loop Monitor loop (every 1s)
        Mon->>Tmux: capture-pane
        Mon->>Mon: detect state (editing? testing?)
        Mon->>Mon: auto-confirm? (send Enter)
        Mon->>Mon: completed? (prompt count ≥ 2)
        Mon->>CAMC: update agents.json
    end
```

## 6. Relay NAT Traversal

```mermaid
graph LR
    subgraph Private["🔒 Private Network<br/>(behind NAT/firewall)"]
        serve["cam serve<br/>:8420"]
        rc["RelayConnector"]
        camc_l["camc local"]
        camc_r["camc remotes<br/>(via SSH)"]
    end

    subgraph Cloud["☁️ Public Cloud"]
        relay["Relay Server<br/>:8443<br/>(stateless proxy)"]
    end

    subgraph Anywhere["📱 Any Network"]
        app["Android APP"]
        web["Web PWA"]
        teams["teaspirit<br/>(Teams Bot)"]
    end

    serve --- rc
    rc -->|"① outbound WS<br/>/server?sid=X"| relay

    app -->|"② WS /client"| relay
    web -->|"② WS /client"| relay
    teams -->|"③ HTTP direct<br/>(if reachable)"| serve

    relay -.->|"proxy requests"| rc
    rc -.->|"ASGI dispatch"| serve
    serve -.->|"responses"| rc
    rc -.->|"WS frames"| relay
    relay -.->|"broadcast"| app
    relay -.->|"broadcast"| web

    serve --> camc_l
    serve -->|"SSH"| camc_r
```

## 7. State Sync Flow

```mermaid
sequenceDiagram
    participant Mon as 🟢 Monitor<br/>(on each machine)
    participant JSON as 📄 agents.json
    participant JSONL as 📄 events.jsonl
    participant Poll as CamcPoller<br/>(in cam serve)
    participant SQL as SQLite
    participant Bus as EventBus
    participant WS as WebSocket
    participant Relay as Relay
    participant App as 📱 APP

    Note over Mon,JSON: Monitor updates source of truth
    Mon->>JSON: status=completed
    Mon->>JSONL: {type: "completed"}

    Note over Poll,SQL: Poller syncs every 5s
    loop Every 5s
        Poll->>JSON: read (local) or SSH camc list (remote)
        Poll->>SQL: update_status()
        Poll->>Bus: publish(status_change)
    end

    Note over Bus,App: Events flow to clients
    Bus->>WS: direct WS clients
    Bus->>Relay: via RelayConnector
    Relay->>App: forward event
```

## 8. Data Flow Summary

```mermaid
graph TB
    subgraph Write["Write Path"]
        camc_w["camc run/stop"]
        mon_w["monitor loop"]
    end

    subgraph SOT["Source of Truth"]
        json_w["~/.cam/agents.json<br/>(per machine)"]
        jsonl_w["~/.cam/events.jsonl<br/>(per machine)"]
    end

    subgraph Sync["Sync (5s)"]
        poller_s["CamcPoller"]
    end

    subgraph Cache_Layer["Cache (cam serve)"]
        sql_c["SQLite cam.db<br/>⚠️ Phase 5: remove"]
        mem_c["In-memory<br/>output: 2s TTL<br/>client: 10s TTL"]
    end

    subgraph Read["Read Path"]
        api_r["REST API"]
        ws_r["WebSocket"]
        relay_r["Relay → APP"]
    end

    camc_w -->|"write"| json_w
    mon_w -->|"write"| json_w
    mon_w -->|"append"| jsonl_w

    json_w -->|"poll"| poller_s
    jsonl_w -->|"poll"| poller_s
    poller_s -->|"import"| sql_c
    poller_s -->|"publish"| ws_r

    sql_c -->|"query"| api_r
    mem_c -->|"fast read"| api_r
    api_r --> relay_r
    ws_r --> relay_r
```

## 9. Complete Connection Map

```mermaid
graph TB
    subgraph Interfaces["User Interfaces"]
        cam["cam CLI<br/>• list, serve, sync, heal, doctor"]
        camc_cli["camc CLI (per machine)<br/>• run, list, stop, attach, heal<br/>• machine, context, sync<br/>• capture, send, key, logs"]
        webapp["Web PWA"]
        android["Android APP<br/>(CamBridge: restartApp,<br/>installApk, getAppVersion)"]
        teams["teaspirit (Teams)"]
    end

    subgraph Servers["Servers"]
        direction TB
        camserve["cam serve (FastAPI)<br/>:8420<br/>• REST API (agents, contexts, files)<br/>• WebSocket events<br/>• CamcPoller (5s sync)<br/>• RelayConnector"]
        relay["Relay Server<br/>:8443<br/>• WS proxy (stateless)<br/>• server ↔ client bridge<br/>• event stream forwarding"]
    end

    subgraph Machines["Machines (each runs camc)"]
        m_local["Local Machine<br/>camc + monitors + tmux<br/>agents.json"]
        m_pdx["pdx-110 (SSH :3859)<br/>camc + monitors + tmux<br/>agents.json"]
        m_bpmp["bpmpfw (SSH :22)<br/>camc + monitors + tmux<br/>agents.json"]
    end

    subgraph Agents["AI Agents (in tmux)"]
        a_claude["Claude Code"]
        a_codex["Codex CLI"]
        a_cursor["Cursor"]
    end

    %% Interface → Server
    cam -->|"HTTP"| camserve
    teams -->|"HTTP"| camserve
    webapp -->|"HTTP direct"| camserve
    webapp -->|"REST-over-WS"| relay
    android -->|"REST-over-WS"| relay

    %% Server ↔ Relay
    camserve -->|"outbound WS"| relay

    %% Server → Machines
    camserve -->|"subprocess"| m_local
    camserve -->|"SSH ControlMaster"| m_pdx
    camserve -->|"SSH ControlMaster"| m_bpmp

    %% camc CLI direct access
    camc_cli -->|"direct"| m_local
    camc_cli -->|"direct"| m_pdx
    camc_cli -->|"direct"| m_bpmp

    %% Machines → Agents
    m_local --> a_claude
    m_pdx --> a_claude
    m_pdx --> a_codex
    m_bpmp --> a_claude
    m_bpmp --> a_cursor

    classDef iface fill:#3498db,stroke:#2980b9,color:#fff
    classDef server fill:#e67e22,stroke:#d35400,color:#fff
    classDef machine fill:#27ae60,stroke:#1e8449,color:#fff
    classDef agent fill:#2ecc71,stroke:#27ae60,color:#000

    class cam,camc_cli,webapp,android,teams iface
    class camserve,relay server
    class m_local,m_pdx,m_bpmp machine
    class a_claude,a_codex,a_cursor agent
```
