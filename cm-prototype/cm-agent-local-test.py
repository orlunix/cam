#!/usr/bin/env python3
"""
CM Agent Server - Local Test Version (No WebSocket required)
测试核心 TMUX 管理功能
"""

import subprocess
import time
import re
import os
from typing import Optional

class TmuxSession:
    """TMUX Session 管理"""
    
    def __init__(self, session_id: str, socket: str, tool: str, context_path: str):
        self.session_id = session_id
        self.socket = socket
        self.tool = tool
        self.context_path = context_path
        self.state = "starting"
        self.created_at = time.time()
        self.last_output = ""
    
    def execute(self, cmd: str) -> str:
        """执行命令"""
        try:
            result = subprocess.run(
                cmd, shell=True,
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )
            return result.stdout
        except Exception as e:
            return f"Error: {e}"
    
    def send_keys(self, keys: str):
        """发送按键"""
        cmd = f"tmux -S {self.socket} send-keys -t {self.session_id}:0.0 -l -- '{keys}'"
        self.execute(cmd)
        time.sleep(0.1)
        cmd = f"tmux -S {self.socket} send-keys -t {self.session_id}:0.0 Enter"
        self.execute(cmd)
    
    def capture_pane(self, lines: int = 50) -> str:
        """捕获输出"""
        cmd = f"tmux -S {self.socket} capture-pane -p -J -t {self.session_id}:0.0 -S -{lines}"
        return self.execute(cmd)
    
    def exists(self) -> bool:
        """检查是否存在"""
        cmd = f"tmux -S {self.socket} has-session -t {self.session_id} 2>/dev/null"
        return subprocess.run(cmd, shell=True).returncode == 0
    
    def kill(self):
        """终止 session"""
        cmd = f"tmux -S {self.socket} kill-session -t {self.session_id} 2>/dev/null"
        subprocess.run(cmd, shell=True)


def detect_state(output: str) -> str:
    """检测状态"""
    last_lines = output[-1000:]
    
    if re.search(r'(Planning|Thinking|Analyzing)', last_lines, re.I):
        return 'planning'
    elif re.search(r'(Editing|Writing|Making changes)', last_lines, re.I):
        return 'editing'
    elif re.search(r'(Running tests|Testing)', last_lines, re.I):
        return 'testing'
    elif re.search(r'(Done|Completed|Finished)', last_lines, re.I):
        return 'done'
    elif re.search(r'(Error|Failed)', last_lines, re.I):
        return 'failed'
    else:
        return 'running'


def should_auto_confirm(output: str) -> bool:
    """检测是否需要自动确认"""
    last_lines = output[-500:]
    
    patterns = [
        r'\(y/n\)',
        r'\[Y/n\]',
        r'Continue\?',
        r'Do you want',
        r'Apply.*\?',
        r'Accept.*\?',
        r'❯.*1\..*Yes',
    ]
    
    return any(re.search(p, last_lines, re.I) for p in patterns)


def create_and_monitor_session(tool: str, task: str, context_path: str, duration: int = 60):
    """创建并监控 session（本地测试）"""
    
    socket_dir = "/tmp/cm-tmux-sockets"
    os.makedirs(socket_dir, exist_ok=True)
    
    # 生成 session ID
    session_id = f"local-test-{int(time.time())}"
    socket = f"{socket_dir}/{session_id}.sock"
    
    print(f"🚀 Creating TMUX session: {session_id}")
    print(f"   Tool: {tool}")
    print(f"   Task: {task}")
    print(f"   Path: {context_path}")
    print()
    
    # 创建 session
    subprocess.run([
        'tmux', '-S', socket,
        'new-session', '-d', '-s', session_id
    ], check=True)
    
    session = TmuxSession(session_id, socket, tool, context_path)
    
    # 启动工具
    tool_cmd = f"cd '{context_path}' && {tool}"
    session.send_keys(tool_cmd)
    time.sleep(2)
    
    # 发送任务
    if tool == 'claude' and task:
        time.sleep(1)
        
        # 检查 trust prompt
        output = session.capture_pane(30)
        if 'trust this folder' in output.lower():
            print("   [AUTO] Trust folder prompt detected")
            session.send_keys('')
            time.sleep(1)
        
        # 发送任务
        print(f"   Sending task...")
        session.send_keys(task)
    
    print(f"✅ Session created and started")
    print()
    
    # 监控循环
    print("👀 Monitoring session...")
    print("-" * 60)
    
    start_time = time.time()
    iteration = 0
    
    while session.exists() and (time.time() - start_time) < duration:
        iteration += 1
        
        # 捕获输出
        output = session.capture_pane(50)
        
        # 检查变化
        if output != session.last_output:
            session.last_output = output
            
            # 检测状态
            old_state = session.state
            session.state = detect_state(output)
            
            if session.state != old_state:
                print(f"[{iteration:3d}] State: {old_state} → {session.state}")
            
            # 自动确认
            if should_auto_confirm(output):
                print(f"[{iteration:3d}] [AUTO] Confirming prompt...")
                
                if re.search(r'❯.*1\..*Yes|^\s*1\.', output[-500:], re.MULTILINE):
                    cmd = f"tmux -S {socket} send-keys -t {session_id}:0.0 Enter"
                    session.execute(cmd)
                else:
                    session.send_keys('y')
            
            # 检查完成
            if session.state in ['done', 'failed']:
                print(f"[{iteration:3d}] Completed with state: {session.state}")
                break
        
        time.sleep(2)
    
    print("-" * 60)
    print()
    
    # 最终输出
    print("📸 Final output:")
    print("-" * 60)
    final_output = session.capture_pane(30)
    print(final_output)
    print("-" * 60)
    print()
    
    # 保持 session
    print(f"📋 Session info:")
    print(f"   ID: {session_id}")
    print(f"   Socket: {socket}")
    print(f"   Attach: tmux -S {socket} attach -t {session_id}")
    print(f"   Kill:   tmux -S {socket} kill-session -t {session_id}")
    print()
    
    return session_id, session.state


def main():
    """主函数"""
    print("=" * 60)
    print("CM Agent Server - Local Test (No WebSocket)")
    print("=" * 60)
    print()
    
    # 测试参数
    tool = 'claude'
    task = '创建文件 local-agent-test.txt 内容是 "Local Agent Test Passed!"'
    context_path = '/home/hren/.openclaw/workspace'
    
    try:
        session_id, final_state = create_and_monitor_session(
            tool=tool,
            task=task,
            context_path=context_path,
            duration=60
        )
        
        print("=" * 60)
        print(f"✅ Test Complete")
        print(f"   Session: {session_id}")
        print(f"   Final State: {final_state}")
        print("=" * 60)
        print()
        
        # 检查文件是否创建
        test_file = f"{context_path}/local-agent-test.txt"
        if os.path.exists(test_file):
            print("🎉 Success! Test file created:")
            with open(test_file, 'r') as f:
                print(f"   Content: {f.read().strip()}")
        else:
            print("⏳ Test file not created yet (might still be in progress)")
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
