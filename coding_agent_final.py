#!/usr/bin/env python3
"""
Coding Agent Wrapper - Python版本（基于 expect 的策略）

使用 pexpect 库实现与 ca 脚本相同的逻辑：
1. 日志文件轮询
2. 定期检查（每3秒）
3. 发送不带换行的 "1"
"""

import pexpect
import sys
import os
import time
import re
import subprocess
from pathlib import Path
from typing import Optional


class CodingAgentWrapper:
    """Claude Code 自动化包装器"""
    
    def __init__(self, debug: bool = True):
        self.debug = debug
        self.log_file: Optional[str] = None
        self.child: Optional[pexpect.spawn] = None
        self.auto_confirm_count = 0
        
    def _log(self, msg: str):
        """打印日志"""
        if self.debug:
            print(msg, flush=True)
    
    def _strip_ansi(self, text: str) -> str:
        """剥离 ANSI 转义码"""
        # 移除 ANSI 转义序列
        ansi_escape = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
        text = ansi_escape.sub('', text)
        
        # 移除 OSC 序列
        osc_escape = re.compile(r'\x1b]8;;[^\x1b]*\x1b\\')
        text = osc_escape.sub('', text)
        
        return text
    
    def _check_log_for_prompt(self) -> bool:
        """检查日志文件中是否有确认提示"""
        if not self.log_file or not os.path.exists(self.log_file):
            return False
        
        try:
            # 读取最后 100 行
            result = subprocess.run(
                f"tail -n 100 {self.log_file}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            
            if result.returncode != 0:
                return False
            
            # 剥离 ANSI
            clean = self._strip_ansi(result.stdout)
            
            # 检查确认提示的模式
            patterns = [
                r'1\..*Yes',
                r'Do.*you.*want',
                r'Permission.*requires',
            ]
            
            for pattern in patterns:
                if re.search(pattern, clean, re.IGNORECASE):
                    return True
            
            return False
            
        except Exception as e:
            if self.debug:
                self._log(f"⚠️ Log check error: {e}")
            return False
    
    def _check_log_for_done(self) -> bool:
        """检查是否完成"""
        if not self.log_file or not os.path.exists(self.log_file):
            return False
        
        try:
            result = subprocess.run(
                f"tail -n 50 {self.log_file}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            
            if result.returncode != 0:
                return False
            
            clean = self._strip_ansi(result.stdout)
            
            # 完成信号
            patterns = [
                r'esc.*interrupt',
                r'\?.*for.*shortcuts',
            ]
            
            for pattern in patterns:
                if re.search(pattern, clean, re.IGNORECASE):
                    return True
            
            return False
            
        except Exception as e:
            return False
    
    def execute(self, task: str, workdir: str, timeout: int = 300) -> dict:
        """
        执行编码任务
        
        Args:
            task: 任务描述
            workdir: 工作目录
            timeout: 超时时间（秒）
            
        Returns:
            dict: {
                'success': bool,
                'auto_confirms': int,
                'log_file': str,
                'error': str (可选)
            }
        """
        workdir = os.path.abspath(workdir)
        
        if not os.path.isdir(workdir):
            return {
                'success': False,
                'error': f"Directory not found: {workdir}"
            }
        
        # 创建日志文件
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_file = os.path.join(workdir, f".claude-wrapper-{timestamp}.log")
        
        self._log("🚀 Coding Agent Wrapper")
        self._log("=" * 60)
        self._log(f"📁 Workdir: {workdir}")
        self._log(f"📝 Task: {task}")
        self._log(f"📋 Log: {self.log_file}")
        self._log("=" * 60)
        
        try:
            # 启动 Claude Code
            log_fp = open(self.log_file, 'w', buffering=1)  # 行缓冲
            
            self.child = pexpect.spawn(
                'claude',
                args=[task],
                cwd=workdir,
                timeout=None,
                encoding='utf-8',
                logfile=log_fp,
            )
            
            self.auto_confirm_count = 0
            idle_count = 0
            max_idle = 20  # 60秒无动作
            start_time = time.time()
            
            # 主循环
            while True:
                # 等待 3 秒
                time.sleep(3)
                
                # 检查是否有确认提示
                if self._check_log_for_prompt():
                    if self.auto_confirm_count < 50:
                        self._log(f"\n✅ Auto-confirm #{self.auto_confirm_count}")
                        self.child.send('1')  # 不带换行！
                        self.auto_confirm_count += 1
                        idle_count = 0
                    else:
                        self._log("\n⚠️ Too many confirmations, stopping")
                        break
                else:
                    idle_count += 1
                    
                    # 检查是否完成
                    if idle_count > 3 and self._check_log_for_done():
                        self._log("\n✅ Task completed!")
                        break
                    
                    # 空闲超时
                    if idle_count > max_idle:
                        self._log(f"\n⏰ Idle timeout ({idle_count * 3}s)")
                        break
                
                # 总超时
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    self._log(f"\n⏰ Total timeout ({elapsed:.1f}s)")
                    break
                
                # 检查进程是否还在
                if not self.child.isalive():
                    self._log("\n✅ Process ended")
                    break
            
            # 关闭
            if self.child.isalive():
                self.child.close()
            
            self._log("=" * 60)
            self._log("📊 Statistics:")
            self._log(f"  - Auto-confirmations: {self.auto_confirm_count}")
            self._log(f"  - Idle cycles: {idle_count}")
            self._log(f"📋 Full log: {self.log_file}")
            self._log("=" * 60)
            
            return {
                'success': True,
                'auto_confirms': self.auto_confirm_count,
                'log_file': self.log_file,
            }
            
        except Exception as e:
            self._log(f"\n❌ Error: {e}")
            return {
                'success': False,
                'error': str(e),
                'auto_confirms': self.auto_confirm_count,
                'log_file': self.log_file,
            }
        
        finally:
            # 清理
            if self.child and self.child.isalive():
                self.child.close()
            
            # 关闭日志文件
            try:
                if hasattr(self.child, 'logfile') and self.child.logfile:
                    self.child.logfile.close()
            except:
                pass


# ========== 使用示例 ==========

def main():
    """命令行使用"""
    if len(sys.argv) < 3:
        print("Usage: python3 coding_agent_final.py <workdir> <task>")
        print("")
        print("Example:")
        print('  python3 coding_agent_final.py /tmp/project "Create hello.py"')
        sys.exit(1)
    
    workdir = sys.argv[1]
    task = sys.argv[2]
    
    wrapper = CodingAgentWrapper(debug=True)
    result = wrapper.execute(task, workdir, timeout=300)
    
    if result['success']:
        print(f"\n✅ Success!")
        print(f"Auto-confirmations: {result['auto_confirms']}")
        sys.exit(0)
    else:
        print(f"\n❌ Failed: {result.get('error', 'Unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
