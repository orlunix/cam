#!/usr/bin/env python3
"""
CM SSH Persistent Connection
使用 SSH ControlMaster 实现连接复用，避免频繁建立新连接
"""

import subprocess
import time
import os
import tempfile
from typing import Optional, List, Dict

class PersistentSSHSession:
    """持久 SSH 连接管理器 - 使用 ControlMaster"""
    
    def __init__(self, host: str, port: int = 22, user: str = None):
        self.host = host
        self.port = port
        self.user = user or "hren"
        self.control_path = None
        self.session_id = None
        self._setup_control_master()
    
    def _setup_control_master(self):
        """设置 SSH ControlMaster"""
        # 创建控制套接字路径
        tmpdir = tempfile.gettempdir()
        self.control_path = os.path.join(tmpdir, f'ssh-cm-{self.user}@{self.host}:{self.port}')
        
        print(f"🔧 Setting up SSH ControlMaster")
        print(f"   Control socket: {self.control_path}")
        
        # 启动主连接（后台运行）
        master_cmd = [
            'ssh',
            '-fN',  # 后台运行，不执行命令
            '-M',   # Master mode
            '-S', self.control_path,  # Control socket path
            '-o', 'ControlPersist=10m',  # 保持连接 10 分钟
            '-o', 'ServerAliveInterval=60',  # 每 60 秒发送心跳
            '-o', 'ServerAliveCountMax=3',   # 最多 3 次失败
            '-p', str(self.port),
            f'{self.user}@{self.host}'
        ]
        
        try:
            subprocess.run(master_cmd, check=True, timeout=10)
            print(f"✅ SSH ControlMaster established (with keep-alive)")
            time.sleep(0.5)  # 等待连接稳定
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to establish ControlMaster: {e}")
            raise
    
    def _ssh_cmd(self, remote_cmd: str, timeout: int = 10) -> tuple:
        """通过已建立的连接执行命令（复用连接）"""
        ssh_cmd = [
            'ssh',
            '-S', self.control_path,  # 使用现有控制连接
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
    
    def batch_commands(self, commands: List[str], work_dir: str = None) -> Dict:
        """批量发送命令（一次 SSH 连接）"""
        # 构建完整的命令脚本
        script_lines = []
        
        if work_dir:
            script_lines.append(f'cd {work_dir}')
        
        script_lines.extend(commands)
        
        # 用分号连接所有命令
        full_cmd = ' && '.join(script_lines)
        
        print(f"📦 Sending {len(commands)} commands in one SSH call...")
        stdout, stderr, code = self._ssh_cmd(full_cmd)
        
        return {
            'success': code == 0,
            'output': stdout,
            'error': stderr if code != 0 else None,
            'command_count': len(commands)
        }
    
    def create_session(self, work_dir: str, task: str = "") -> Dict:
        """创建 TMUX session（通过已有连接）"""
        self.session_id = f"cm-{int(time.time())}"
        
        cmd = f'tmux new-session -d -s {self.session_id} -c {work_dir}'
        stdout, stderr, code = self._ssh_cmd(cmd)
        
        if code != 0:
            return {'success': False, 'error': stderr}
        
        return {
            'success': True,
            'session_id': self.session_id,
            'work_dir': work_dir
        }
    
    def send_keys_batch(self, commands: List[str]) -> Dict:
        """批量发送按键到 TMUX（一次 SSH 连接）"""
        if not self.session_id:
            return {'success': False, 'error': 'No active session'}
        
        # 构建批量 tmux 命令
        tmux_cmds = []
        for cmd in commands:
            escaped = cmd.replace('"', '\\"')
            tmux_cmds.append(f'tmux send-keys -t {self.session_id} "{escaped}" C-m')
        
        # 用分号连接
        full_cmd = ' && '.join(tmux_cmds)
        
        print(f"📤 Sending {len(commands)} commands to TMUX...")
        stdout, stderr, code = self._ssh_cmd(full_cmd)
        
        return {
            'success': code == 0,
            'command_count': len(commands),
            'error': stderr if code != 0 else None
        }
    
    def capture_output(self, lines: int = 50) -> Dict:
        """捕获输出（通过已有连接）"""
        if not self.session_id:
            return {'success': False, 'error': 'No active session'}
        
        cmd = f'tmux capture-pane -t {self.session_id} -p -S -{lines}'
        stdout, stderr, code = self._ssh_cmd(cmd)
        
        return {
            'success': code == 0,
            'output': stdout if code == 0 else None,
            'error': stderr if code != 0 else None
        }
    
    def execute_workflow(self, work_dir: str, commands: List[str], 
                         capture_interval: float = 0.5) -> Dict:
        """完整工作流：创建session，执行命令，捕获输出"""
        # 1. 创建 session
        session_result = self.create_session(work_dir)
        if not session_result['success']:
            return session_result
        
        # 2. 批量发送命令
        send_result = self.send_keys_batch(commands)
        if not send_result['success']:
            return send_result
        
        # 3. 等待执行
        time.sleep(capture_interval * len(commands))
        
        # 4. 捕获输出
        output_result = self.capture_output(lines=100)
        
        return {
            'success': True,
            'session_id': self.session_id,
            'work_dir': work_dir,
            'commands': commands,
            'output': output_result.get('output', '')
        }
    
    def check_connection(self) -> bool:
        """检查连接是否活跃"""
        check_cmd = [
            'ssh',
            '-S', self.control_path,
            '-O', 'check',
            f'{self.user}@{self.host}'
        ]
        
        result = subprocess.run(check_cmd, capture_output=True, text=True)
        return result.returncode == 0
    
    def close(self):
        """关闭主连接"""
        if self.session_id:
            # 清理 TMUX session
            self._ssh_cmd(f'tmux kill-session -t {self.session_id} 2>/dev/null')
        
        # 关闭 ControlMaster
        close_cmd = [
            'ssh',
            '-S', self.control_path,
            '-O', 'exit',
            f'{self.user}@{self.host}'
        ]
        
        subprocess.run(close_cmd, capture_output=True)
        print(f"👋 SSH ControlMaster closed")
    
    def __enter__(self):
        """Context manager 支持"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """自动清理"""
        self.close()


def demo():
    """演示持久连接使用"""
    print("🚀 CM Persistent SSH Connection Demo\n")
    
    # 使用 context manager 自动管理连接
    with PersistentSSHSession(
        host='pdx-container-xterm-110.prd.it.nvidia.com',
        port=3859,
        user='hren'
    ) as ssh:
        
        # 测试连接
        if ssh.check_connection():
            print("✅ SSH connection is active\n")
        
        # 方式 1: 批量执行命令（不用 TMUX）
        print("📦 Method 1: Batch commands (no TMUX)")
        result1 = ssh.batch_commands(
            commands=[
                'pwd',
                'hostname',
                'date',
                'echo "Batch test"'
            ],
            work_dir='/home/scratch.hren_gpu/test/fd/feynman-211_peregrine_add_memory_ecc'
        )
        
        if result1['success']:
            print(f"✅ Sent {result1['command_count']} commands in ONE SSH call")
            print(f"Output:\n{result1['output']}")
        print("-" * 60)
        
        # 方式 2: TMUX workflow
        print("\n📦 Method 2: TMUX workflow")
        result2 = ssh.execute_workflow(
            work_dir='/home/scratch.hren_gpu/test/fd/feynman-211_peregrine_add_memory_ecc',
            commands=[
                'ls -lh | head -5',
                'git log --oneline -3',
                'echo "Workflow completed"'
            ]
        )
        
        if result2['success']:
            print(f"✅ Session: {result2['session_id']}")
            print(f"✅ Executed {len(result2['commands'])} commands")
            print(f"Output:\n{result2['output']}")
        
        print("\n✅ All operations used ONE persistent SSH connection!")


if __name__ == '__main__':
    demo()
