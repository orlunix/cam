# Connecting to hren's WSL machine from prgn.nvidia.com

This document is for anyone on **prgn.nvidia.com** who needs shell access to **hren's local WSL1 Ubuntu environment** on the Windows laptop **omni-wfa-q8o5s**.

Access is provided through a **reverse SSH tunnel**. You do not connect to the laptop's IP address directly. You connect to **localhost on prgn**, and traffic is forwarded through the tunnel into WSL.

---

## Quick start

From a shell on **prgn.nvidia.com**:

```bash
ssh -p 2222 hren@127.0.0.1
```

If key-based auth is set up (default):

```bash
ssh -p 2222 -i ~/.ssh/id_rsa hren@127.0.0.1
```

You should get a shell in **WSL1 Ubuntu** (Linux), not on prgn itself.

Verify you landed in WSL:

```bash
hostname          # expect: omni-wfa-q8o5s
uname -a          # expect kernel containing 4.4.0-...-Microsoft (WSL1)
pwd               # expect: /home/hren
```

---

## How it works

```text
  hren's laptop (Windows + WSL1)              prgn.nvidia.com
  ───────────────────────────────             ─────────────────

  WSL runs autossh outbound to prgn ────────► sshd :22
       with reverse forward                     │
                                                │ listens on
                                                ▼
                                         127.0.0.1:2222
                                                │
  You on prgn:  ssh -p 2222 hren@127.0.0.1 ────┘
                                                │
                                                └──► forwarded to WSL sshd :2223
```

| Endpoint | Role |
|----------|------|
| **prgn `127.0.0.1:2222`** | Entry point for you on prgn |
| **WSL `127.0.0.1:2223`** | SSH server inside WSL (not port 22; see below) |
| **Outbound SSH** | WSL → prgn:22 keeps the tunnel alive |

The laptop does **not** need inbound firewall rules on the corporate network. WSL initiates the connection to prgn.

---

## Prerequisites (on the laptop side)

The tunnel must be **up on hren's machine** before you can connect from prgn. That is maintained automatically when the laptop is logged in and WSL is running.

If connection fails, ask hren to run on the laptop (PowerShell):

```powershell
wsl -d Ubuntu -- /usr/local/bin/wsl-reverse-tunnel.sh status
```

Expected output:

```text
sshd (2223): up
autossh: up
prgn port 2222 up
```

---

## Check from prgn before connecting

### 1. Tunnel port is listening

```bash
ss -tln | grep 2222
```

Expected:

```text
LISTEN 0  128  127.0.0.1:2222  0.0.0.0:*
```

If nothing is listening, the reverse tunnel is down. The laptop side must restart it (see above).

### 2. Test SSH through the tunnel

```bash
ssh -p 2222 -i ~/.ssh/id_rsa \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=8 \
  hren@127.0.0.1 hostname
```

Expected output: `omni-wfa-q8o5s`

---

## Authentication

| Direction | Auth method |
|-----------|-------------|
| WSL → prgn (tunnel) | WSL key: `~/.ssh/id_ed25519` on WSL |
| prgn → WSL (your login) | prgn key: `~/.ssh/id_rsa` must be in WSL `~/.ssh/authorized_keys` |

Password login to WSL through the tunnel is disabled. Use your **prgn SSH key**.

If you see `Permission denied (publickey)`:

1. Confirm you are using the correct key: `-i ~/.ssh/id_rsa`
2. Ask hren to verify your public key is in WSL `~/.ssh/authorized_keys`
3. Ask hren to verify permissions: `chmod 600 ~/.ssh/authorized_keys`

---

## What you get access to

| You connect to | You do **not** get |
|----------------|-------------------|
| WSL1 Ubuntu (`/home/hren`, Linux tools, apt) | Windows PowerShell/CMD directly |
| Files under WSL filesystem | Laptop's LAN IP as a direct SSH target |
| `/mnt/c/...` Windows drives via WSL | prgn's shell (unless you `exit`) |

