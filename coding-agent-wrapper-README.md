# Coding Agent Wrapper

自动化交互式编码工具（Claude Code, Codex, Cursor），将它们变成可编程的 API。

## 🎯 功能

- ✅ **自动批准** - 自动发送 `1` / `y` 响应所有确认提示
- ✅ **完成检测** - 智能判断任务何时真正完成
- ✅ **统一接口** - 屏蔽不同工具的差异
- ✅ **可靠的错误处理** - 超时、崩溃、异常都能妥善处理

## 📦 文件结构

```
coding_agent_wrapper.py    # 核心实现（~400 行）
test_agent_wrapper.py      # 测试套件
coding-agent-wrapper-design.md  # 详细设计文档
```

## 🚀 快速开始

### 1. 基本使用

```python
from coding_agent_wrapper import CodingAgent, AgentConfig

# 配置
config = AgentConfig(
    tool="claude",        # 或 "codex", "cursor"
    auto_approve=True,    # 自动批准所有确认
    timeout=300,          # 5分钟总超时
    idle_timeout=10,      # 10秒空闲视为完成
    debug=True,           # 打印调试信息
)

# 创建 agent
agent = CodingAgent(config)

# 执行任务
result = agent.execute(
    task="Create a Python script that prints 'Hello, World!'",
    workdir="./my-project"
)

# 检查结果
if result.status == "completed":
    print(f"✅ Done in {result.duration:.1f}s")
    print(f"Files changed: {result.files_changed}")
else:
    print(f"❌ Failed: {result.error_message}")
```

### 2. 运行测试

```bash
# 给脚本执行权限
chmod +x test_agent_wrapper.py

# 运行测试（会提示选择）
python3 test_agent_wrapper.py
```

测试包括：
1. **Simple Task** - 创建单个 Python 文件
2. **Multiple Files** - 创建多文件项目（calculator + tests）
3. **Code Modification** - 修改已有代码（添加错误处理）

## 🛠️ 工作原理

### 状态机

```
INITIALIZING → THINKING → WAITING_APPROVAL → EXECUTING → COMPLETED
                    ↓           ↓
                  ERROR ← ──────┘
```

### 自动响应逻辑

```python
if "1. Yes" in output:
    send("1\n")
elif "[y/n]" in output:
    send("y\n")
elif "Continue?" in output:
    send("\n")
```

### 完成检测（多策略）

1. **空闲超时** - N 秒无新输出
2. **提示符检测** - 识别 `❯ ` 或 `> ` 
3. **状态匹配** - 匹配 "esc to interrupt" 等标记
4. **文件变化** - 检测到文件被修改

## 📊 配置选项

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tool` | str | `"claude"` | 编码工具名称 |
| `auto_approve` | bool | `True` | 是否自动批准 |
| `timeout` | float | `600.0` | 总超时（秒） |
| `idle_timeout` | float | `10.0` | 空闲超时（秒） |
| `debug` | bool | `False` | 打印调试日志 |

## 🔍 状态识别模式

### Claude Code

```python
"thinking": ["Flibbertigibbeting", "Cogitating"]
"waiting_approval": ["Do you want to proceed?", "❯.*1\\..*Yes"]
"completed": ["esc to interrupt"]
"error": ["Error:", "Failed:"]
```

### Codex

```python
"thinking": ["Planning", "Analyzing"]
"waiting_approval": ["Approve?", "[y/n]"]
"completed": ["Done"]
```

### Cursor

（可扩展，添加 Cursor 特定模式）

## 📝 ExecutionResult 结构

```python
@dataclass
class ExecutionResult:
    status: str              # "completed" / "error" / "timeout"
    files_changed: List[str] # 修改的文件列表
    output_log: str          # 完整输出日志
    duration: float          # 执行时长（秒）
    error_message: str       # 错误信息（可选）
```

## 🎨 使用场景

### 场景 1: 批量代码生成

```python
tasks = [
    "Create user authentication module",
    "Add email validation utility",
    "Write unit tests for auth module",
]

