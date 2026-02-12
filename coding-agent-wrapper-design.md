# Coding Agent Wrapper - 设计文档

## 目标

将交互式编码工具（Claude Code, Codex, Cursor）封装成简单的 API 接口：

```python
agent = CodingAgent(tool="claude")
result = agent.execute("Build a REST API for todos", workdir="./project")
# 自动处理所有确认，直到完成
```

---

## 核心挑战

### 1. 状态识别
从 ANSI 富文本输出中识别：
- 🤔 **思考中** (Thinking/Planning)
- ⏸️ **等待批准** (Waiting for approval)
- ⏳ **等待输入** (Waiting for user input)
- 🔨 **执行中** (Running command)
- ✅ **完成** (Task done, waiting for next instruction)
- ❌ **错误** (Error occurred)

### 2. 自动响应
根据状态自动发送输入：
- 批准请求 → 发送 `1` 或 `y`
- 继续请求 → 发送 `Enter`
- 完成信号 → 停止循环

---

## 架构设计

```
┌─────────────────────────────────────────┐
│  用户代码 (User Code)                    │
│  agent.execute("task description")      │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│  Agent Controller (控制器)               │
│  - 任务管理                              │
│  - 状态机                                │
│  - 超时/错误处理                         │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│  State Machine (状态机)                  │
│  - 解析输出流                            │
│  - 识别当前状态                          │
│  - 决定下一步动作                        │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│  PTY Manager (终端管理器)                │
│  - 启动 claude/codex/cursor              │
│  - 捕获输出流                            │
│  - 发送输入                              │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│  Output Parser (输出解析器)              │
│  - 剥离 ANSI 转义码                      │
│  - 识别关键模式                          │
│  - 提取结构化信息                        │
└───────────────────────────────────────────┘
```

---

## 核心组件

### 1. State Machine (状态机)

```python
class AgentState(Enum):
    INITIALIZING = "initializing"
    THINKING = "thinking"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ERROR = "error"
```

### 2. Pattern Matcher (模式识别器)

```python
PATTERNS = {
    "claude": {
        "thinking": [
            r"Flibbertigibbeting",
            r"Cogitating",
        ],
        "waiting_approval": [
            r"Do you want to proceed\?",
            r"Permission rule.*requires confirmation",
            r"❯.*1\..*Yes",
        ],
        "waiting_input": [
            r"❯\s+$",  # 空提示符
        ],
        "completed": [
            r"Task completed",
            r"All changes applied",
            # 或者：连续 N 秒没有新输出，且在输入提示符
        ],
        "error": [
            r"Error:",
            r"Failed:",
            r"Exception:",
        ],
    },
    "codex": {
        "thinking": [r"Planning", r"Analyzing"],
        "waiting_approval": [r"Approve\?", r"\[y/n\]"],
        # ... 类似模式
    },
}
```

### 3. Auto-Response Logic (自动响应逻辑)

```python
def auto_respond(state: AgentState, output: str) -> Optional[str]:
    """根据状态决定发送什么"""
    if state == AgentState.WAITING_APPROVAL:
        # 检测具体是哪种确认
        if "1. Yes" in output:
            return "1\n"
        elif "[y/n]" in output:
            return "y\n"
        elif "Continue?" in output:
            return "\n"  # 只发 Enter
    
    elif state == AgentState.WAITING_INPUT:
        # 如果是等待下一个任务，停止
        if is_ready_for_next_task(output):
            return None  # 结束循环
    
    return None
```

### 4. Completion Detection (完成检测)

**最关键的部分：如何判断任务真正完成？**

多重策略：

```python
def is_task_completed(history: List[str], idle_time: float) -> bool:
    """
    判断任务是否完成的复合条件
    """
    last_output = history[-50:]  # 最近的输出
    
    # 策略 1: 明确的完成信号
    if any(re.search(pattern, last_output) for pattern in COMPLETION_PATTERNS):
        return True
    
    # 策略 2: 空闲超时（在输入提示符，且无新输出）
    if idle_time > 5.0 and is_at_prompt(last_output):
        return True
    
    # 策略 3: 文件变化已应用（检测文件系统）
    if files_recently_modified() and idle_time > 3.0:
        return True
    
    # 策略 4: 工具特定的完成标记
    if tool == "claude" and "esc to interrupt" in last_output:
        # Claude Code 回到空提示符
        return True
    
    return False
```

---

## 实现：Python Wrapper

### API 接口

```python
from coding_agent_wrapper import CodingAgent, AgentConfig

# 配置
config = AgentConfig(
    tool="claude",  # 或 "codex", "cursor"
    auto_approve=True,
    timeout=300,  # 5分钟超时
    idle_timeout=10,  # 10秒无输出视为完成
)

# 创建 agent
agent = CodingAgent(config)

# 执行任务
result = agent.execute(
    task="Build a REST API with /todos endpoints (GET, POST, DELETE)",
    workdir="/home/user/project"
)

# 结果
print(result.status)  # "completed" / "error" / "timeout"
print(result.files_changed)  # ["src/api.py", "src/models.py"]
print(result.output_log)  # 完整输出日志
print(result.duration)  # 执行时长
```

