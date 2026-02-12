#!/usr/bin/env python3
"""
Coding Agent Wrapper - 自动化交互式编码工具

将 Claude Code, Codex, Cursor 等交互式工具封装成可编程的 API。
自动处理所有确认提示，直到任务完成。

Usage:
    from coding_agent_wrapper import CodingAgent, AgentConfig
    
    agent = CodingAgent(AgentConfig(tool="claude", auto_approve=True))
    result = agent.execute("Build a REST API", workdir="./project")
    print(f"Status: {result.status}")
"""

import os
import re
import pty
import sys
import time
import select
import subprocess
from enum import Enum
from typing import Optional, List, Dict, Callable
from dataclasses import dataclass, field


class AgentState(Enum):
    """编码 agent 的可能状态"""
    INITIALIZING = "initializing"
    THINKING = "thinking"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class AgentConfig:
    """Agent 配置"""
    tool: str = "claude"  # "claude", "codex", "cursor"
    auto_approve: bool = True  # 自动批准所有确认
    timeout: float = 600.0  # 总超时（秒）
    idle_timeout: float = 10.0  # 空闲超时（秒）
    debug: bool = False  # 打印调试信息


@dataclass
class ExecutionResult:
    """执行结果"""
    status: str  # "completed", "error", "timeout"
    files_changed: List[str] = field(default_factory=list)
    output_log: str = ""
    duration: float = 0.0
    error_message: Optional[str] = None


class OutputParser:
    """输出解析器 - 识别工具状态"""
    
    # 工具特定的模式
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
                r"Is this a project you created or one you trust\?",  # 安全检查
                r"Yes, I trust this folder",  # 安全检查
            ],
            "waiting_input": [
                r"❯\s*$",
            ],
            "completed": [
                r"esc to interrupt",  # Claude Code 的空闲提示符
            ],
            "error": [
                r"Error:",
                r"Failed:",
                r"Exception:",
            ],
        },
        "codex": {
            "thinking": [r"Planning", r"Analyzing"],
            "waiting_approval": [
                r"Approve\?",
                r"\[y/n\]",
                r"Continue\?",
            ],
            "waiting_input": [r">\s*$"],
            "completed": [r"Done"],
            "error": [r"Error:", r"Failed:"],
        },
    }
    
    @classmethod
    def strip_ansi(cls, text: str) -> str:
        """剥离 ANSI 转义码（改进版）"""
        # \x1b[ 开头的控制序列
        text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        
        # OSC 序列 (如超链接)
        text = re.sub(r'\x1b]8;;[^\x1b]*\x1b\\', '', text)
        
        # 其他控制序列
        text = re.sub(r'\x1b[^\[]', '', text)
        
        # 问号开头的模式
        text = re.sub(r'\[\?[0-9;]*[a-zA-Z]', '', text)
        
        # CSI 移动光标 - 用空格替换（保留布局）
        def replace_cursor_move(match):
            m = re.match(r'\[(\d+)C', match.group())
            if m:
                count = int(m.group(1))
                return ' ' * count
            return ' '
        
        text = re.sub(r'\[\d+C', replace_cursor_move, text)
        
        # 颜色/样式代码
        text = re.sub(r'\[[0-9;]*m', '', text)
        
        return text
    
    @classmethod
    def detect_state(cls, text: str, tool: str) -> Optional[AgentState]:
        """从输出文本中检测状态"""
        if not text:
            return None
        
        # 先剥离 ANSI
        clean_text = cls.strip_ansi(text)
        
        patterns = cls.PATTERNS.get(tool, cls.PATTERNS["claude"])
        
        # 按优先级检查（error > approval > thinking > completed）
        for state_name in ["error", "waiting_approval", "thinking", "completed", "waiting_input"]:
            regexes = patterns.get(state_name, [])
            for regex in regexes:
                if re.search(regex, clean_text, re.IGNORECASE | re.MULTILINE):
                    return AgentState(state_name)
        
        return None