for task in tasks:
    result = agent.execute(task, workdir="./project")
    if result.status == "completed":
        print(f"✅ {task}")
    else:
        print(f"❌ {task}: {result.error_message}")
```

### 场景 2: OpenClaw 集成

```python
def openclaw_coding_task(task: str, workdir: str):
    """在 OpenClaw agent 中使用"""
    agent = CodingAgent(AgentConfig(
        tool="claude",
        auto_approve=True,
        timeout=600,
        debug=False,
    ))
    
    result = agent.execute(task, workdir)
    
    # 通知用户
    if result.status == "completed":
        return f"✅ Task completed!\n\nFiles changed:\n" + \
               "\n".join(f"- {f}" for f in result.files_changed)
    else:
        return f"❌ Task failed: {result.error_message}"
```

### 场景 3: CI/CD 流水线

```python
# 在 GitHub Actions 中使用
def fix_linting_errors():
    agent = CodingAgent(AgentConfig(tool="codex"))
    result = agent.execute(
        "Fix all ESLint errors in src/",
        workdir=os.getcwd()
    )
    
    if result.status != "completed":
        sys.exit(1)  # 失败则退出
```

## ⚠️ 限制 & 注意事项

### 1. 需要工具已安装

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code

# Codex
npm install -g @codex-ai/codex

# Cursor
# 下载安装 Cursor IDE
```

### 2. 需要 Git 仓库

某些工具（如 Codex）要求在 git 仓库中运行：

```bash
cd your-project
git init
```

### 3. 完成检测可能不准确

如果任务很复杂，空闲超时可能误判。建议：
- 调整 `idle_timeout`（默认 10 秒）
- 检查 `result.output_log` 确认真正完成
- 添加工具特定的完成模式

### 4. ANSI 解析不完美

富文本 UI 的输出很复杂，某些边缘情况可能识别错误。

## 🔧 扩展 & 自定义

### 添加新工具支持

1. 在 `OutputParser.PATTERNS` 中添加模式：

```python
PATTERNS["my-tool"] = {
    "thinking": [r"Processing"],
    "waiting_approval": [r"Confirm\?"],
    "completed": [r"Task done"],
    "error": [r"ERROR:"],
}
```

2. 在 `_build_command()` 中添加命令构建逻辑：

```python
elif self.config.tool == "my-tool":
    return ["my-tool", "--task", task]
```

### 自定义完成检测

覆盖 `_is_completed()` 方法：

```python
class MyAgent(CodingAgent):
    def _is_completed(self) -> bool:
        # 自定义逻辑
        if self._custom_check():
            return True
        return super()._is_completed()
```

## 🐛 调试

### 启用调试日志

```python
config = AgentConfig(debug=True)
```

### 查看完整输出

```python
result = agent.execute(...)

# 保存日志
with open("agent.log", "w") as f:
    f.write(result.output_log)

# 分析最后 1000 字符
print(result.output_log[-1000:])
```

### 手动测试模式识别

```python
from coding_agent_wrapper import OutputParser

test_output = """
Do you want to proceed?
❯ 1. Yes
  2. No
"""

state = OutputParser.detect_state(test_output, "claude")
print(state)  # AgentState.WAITING_APPROVAL
```

## 📚 更多信息

- **设计文档**: `coding-agent-wrapper-design.md` - 详细架构和实现思路
- **测试套件**: `test_agent_wrapper.py` - 完整测试用例
- **核心代码**: `coding_agent_wrapper.py` - ~400 行 Python

## 🤝 贡献

欢迎改进这个工具！

**改进方向：**
1. 添加更多工具支持（Cursor, Aider, etc.）
2. 改进完成检测逻辑
3. 更好的 ANSI 解析
4. 进度回调机制
5. 重试/恢复机制

## 📄 License

MIT License - 随意使用和修改

---

**Enjoy automated coding! 🚀**
