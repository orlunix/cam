#!/usr/bin/env python3
"""
调试脚本 - 直接测试 Claude Code 的交互

模拟手动输入来看看 Claude Code 到底期望什么
"""

import os
import pty
import sys
import time
import select
import subprocess

def test_claude_interactive():
    """测试 Claude Code 的交互"""
    
    workdir = "/tmp/test-claude-debug"
    os.makedirs(workdir, exist_ok=True)
    
    # 初始化 git
    subprocess.run(["git", "init"], cwd=workdir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=workdir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, capture_output=True)
    
    print(f"📁 Working in: {workdir}")
    print("🚀 Starting Claude Code...")
    print("="*60)
    
    # 启动 Claude Code
    master, slave = pty.openpty()
    
    process = subprocess.Popen(
        ["claude", "Create a simple hello.py file"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=workdir,
        preexec_fn=os.setsid,
    )
    
    os.close(slave)
    
    print("\n📊 Output from Claude Code:")
    print("-"*60)
    
    buffer = []
    last_chunk_time = time.time()
    
    try:
        while True:
            # 读取输出
            ready, _, _ = select.select([master], [], [], 0.5)
            
            if ready:
                try:
                    data = os.read(master, 4096)
                    if data:
                        text = data.decode('utf-8', errors='replace')
                        buffer.append(text)
                        print(text, end='', flush=True)
                        last_chunk_time = time.time()
                        
                        # 检测安全提示
                        recent = "".join(buffer[-20:])
                        if "Is this a project you created or one you trust?" in recent:
                            print("\n\n🔍 Detected safety prompt!")
                            print("📤 Sending: 1")
                            time.sleep(1)
                            os.write(master, b"1")
                            print("📤 Sending: Enter")
                            time.sleep(0.5)
                            os.write(master, b"\n")
                            
                        # 检测其他确认
                        elif "Do you want to proceed?" in recent:
                            print("\n\n🔍 Detected approval prompt!")
                            print("📤 Sending: 1")
                            time.sleep(0.5)
                            os.write(master, b"1\n")
                            
                except OSError:
                    break
            
            # 检查进程是否还在运行
            if process.poll() is not None:
                print("\n\n✅ Process exited")
                break
            
            # 超时检查
            idle = time.time() - last_chunk_time
            if idle > 30:
                print(f"\n\n⏰ No output for {idle:.1f}s, stopping...")
                break
                
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user")
    
    finally:
        # 清理
        try:
            process.terminate()
            process.wait(timeout=5)
        except:
            process.kill()
        
        try:
            os.close(master)
        except:
            pass
    
    print("\n" + "="*60)
    print(f"📋 Full output saved to buffer ({len(buffer)} chunks)")
    
    # 检查文件
    hello_py = os.path.join(workdir, "hello.py")
    if os.path.exists(hello_py):
        print(f"✅ File created: hello.py")
        with open(hello_py, 'r') as f:
            print(f"\n📄 Content:\n{f.read()}")
    else:
        print(f"❌ File NOT created: hello.py")


if __name__ == "__main__":
    test_claude_interactive()
