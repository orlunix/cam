#!/bin/bash
# demo-codex-session.sh - 完整演示：用 OpenClaw 运行 Codex

set -e

CM_DATA="$HOME/.cm"
DEMO_PROJECT="/tmp/cm-demo-project"

echo "=== Coding Manager + OpenClaw 集成演示 ==="
echo ""

# 1. 准备演示项目
echo "步骤 1: 准备演示项目..."
mkdir -p "$DEMO_PROJECT"
cd "$DEMO_PROJECT"

# Codex 需要 git repo
if [ ! -d ".git" ]; then
    git init
    git config user.email "demo@example.com"
    git config user.name "Demo User"
fi

# 创建一个简单的文件
cat > main.py <<'EOF'
def greet(name):
    print(f"Hello {name}")

greet("World")
EOF

git add main.py
git commit -m "Initial commit" 2>/dev/null || true

echo "  ✓ 项目准备完成: $DEMO_PROJECT"
echo ""

# 2. 创建 CM context
echo "步骤 2: 创建 CM context..."
/home/hren/.openclaw/workspace/cm-prototype/cm ctx add demo-project "$DEMO_PROJECT" --tags demo 2>/dev/null || true
echo "  ✓ Context 创建"
echo ""

# 3. 创建 session
echo "步骤 3: 创建编码任务..."
task="Add error handling to the greet function. Check if name is empty."

# 手动创建 session（模拟 cm start）
session_id="sess-demo-$(date +%s)"
timestamp=$(date -Iseconds)

mkdir -p "$CM_DATA/sessions/active"

cat > "$CM_DATA/sessions/active/$session_id.md" <<MDEOF
---
id: $session_id
context: demo-project
context_path: $DEMO_PROJECT
tool: codex
status: starting
state: initializing
created: $timestamp
updated: $timestamp
process_id: 
auto_confirm: true
---

# Session $session_id

## Task

$task

## Status

🟡 **Starting**

## Timeline

| Time     | Event          | Details                    |
|----------|----------------|----------------------------|
| $(date +%H:%M:%S) | started  | Session created            |

## Files Changed

_None yet_

## Output

\`\`\`
_Waiting for codex..._
\`\`\`
MDEOF

echo "codex exec --full-auto '$task'" > "$CM_DATA/sessions/active/$session_id.cmd"
echo "$DEMO_PROJECT" > "$CM_DATA/sessions/active/$session_id.workdir"

echo "  ✓ Session 创建: $session_id"
echo ""

# 4. 现在需要 OpenClaw agent 执行
cat <<'EOF'
步骤 4: 启动 Codex（需要 OpenClaw agent 执行）

现在需要你（OpenClaw agent）使用 exec 工具:

```
exec(
    pty: true,
    background: true,
    workdir: "/tmp/cm-demo-project",
    command: "codex exec --full-auto 'Add error handling to the greet function. Check if name is empty.'"
)
```

这会返回一个 process sessionId，例如: "abc-123-xyz"

然后使用 process 工具监控输出:

```
process(
    action: "log",
    sessionId: "abc-123-xyz",
    follow: true
)
```

输出示例:
```
✓ Planning changes...
⚡ Editing main.py
  - Adding error handling
  - Checking for empty name
❓ Apply these changes? (y/n)
```

当看到确认提示时，发送输入:

```
process(
    action: "submit",
    sessionId: "abc-123-xyz",
    data: "y"
)
```

继续监控直到完成:

```
✓ Changes applied
Done
```

EOF

echo ""
echo "Session 文件位置:"
echo "  $CM_DATA/sessions/active/$session_id.md"
echo ""
echo "要查看 session:"
echo "  /home/hren/.openclaw/workspace/cm-prototype/cm status $session_id"
echo ""
echo "=== 演示设置完成 ==="
echo ""
echo "下一步: 让 OpenClaw agent 使用 exec 和 process 工具来实际运行 Codex"