**Distro:** WSL1 `Ubuntu` (not `Ubuntu-24.04` WSL2).  
**User:** `hren`  
**Host:** `omni-wfa-q8o5s`

---

## Common commands on prgn

**Interactive session:**

```bash
ssh -p 2222 hren@127.0.0.1
```

**Run one command:**

```bash
ssh -p 2222 hren@127.0.0.1 'ls -la ~ && df -h /'
```

**Copy file from WSL to prgn:**

```bash
scp -P 2222 -i ~/.ssh/id_rsa hren@127.0.0.1:/home/hren/somefile.txt .
```

**Copy file from prgn to WSL:**

```bash
scp -P 2222 -i ~/.ssh/id_rsa ./somefile.txt hren@127.0.0.1:/home/hren/
```

**RSYNC example:**

```bash
rsync -avz -e 'ssh -p 2222 -i ~/.ssh/id_rsa' \
  hren@127.0.0.1:/home/hren/project/ ./project-backup/
```

Note: SCP/rsync use **`-P` (uppercase)** for port, SSH uses **`-p` (lowercase)**.

---

## Troubleshooting (on prgn)

### Connection refused on port 2222

```bash
ss -tln | grep 2222
```

No output → tunnel is down on the laptop side. Contact hren or wait for auto-recovery (watchdog runs every 5 minutes on the laptop when it is on and logged in).

### Connection hangs

```bash
ssh -vv -p 2222 hren@127.0.0.1
```

Check whether TCP connects but auth fails, or TCP never connects.

### Host key warnings

If `127.0.0.1:2222` host key changed (tunnel was rebuilt):

```bash
ssh-keygen -f ~/.ssh/known_hosts -R '[127.0.0.1]:2222'
ssh -p 2222 -o StrictHostKeyChecking=accept-new hren@127.0.0.1
```

### Lands on Windows OpenSSH instead of WSL

Symptom in verbose output:

```text
remote software version OpenSSH_for_Windows_9.8
```

This means the laptop-side tunnel is forwarding to the wrong port. hren must fix WSL sshd to listen on **2223** and the tunnel to use `-R 127.0.0.1:2222:127.0.0.1:2223`. This should already be configured; report it if you see it again.

### Permission denied (publickey)

Use explicit key:

```bash
ssh -p 2222 -i ~/.ssh/id_rsa hren@127.0.0.1
```

If still failing, the authorized_keys on WSL needs your public key.

---

## When the tunnel is unavailable

The tunnel requires:

- hren's laptop is **on** and **logged into Windows**
- WSL1 distro **Ubuntu** is running
- Network path from laptop to **prgn.nvidia.com:22** is up
- `autossh` process is running inside WSL

It will **not** be available when:

- Laptop is shut down or sleeping
- WSL was stopped (`wsl --shutdown`)
- VPN/network outage between laptop and prgn
- hren manually stopped the tunnel

After laptop reboot, the tunnel should come back automatically at Windows logon (Task Scheduler). Allow a few minutes after logon.

---

## Security notes

- The tunnel binds to **127.0.0.1:2222 on prgn only** — not exposed to other hosts on the network.
- Only users with shell access on prgn can use this port.
- Access to WSL is key-based; do not share private keys.
- This path is intended for **hren's WSL environment**, not as a general-purpose jump host.

---

## Summary

| Item | Value |
|------|-------|
| Connect from | prgn.nvidia.com |
| Command | `ssh -p 2222 hren@127.0.0.1` |
| Key | `~/.ssh/id_rsa` on prgn |
| Target environment | WSL1 Ubuntu, user `hren`, host `omni-wfa-q8o5s` |
| Check tunnel | `ss -tln \| grep 2222` on prgn |
| Laptop-side status | ask hren to run `wsl-reverse-tunnel.sh status` |

---

## Contact

If the tunnel is down or auth fails after checking the steps above, contact **hren** and include:

1. Output of `ss -tln | grep 2222` on prgn
2. Output of `ssh -vv -p 2222 hren@127.0.0.1` (last ~20 lines)
3. Whether the laptop is on and connected to VPN/network
