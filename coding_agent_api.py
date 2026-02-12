#!/usr/bin/env python3
"""
Coding Agent Wrapper - Production API

提供简单的 API 来自动化 Claude Code
"""

import pexpect
import sys
import os
import time
import re
from typing import Dict, Optional, List
from dataclasses import dataclass


@dataclass
class AgentResult:
    """执行结果"""
    success: bool
    duration: float
    auto_confirms: int
    files_created: List[str] = None
    error: Optional[str] = None
    
    def __post_init__(self):
        if self.files_created is None:
            self.files_created = []


class CodingAgentAPI:
    """
    Claude Code 自动化 API
    
    Usage:
        api = CodingAgentAPI()
        result = api.execute("Create hello.py", workdir="/tmp/project")
        
        if result.success:
            print(f"Created: {result.files_created}")
    """
    
    def __init__(self, debug: bool = False):
        """
        初始化
        
        Args:
            debug: 是否打印调试信息
        """
        self.debug = debug
    
    def _log(self, msg: str):
        """打印日志"""
        if self.debug:
            print(msg, flush=True)
    
    def execute(
        self,
        task: str,
        workdir: str,
        timeout: int = 300,
        tool: str = "claude"
    ) -> AgentResult:
        """
        执行编码任务
        
        Args:
            task: 任务描述，例如 "Create a REST API with Flask"
            workdir: 工作目录（必须已存在且是 git 仓库）
            timeout: 超时时间（秒），默认 300（5分钟）
            tool: 使用的工具，默认 "claude"
            
        Returns:
            AgentResult: 执行结果
            
        Example:
            >>> api = CodingAgentAPI(debug=True)
            >>> result = api.execute(
            ...     task="Create calculator.py with add/sub/mul/div",
            ...     workdir="/tmp/myproject"
            ... )
            >>> if result.success:
            ...     print(f"Done in {result.duration:.1f}s")
            ...     print(f"Files: {result.files_created}")
        """
        workdir = os.path.abspath(workdir)
        
        # 验证目录
        if not os.path.isdir(workdir):
            return AgentResult(
                success=False,
                duration=0,
                auto_confirms=0,
                error=f"Directory not found: {workdir}"
            )
        
        self._log("🚀 Coding Agent API")
        self._log("=" * 60)
        self._log(f"📁 Workdir: {workdir}")
        self._log(f"📝 Task: {task}")
        self._log(f"🔧 Tool: {tool}")
        self._log("=" * 60)
        
        start_time = time.time()
        auto_confirm_count = 0
        files_created = []
        
        try:
            # 启动工具
            child = pexpect.spawn(
                tool,
                args=[task],
                cwd=workdir,
                timeout=10,
                encoding='utf-8',
            )
            
            # 可选：打印输出
            if self.debug:
                child.logfile_read = sys.stdout
            
            idle_checks = 0
            max_idle = 20  # 200秒无动作
            
            while True:
                # 检查总超时
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    self._log(f"\n⏰ Total timeout ({timeout}s)")
                    break
                
                try:
                    # 等待多种模式
                    index = child.expect([
                        r'Is this a project',  # 0: 安全提示
                        r'1\..*Yes',           # 1: 选项 1
                        r'Do.*you.*want',      # 2: 确认提示
                        r'Created.*\.(py|js|ts|java|cpp|c|go|rs|rb|php|html|css)',  # 3: 文件创建
                        pexpect.TIMEOUT,       # 4
                        pexpect.EOF,           # 5
                    ], timeout=10)
                    
                    if index <= 2:  # 需要确认
                        if auto_confirm_count < 50:
                            self._log(f"\n✅ Auto-confirm #{auto_confirm_count} (pattern {index})")
                            child.send('1')
                            auto_confirm_count += 1
                            idle_checks = 0
                        else:
                            self._log("\n⚠️ Too many confirmations")
                            break
                    
                    elif index == 3:  # 文件创建
                        # 提取文件名
                        match = re.search(r'Created\s+([^\s]+\.(py|js|ts|java|cpp|c|go|rs|rb|php|html|css))', 
                                        child.before + child.after)
                        if match:
                            filename = match.group(1)
                            # 移除路径前缀，只保留文件名
                            filename = os.path.basename(filename)
                            if filename not in files_created:
                                files_created.append(filename)
                            self._log(f"\n✅ File created: {filename}")
                        
                        # 继续等待，可能还有更多文件
                        idle_checks = 0
                    
                    elif index == 4:  # TIMEOUT
                        idle_checks += 1
                        if idle_checks > max_idle:
                            self._log(f"\n⏰ Idle timeout ({idle_checks * 10}s)")
                            break
                    
                    elif index == 5:  # EOF
                        self._log("\n✅ Process ended")
                        break
                
                except pexpect.TIMEOUT:
                    idle_checks += 1
                    if idle_checks > max_idle:
                        self._log(f"\n⏰ Idle timeout ({idle_checks * 10}s)")
                        break
                
                except pexpect.EOF:
                    self._log("\n✅ Process ended (EOF)")
                    break
            
            # 关闭
            if child.isalive():
                child.close()
            
            duration = time.time() - start_time
            
            self._log("=" * 60)
            self._log("📊 Statistics:")
            self._log(f"  - Auto-confirmations: {auto_confirm_count}")
            self._log(f"  - Duration: {duration:.1f}s")
            self._log(f"  - Files: {files_created}")
            self._log("=" * 60)
            
            # 验证文件是否真的存在
            verified_files = []
            for filename in files_created:
                filepath = os.path.join(workdir, filename)
                if os.path.exists(filepath):
                    verified_files.append(filename)
            
            return AgentResult(
                success=True,
                duration=duration,
                auto_confirms=auto_confirm_count,
                files_created=verified_files,
            )
        
        except Exception as e:
            self._log(f"\n❌ Error: {e}")
            return AgentResult(
                success=False,
                duration=time.time() - start_time,
                auto_confirms=auto_confirm_count,
                files_created=files_created,
                error=str(e),
            )


