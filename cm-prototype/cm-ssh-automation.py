#!/usr/bin/env python3
"""
CM Remote Session Manager - SSH-based automation
直接 SSH + TMUX 实现远程会话管理，无需 Agent Server
"""

import subprocess
import time
import json
import uuid
from typing import Optional, Dict, List

class SSHRemoteSession:
    """通过 SSH 管理远程 TMUX session"""
    
    def __init__(self, host: str, port: int = 22, user: str = None):
        self.host = host
        self.port = port
        self.user = user or "hren"
        self.session_id = None
        
    def _ssh_cmd(self, remote_cmd: str, timeout: int = 10) -> tuple:
        """执行 SSH 命令并返回 (stdout, stderr, returncode)"""
        ssh_cmd = [
            'ssh',
            '-p', str(self.port),
            f'{self.user}@{self.host}',
            remote_cmd
        ]
        
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return '', 'Timeout', -1
        except Exception as e:
            return '', str(e), -1
    
    def create_session(self, work_dir: str, task: str = "") -> dict:
        """创建远程 TMUX session"""
        # 生成 session ID
        self.session_id = f"cm-{int(time.time())}"
        
        # 创建 TMUX session
        cmd = f'tmux new-session -d -s {self.session_id} -c {work_dir}'
        stdout, stderr, code = self._ssh_cmd(cmd)
        
        if code != 0:
            return {
                'success': False,
                'error': f'Failed to create session: {stderr}'
            }
        
        # 发送初始命令
        if task:
            self.send_keys(f'echo "Task: {task}"')
            self.send_keys('echo "Session ready"')
        
        return {
            'success': True,
            'session_id': self.session_id,
            'work_dir': work_dir,
            'task': task
        }
    
    def send_keys(self, keys: str, literal: bool = False) -> dict:
        """发送按键到 TMUX session"""
        if not self.session_id:
            return {'success': False, 'error': 'No active session'}
        
        # 转义特殊字符
        if literal:
            keys_escaped = keys.replace('"', '\\"')
        else:
            keys_escaped = keys
        
        cmd = f'tmux send-keys -t {self.session_id} "{keys_escaped}" C-m'
        stdout, stderr, code = self._ssh_cmd(cmd)
        
        return {
            'success': code == 0,
            'error': stderr if code != 0 else None
        }
    
    def capture_output(self, lines: int = 50) -> dict:
        """捕获 TMUX session 输出"""
        if not self.session_id:
            return {'success': False, 'error': 'No active session'}
        
        cmd = f'tmux capture-pane -t {self.session_id} -p -S -{lines}'
        stdout, stderr, code = self._ssh_cmd(cmd)
        
        return {
            'success': code == 0,
            'output': stdout if code == 0 else None,
            'error': stderr if code != 0 else None
        }
    
    def list_sessions(self) -> dict:
        """列出所有 TMUX sessions"""
        cmd = 'tmux list-sessions 2>/dev/null'
        stdout, stderr, code = self._ssh_cmd(cmd)
        
        if code != 0:
            return {'success': False, 'sessions': []}
        
        sessions = []
        for line in stdout.strip().split('\n'):
            if line:
                # Parse: session-name: N windows (created ...)
                parts = line.split(':')
                if len(parts) >= 2:
                    sessions.append({
                        'name': parts[0].strip(),
                        'info': ':'.join(parts[1:]).strip()
                    })
        
        return {
            'success': True,
            'sessions': sessions
        }
    
    def session_exists(self, session_id: str = None) -> bool:
        """检查 session 是否存在"""
        sid = session_id or self.session_id
        if not sid:
            return False
        
        cmd = f'tmux has-session -t {sid} 2>/dev/null'
        _, _, code = self._ssh_cmd(cmd, timeout=5)
        return code == 0
    
    def kill_session(self, session_id: str = None) -> dict:
        """终止 TMUX session"""
        sid = session_id or self.session_id
        if not sid:
            return {'success': False, 'error': 'No session specified'}
        
        cmd = f'tmux kill-session -t {sid} 2>/dev/null'
        stdout, stderr, code = self._ssh_cmd(cmd)
        
        if sid == self.session_id:
            self.session_id = None
        
        return {
            'success': code == 0,
            'error': stderr if code != 0 else None
        }
    
    def attach_info(self) -> dict:
        """获取附加到 session 的命令"""
        if not self.session_id:
            return {'success': False, 'error': 'No active session'}
        
        return {
            'success': True,
            'attach_cmd': f'ssh -p {self.port} {self.user}@{self.host} -t "tmux attach -t {self.session_id}"'
        }
    
    def execute_task(self, work_dir: str, commands: List[str], task: str = "") -> dict:
        """执行完整任务流程"""
        # 1. 创建 session
        result = self.create_session(work_dir, task)
        if not result['success']:
            return result
        
        session_id = result['session_id']
        
        # 2. 执行命令序列
        outputs = []
        for cmd in commands:
            # 发送命令
            send_result = self.send_keys(cmd)
            if not send_result['success']:
                return {
                    'success': False,
                    'session_id': session_id,
                    'error': f'Failed to send command: {cmd}',
                    'outputs': outputs
                }
            
            # 等待执行
            time.sleep(0.5)
            
            # 捕获输出
            capture_result = self.capture_output()
            if capture_result['success']:
                outputs.append({
                    'command': cmd,
                    'output': capture_result['output']
                })
        
        # 3. 返回结果
        return {
            'success': True,
            'session_id': session_id,
            'work_dir': work_dir,
            'task': task,
            'outputs': outputs
        }


def demo():
    """演示自动化使用"""
    print("🚀 CM Remote Session Manager - SSH Automation Demo\n")
    
    # 连接到远程
    remote = SSHRemoteSession(
        host='pdx-container-xterm-110.prd.it.nvidia.com',
        port=3859,
        user='hren'
    )
    
    # 列出现有 sessions
    print("📋 Listing existing sessions...")
    sessions = remote.list_sessions()
    if sessions['success']:
        for s in sessions['sessions']:
            print(f"   - {s['name']}: {s['info']}")
    print()
    
    # 执行自动化任务
    print("🎯 Executing automated task...")
    work_dir = "/home/scratch.hren_gpu/test/fd/feynman-211_peregrine_add_memory_ecc"
    
    commands = [
        'pwd',
        'echo "Starting analysis..."',
        'ls -lh | head -10',
        'git log --oneline -5',
        'echo "Task completed!"'
    ]
    
    result = remote.execute_task(
        work_dir=work_dir,
        commands=commands,
        task="Quick project analysis"
    )
    
    if result['success']:
        print(f"✅ Session created: {result['session_id']}\n")
        
        # 显示输出
        for i, output_item in enumerate(result['outputs'], 1):
            print(f"📤 Command {i}: {output_item['command']}")
            print(f"📥 Output:")
            print(output_item['output'])
            print("-" * 60)
        
        # 获取附加命令
        attach = remote.attach_info()
        if attach['success']:
            print(f"\n💡 To attach to session:")
            print(f"   {attach['attach_cmd']}")
        
        # 清理
        print(f"\n🧹 Cleaning up session...")
        remote.kill_session()
        print("✅ Session terminated")
    else:
        print(f"❌ Task failed: {result.get('error')}")


if __name__ == '__main__':
    demo()
