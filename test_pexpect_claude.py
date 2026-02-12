#!/usr/bin/env python3
"""
使用 pexpect 的版本 - 更可靠的交互式程序控制
"""

import pexpect
import sys
import os
import time
import tempfile
import subprocess

def test_claude_with_pexpect():
    """使用 pexpect 测试 Claude Code"""
    
    # 创建测试目录
    test_dir = tempfile.mkdtemp(prefix="pexpect-test-")
    subprocess.run(["git", "init"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=test_dir, capture_output=True)
    
    print(f"📁 Test directory: {test_dir}")
    print("🚀 Starting Claude Code with pexpect...")
    print("="*60)
    
    try:
        # 启动 Claude Code
        child = pexpect.spawn(
            'claude',
            args=["Create a simple hello.py file that prints 'Hello World'"],
            cwd=test_dir,
            timeout=120,
            encoding='utf-8',
            echo=False,
        )
        
        # 打印所有输出
        child.logfile = sys.stdout
        
        print("\n🔍 Waiting for safety prompt...")
        
        # 等待安全提示（使用更宽松的匹配）
        index = child.expect([
            r'Is this a project',  # 更短、更宽松
            r'Do you want to proceed',
            pexpect.TIMEOUT,
            pexpect.EOF,
        ], timeout=30)
        
        if index == 0:
            print("\n✅ Got safety prompt!")
            print("📤 Sending: 1")
            child.sendline('1')
            time.sleep(1)
            
        elif index == 1:
            print("\n✅ Got approval prompt!")
            print("📤 Sending: 1")
            child.sendline('1')
            time.sleep(1)
            
        elif index == 2:
            print("\n⏰ Timeout waiting for prompt")
            print(f"Last output: {child.before}")
            
        elif index == 3:
            print("\n❌ Process ended early")
            return
        
        # 继续等待后续的确认
        print("\n🔍 Waiting for more prompts...")
        while True:
            try:
                index = child.expect([
                    r'Do you want to proceed',
                    r'Yes.*trust',  # 匹配 "Yes, I trust this folder"
                    r'Continue',
                    r'esc to interrupt',  # 完成信号
                    pexpect.TIMEOUT,
                    pexpect.EOF,
                ], timeout=10)
                
                if index == 0 or index == 1:
                    print(f"\n✅ Got approval prompt (type {index})")
                    print("📤 Sending: 1")
                    child.sendline('1')
                    time.sleep(0.5)
                    
                elif index == 2:
                    print("\n✅ Got continue prompt")
                    print("📤 Sending: Enter")
                    child.sendline('')
                    time.sleep(0.5)
                    
                elif index == 3:
                    print("\n✅ Task completed!")
                    break
                    
                elif index == 4:
                    print("\n⏰ Timeout - checking if done...")
                    # 可能已经完成了
                    break
                    
                elif index == 5:
                    print("\n✅ Process ended")
                    break
                    
            except pexpect.TIMEOUT:
                print("\n⏰ No more prompts, assuming done")
                break
        
        # 等待进程结束
        child.close()
        
        print("\n" + "="*60)
        print("📊 Results:")
        print(f"Exit code: {child.exitstatus}")
        
        # 检查文件
        hello_py = os.path.join(test_dir, "hello.py")
        if os.path.exists(hello_py):
            print(f"\n✅ File created: hello.py")
            with open(hello_py, 'r') as f:
                content = f.read()
            print(f"\n📄 Content:\n{'-'*60}\n{content}\n{'-'*60}")
        else:
            print(f"\n❌ File NOT created")
            
            # 列出目录内容
            print(f"\n📂 Directory contents:")
            for item in os.listdir(test_dir):
                print(f"  - {item}")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"\n💡 Test directory: {test_dir}")


if __name__ == "__main__":
    test_claude_with_pexpect()