### 核心实现

```python
import pty
import os
import select
import re
import time
from typing import Optional, List
from dataclasses import dataclass
from enum import Enum

@dataclass
class ExecutionResult:
    status: str  # "completed" / "error" / "timeout"
    files_changed: List[str]
    output_log: str
    duration: float
    error_message: Optional[str] = None

class CodingAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.process = None
        self.output_buffer = []
        self.state = AgentState.INITIALIZING
        
    def execute(self, task: str, workdir: str) -> ExecutionResult:
        """执行任务的主入口"""
        start_time = time.time()
        
        try:
            # 1. 启动工具
            self._start_tool(task, workdir)
            
            # 2. 进入主循环
            while True:
                # 读取输出
                output = self._read_output(timeout=1.0)
                if output:
                    self.output_buffer.append(output)
                    
                # 更新状态
                self._update_state(output)
                
                # 自动响应
                response = self._decide_response()
                if response:
                    self._send_input(response)
                
                # 检查完成/超时
                if self._is_completed():
                    break
                if time.time() - start_time > self.config.timeout:
                    raise TimeoutError("Task timeout")
                    
            # 3. 收集结果
            return ExecutionResult(
                status="completed",
                files_changed=self._detect_file_changes(workdir),
                output_log="".join(self.output_buffer),
                duration=time.time() - start_time,
            )
            
        except Exception as e:
            return ExecutionResult(
                status="error",
                files_changed=[],
                output_log="".join(self.output_buffer),
                duration=time.time() - start_time,
                error_message=str(e),
            )
        finally:
            self._cleanup()
    
    def _start_tool(self, task: str, workdir: str):
        """启动编码工具"""
        cmd = self._build_command(task)
        master, slave = pty.openpty()
        self.process = subprocess.Popen(
            cmd,
            stdin=slave, stdout=slave, stderr=slave,
            cwd=workdir,
            preexec_fn=os.setsid,
        )
        os.close(slave)
        self.master_fd = master
    
    def _read_output(self, timeout: float) -> Optional[str]:
        """非阻塞读取输出"""
        ready, _, _ = select.select([self.master_fd], [], [], timeout)
        if ready:
            try:
                data = os.read(self.master_fd, 4096)
                return data.decode('utf-8', errors='replace')
            except OSError:
                return None
        return None
    
    def _update_state(self, output: str):
        """根据输出更新状态"""
        if not output:
            return
            
        patterns = PATTERNS[self.config.tool]
        
        # 检查每种状态的模式
        for state, regexes in patterns.items():
            if any(re.search(r, output, re.IGNORECASE) for r in regexes):
                self.state = AgentState(state)
                break
    
    def _decide_response(self) -> Optional[str]:
        """决定自动响应"""
        if not self.config.auto_approve:
            return None
            
        if self.state == AgentState.WAITING_APPROVAL:
            recent = "".join(self.output_buffer[-10:])
            if "1. Yes" in recent:
                return "1\n"
            elif "[y/n]" in recent:
                return "y\n"
        
        return None
    
    def _is_completed(self) -> bool:
        """判断任务是否完成"""
        # 策略 1: 状态为 COMPLETED
        if self.state == AgentState.COMPLETED:
            return True
        
        # 策略 2: 空闲超时
        if len(self.output_buffer) > 0:
            last_output_time = getattr(self, '_last_output_time', time.time())
            idle = time.time() - last_output_time
            
            if idle > self.config.idle_timeout:
                recent = "".join(self.output_buffer[-20:])
                if self._is_at_ready_prompt(recent):
                    return True
        
        return False
    
    def _is_at_ready_prompt(self, text: str) -> bool:
        """检查是否在"等待下一个命令"的提示符"""
        # Claude Code: 空提示符 + "esc to interrupt"
        # Codex: 空提示符 + 没有活动
        patterns = [
            r"❯\s+$",
            r">\s+$",
            r"esc to interrupt",
        ]
        return any(re.search(p, text) for p in patterns)
    
    def _send_input(self, text: str):
        """发送输入到工具"""
        os.write(self.master_fd, text.encode('utf-8'))
        self._last_input_time = time.time()
    
    def _detect_file_changes(self, workdir: str) -> List[str]:
        """检测哪些文件被修改（可选：使用 git diff）"""
        # 简化实现：检查最近修改的文件
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip().split('\n') if result.returncode == 0 else []
    
    def _cleanup(self):
        """清理资源"""
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
        if hasattr(self, 'master_fd'):
            os.close(self.master_fd)
```

---

## 使用示例

### 示例 1: 简单任务