# ========== 便捷函数 ==========

def execute_task(task: str, workdir: str, **kwargs) -> AgentResult:
    """
    便捷函数：执行单个任务
    
    Args:
        task: 任务描述
        workdir: 工作目录
        **kwargs: 传递给 CodingAgentAPI.execute()
        
    Returns:
        AgentResult
        
    Example:
        result = execute_task("Create app.py", "/tmp/project", debug=True)
    """
    api = CodingAgentAPI(**{k: v for k, v in kwargs.items() if k == 'debug'})
    return api.execute(task, workdir, **{k: v for k, v in kwargs.items() if k != 'debug'})


def execute_tasks(tasks: List[str], workdir: str, **kwargs) -> List[AgentResult]:
    """
    便捷函数：执行多个任务（顺序执行）
    
    Args:
        tasks: 任务列表
        workdir: 工作目录
        **kwargs: 传递给 CodingAgentAPI.execute()
        
    Returns:
        List[AgentResult]
        
    Example:
        results = execute_tasks([
            "Create models.py with User class",
            "Create api.py with Flask routes",
        ], "/tmp/project")
    """
    api = CodingAgentAPI(**{k: v for k, v in kwargs.items() if k == 'debug'})
    results = []
    
    for i, task in enumerate(tasks, 1):
        print(f"\n{'='*60}")
        print(f"Task {i}/{len(tasks)}: {task}")
        print('='*60)
        
        result = api.execute(task, workdir, **{k: v for k, v in kwargs.items() if k != 'debug'})
        results.append(result)
        
        if not result.success:
            print(f"❌ Task {i} failed, stopping")
            break
    
    return results


# ========== CLI ==========

def main():
    """命令行使用"""
    if len(sys.argv) < 3:
        print("Usage: python3 coding_agent_api.py <workdir> <task>")
        print("")
        print("Example:")
        print('  python3 coding_agent_api.py /tmp/project "Create hello.py"')
        sys.exit(1)
    
    workdir = sys.argv[1]
    task = sys.argv[2]
    
    result = execute_task(task, workdir, debug=True)
    
    if result.success:
        print(f"\n✅ Success!")
        print(f"Duration: {result.duration:.1f}s")
        print(f"Files: {result.files_created}")
        sys.exit(0)
    else:
        print(f"\n❌ Failed: {result.error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