class CodingAgent:
    """编码 Agent 包装器"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.process = None
        self.master_fd = None
        self.output_buffer: List[str] = []
        self.state = AgentState.INITIALIZING
        self.last_output_time = time.time()
        self.last_state_change = time.time()
        
    def execute(self, task: str, workdir: str) -> ExecutionResult:
        """
        执行任务
        
        Args:
            task: 任务描述
            workdir: 工作目录
            
        Returns:
            ExecutionResult: 执行结果
        """
        start_time = time.time()
        
        try:
            self._log(f"🚀 Starting task: {task}")
            self._log(f"📁 Workdir: {workdir}")
            
            # 1. 启动工具
            self._start_tool(task, workdir)
            
            # 2. 主循环
            last_output_len = 0
            
            while True:
                # 读取输出
                output = self._read_output(timeout=0.5)
                
                if output:
                    self.output_buffer.append(output)
                    self.last_output_time = time.time()
                    
                    # 只在有新输出时更新状态和响应
                    if len(self.output_buffer) > last_output_len:
                        last_output_len = len(self.output_buffer)
                        
                        # 更新状态
                        recent = "".join(self.output_buffer[-30:])
                        new_state = OutputParser.detect_state(recent, self.config.tool)
                        if new_state and new_state != self.state:
                            self._log(f"🔄 State: {self.state.value} → {new_state.value}")
                            self.state = new_state
                            self.last_state_change = time.time()
                        
                        # 自动响应
                        response = self._decide_response()
                        if response:
                            self._log(f"📤 Sending: {repr(response)}")
                            self._send_input(response)
                
                # 检查完成条件
                if self._is_completed():
                    self._log("✅ Task completed!")
                    break
                
                # 检查超时
                elapsed = time.time() - start_time
                if elapsed > self.config.timeout:
                    raise TimeoutError(f"Task timeout after {elapsed:.1f}s")
            
            # 3. 收集结果
            return ExecutionResult(
                status="completed",
                files_changed=self._detect_file_changes(workdir),
                output_log="".join(self.output_buffer),
                duration=time.time() - start_time,
            )
            
        except Exception as e:
            self._log(f"❌ Error: {e}")
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
        self._log(f"💻 Command: {' '.join(cmd)}")
        
        # 创建 PTY
        master, slave = pty.openpty()
        
        # 启动进程
        self.process = subprocess.Popen(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=workdir,
            preexec_fn=os.setsid,
        )
        
        os.close(slave)
        self.master_fd = master
        self.state = AgentState.THINKING
    
    def _build_command(self, task: str) -> List[str]:
        """构建命令行"""
        if self.config.tool == "claude":
            return ["claude", task]
        elif self.config.tool == "codex":
            # 注意：codex exec 需要引号包裹任务
            return ["codex", "exec", task]
        elif self.config.tool == "cursor":
            return ["cursor", "--task", task]
        else:
            raise ValueError(f"Unknown tool: {self.config.tool}")
    
    def _read_output(self, timeout: float) -> Optional[str]:
        """非阻塞读取输出"""
        if self.master_fd is None:
            return None
        
        ready, _, _ = select.select([self.master_fd], [], [], timeout)
        
        if ready:
            try:
                data = os.read(self.master_fd, 4096)
                if data:
                    return data.decode('utf-8', errors='replace')
            except OSError:
                return None
        
        return None
    
    def _decide_response(self) -> Optional[str]:
        """决定自动响应"""
        if not self.config.auto_approve:
            return None
        
        if self.state == AgentState.WAITING_APPROVAL:
            recent = "".join(self.output_buffer[-20:])
            clean = OutputParser.strip_ansi(recent)
            
            # Claude Code 风格：1. Yes / 2. No
            if re.search(r"1\..*Yes", clean, re.IGNORECASE):
                return "1\n"
            
            # Codex 风格：[y/n]
            elif re.search(r"\[y/n\]", clean, re.IGNORECASE):
                return "y\n"
            
            # 通用 Continue?
            elif re.search(r"Continue\?", clean, re.IGNORECASE):
                return "\n"
        
        return None
    
    def _is_completed(self) -> bool:
        """判断任务是否完成"""
        # 策略 1: 明确的完成状态
        if self.state == AgentState.COMPLETED:
            idle = time.time() - self.last_output_time
            # 在完成状态且空闲超过 3 秒
            if idle > 3.0:
                return True
        
        # 策略 2: 空闲超时（在输入提示符）
        idle = time.time() - self.last_output_time
        if idle > self.config.idle_timeout:
            recent = "".join(self.output_buffer[-30:])
            clean = OutputParser.strip_ansi(recent)
            
            # 检查是否在空闲提示符
            if self._is_at_ready_prompt(clean):
                self._log(f"⏰ Idle timeout ({idle:.1f}s), assuming completed")
                return True
        
        # 策略 3: 错误状态
        if self.state == AgentState.ERROR:
            return True
        
        return False
    
    def _is_at_ready_prompt(self, text: str) -> bool:
        """检查是否在"等待下一个命令"的提示符"""
        patterns = [
            r"❯\s*$",  # Claude Code 空提示符
            r">\s*$",   # 通用提示符
            r"esc to interrupt",  # Claude Code 的空闲提示
        ]
        return any(re.search(p, text, re.MULTILINE) for p in patterns)
    
    def _send_input(self, text: str):
        """发送输入到工具"""
        if self.master_fd:
            os.write(self.master_fd, text.encode('utf-8'))
    
    def _detect_file_changes(self, workdir: str) -> List[str]:
        """检测修改的文件（使用 git diff）"""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')
            
            # 如果没有 git，尝试查找最近修改的文件
            result = subprocess.run(
                ["find", ".", "-type", "f", "-mmin", "-5", "-not", "-path", "./.git/*"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            
            if result.returncode == 0 and result.stdout.strip():
                files = result.stdout.strip().split('\n')
                return [f.lstrip('./') for f in files if f.strip()]
            
        except Exception as e:
            self._log(f"⚠️ Failed to detect file changes: {e}")
        
        return []
    
    def _cleanup(self):
        """清理资源"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                self.process.kill()
        
        if self.master_fd:
            try:
                os.close(self.master_fd)
            except:
                pass
    
    def _log(self, message: str):
        """打印日志"""
        if self.config.debug:
            print(f"[CodingAgent] {message}", file=sys.stderr)


# ========== 使用示例 ==========

def example_usage():
    """使用示例"""
    
    # 配置
    config = AgentConfig(
        tool="claude",
        auto_approve=True,
        timeout=300,
        idle_timeout=10,
        debug=True,
    )
    
    # 创建 agent
    agent = CodingAgent(config)
    
    # 执行任务
    result = agent.execute(
        task="Create a simple Python script that prints 'Hello, World!'",
        workdir="/tmp/test-project"
    )
    
    # 打印结果
    print("\n" + "="*60)
    print(f"Status: {result.status}")
    print(f"Duration: {result.duration:.1f}s")
    print(f"Files changed: {result.files_changed}")
    
    if result.error_message:
        print(f"Error: {result.error_message}")
    
    # 保存日志
    with open("/tmp/agent-output.log", "w") as f:
        f.write(result.output_log)
    print(f"Full log saved to /tmp/agent-output.log")


if __name__ == "__main__":
    # 如果直接运行，执行示例
    example_usage()