```python
agent = CodingAgent(AgentConfig(tool="claude", auto_approve=True))

result = agent.execute(
    task="Add error handling to the API endpoints",
    workdir="./my-project"
)

if result.status == "completed":
    print(f"✅ Done in {result.duration:.1f}s")
    print(f"Files changed: {result.files_changed}")
else:
    print(f"❌ Failed: {result.error_message}")
```

### 示例 2: 批量任务

```python
tasks = [
    "Fix all TypeScript errors in src/",
    "Add unit tests for the auth module",
    "Update README with new API endpoints",
]

for task in tasks:
    print(f"🚀 Starting: {task}")
    result = agent.execute(task, workdir="./project")
    print(f"   {'✅' if result.status == 'completed' else '❌'} {task}")
```

### 示例 3: OpenClaw 集成

```python
# 在 OpenClaw agent 中使用
def handle_coding_task(task: str, workdir: str):
    """OpenClaw agent 调用这个函数"""
    agent = CodingAgent(AgentConfig(
        tool="claude",
        auto_approve=True,
        timeout=600,
    ))
    
    result = agent.execute(task, workdir)
    
    # 通知用户
    if result.status == "completed":
        message.send(
            action="send",
            message=f"✅ Coding task completed!\n\nFiles changed:\n" + 
                    "\n".join(f"- {f}" for f in result.files_changed)
        )
    else:
        message.send(
            action="send",
            message=f"❌ Task failed: {result.error_message}"
        )
    
    return result
```

---

## 高级特性

### 1. 智能暂停点

如果工具问了不在预期内的问题（不是简单的 yes/no），暂停并询问用户：

```python
def _decide_response(self) -> Optional[str]:
    if self.state == AgentState.WAITING_INPUT:
        recent = "".join(self.output_buffer[-10:])
        
        # 简单确认 → 自动响应
        if is_simple_confirmation(recent):
            return "y\n"
        
        # 复杂问题 → 暂停并通知
        else:
            self.pause()
            notify_user(f"Agent needs input: {recent}")
            return None  # 等待用户提供答案
```

### 2. 进度回调

```python
agent = CodingAgent(config, on_progress=lambda state, msg: print(f"[{state}] {msg}"))

agent.execute("Build API")
# 输出:
# [thinking] Planning the API structure...
# [waiting_approval] Asking to create 3 files
# [executing] Writing src/api.py...
# [completed] Task finished
```

### 3. 工具适配器

支持新工具只需实现适配器：

```python
class CursorAdapter(ToolAdapter):
    def build_command(self, task: str) -> List[str]:
        return ["cursor", "--task", task]
    
    def get_patterns(self) -> Dict[str, List[str]]:
        return {
            "thinking": [r"Processing"],
            "waiting_approval": [r"Continue\?"],
            ...
        }
```

---

## 挑战 & 解决方案

### 挑战 1: 完成检测不准确

**问题:** 工具在等待下一步，但我们误判为完成

**解决:**
- 多重检测策略（空闲时间 + 提示符 + 文件变化）
- 工具特定的"完成标记"（如 Claude Code 的提示符样式）
- 可配置的 `idle_timeout`

### 挑战 2: ANSI 解析复杂

**问题:** 进度条、颜色、光标移动让文本乱七八糟

**解决:**
- 使用成熟的库（如 `pyte` 或 `ansi2html`）
- 只保留最新的"屏幕状态"，丢弃中间帧
- 模式匹配前先剥离 ANSI

### 挑战 3: 工具更新导致模式失效

**问题:** Claude Code 更新后，提示符文本变了

**解决:**
- 使用模糊匹配（fuzzy matching）
- 维护多个版本的模式库
- 提供"学习模式"：用户可以手动标注样本

---

## 开发计划

### Phase 1: MVP (1-2 days)
- ✅ PTY 管理器
- ✅ 基础状态机
- ✅ Claude Code 适配器
- ✅ 简单的完成检测

### Phase 2: 鲁棒性 (3-5 days)
- ⬜ 多工具支持（Codex, Cursor）
- ⬜ 改进的完成检测
- ⬜ 错误恢复
- ⬜ 超时处理

### Phase 3: 高级功能 (1 week)
- ⬜ 进度回调
- ⬜ 智能暂停点
- ⬜ 日志记录
- ⬜ 性能优化

---

## 总结

这个 Wrapper 的核心价值：

1. **自动化交互** - 自动发 `1` 直到完成
2. **统一接口** - 屏蔽不同工具的差异
3. **可编程控制** - 像调用函数一样使用编码工具
4. **可靠的完成检测** - 知道什么时候真的完成了

技术难点：
- 状态识别（ANSI 解析 + 模式匹配）
- 完成检测（多策略组合）
- 错误恢复（工具崩溃、超时、意外输入）

如果实现得好，这个工具可以让 **OpenClaw + 编码工具** 成为真正的自动化编程助手！
