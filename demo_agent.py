#!/usr/bin/env python3
"""
快速演示 - Coding Agent Wrapper

最简单的使用示例
"""

import sys
import os
import tempfile
import subprocess

# 添加当前目录到 path
sys.path.insert(0, os.path.dirname(__file__))

from coding_agent_wrapper import CodingAgent, AgentConfig


def main():
    print("🚀 Coding Agent Wrapper - Quick Demo")
    print("="*60)
    
    # 创建临时测试目录
    test_dir = tempfile.mkdtemp(prefix="demo-")
    print(f"📁 Test directory: {test_dir}")
    
    # 初始化 git
    subprocess.run(["git", "init"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "demo@test.com"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Demo"], cwd=test_dir, capture_output=True)
    
    # 配置 agent
    config = AgentConfig(
        tool="claude",
        auto_approve=True,
        timeout=300,  # 增加到 5 分钟
        idle_timeout=15,  # 增加空闲超时
        debug=True,  # 显示详细日志
    )
    
    agent = CodingAgent(config)
    
    print("\n📝 Task: Create a simple Python calculator")
    print("-"*60)
    
    # 执行任务
    result = agent.execute(
        task="""Create a Python script 'calc.py' with:
        - add(a, b) function
        - subtract(a, b) function  
        - A main block that demonstrates both functions
        """,
        workdir=test_dir
    )
    
    # 显示结果
    print("\n" + "="*60)
    print("📊 RESULTS")
    print("="*60)
    print(f"Status: {result.status}")
    print(f"Duration: {result.duration:.1f} seconds")
    print(f"Files changed: {result.files_changed or '(none detected)'}")
    
    if result.error_message:
        print(f"\n❌ Error: {result.error_message}")
    
    # 检查文件
    calc_file = os.path.join(test_dir, "calc.py")
    if os.path.exists(calc_file):
        print(f"\n✅ File created: calc.py")
        with open(calc_file, 'r') as f:
            content = f.read()
        print(f"\n📄 Content:\n{'-'*60}\n{content}\n{'-'*60}")
    else:
        print(f"\n❌ File NOT found: calc.py")
    
    # 保存完整日志
    log_file = os.path.join(test_dir, "full-log.txt")
    with open(log_file, 'w') as f:
        f.write(result.output_log)
    print(f"\n📋 Full log saved to: {log_file}")
    
    print(f"\n💡 Test directory preserved at: {test_dir}")
    print("You can inspect the files manually.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ Demo interrupted")
    except Exception as e:
        print(f"\n❌ Demo failed: {e}")
        import traceback
        traceback.print_exc()
