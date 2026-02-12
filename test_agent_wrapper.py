#!/usr/bin/env python3
"""
测试 Coding Agent Wrapper

演示如何使用自动化的编码 agent
"""

import os
import sys
import tempfile
import subprocess
from pathlib import Path

# 添加当前目录到 Python path
sys.path.insert(0, os.path.dirname(__file__))

from coding_agent_wrapper import CodingAgent, AgentConfig


def setup_test_project():
    """创建测试项目目录"""
    test_dir = tempfile.mkdtemp(prefix="agent-test-")
    
    # 初始化 git（某些工具需要）
    subprocess.run(["git", "init"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=test_dir, capture_output=True)
    
    # 创建初始 commit
    readme = Path(test_dir) / "README.md"
    readme.write_text("# Test Project\n")
    subprocess.run(["git", "add", "README.md"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=test_dir, capture_output=True)
    
    print(f"📁 Test project created: {test_dir}")
    return test_dir


def test_simple_task():
    """测试 1: 简单任务"""
    print("\n" + "="*60)
    print("TEST 1: Simple Python script")
    print("="*60)
    
    test_dir = setup_test_project()
    
    config = AgentConfig(
        tool="claude",
        auto_approve=True,
        timeout=120,
        idle_timeout=8,
        debug=True,
    )
    
    agent = CodingAgent(config)
    
    result = agent.execute(
        task="Create a Python script called 'hello.py' that prints 'Hello, World!' and the current date.",
        workdir=test_dir
    )
    
    print_result(result, test_dir)
    
    # 检查文件是否创建
    hello_py = Path(test_dir) / "hello.py"
    if hello_py.exists():
        print(f"\n✅ File created: hello.py")
        print(f"Content:\n{hello_py.read_text()}")
    else:
        print(f"\n❌ File not created: hello.py")
    
    return result


def test_multiple_files():
    """测试 2: 多文件任务"""
    print("\n" + "="*60)
    print("TEST 2: Multi-file project")
    print("="*60)
    
    test_dir = setup_test_project()
    
    config = AgentConfig(
        tool="claude",
        auto_approve=True,
        timeout=180,
        idle_timeout=10,
        debug=True,
    )
    
    agent = CodingAgent(config)
    
    result = agent.execute(
        task="""Create a simple calculator module with:
        1. calc.py - contains add, subtract, multiply, divide functions
        2. test_calc.py - contains unit tests using pytest
        """,
        workdir=test_dir
    )
    
    print_result(result, test_dir)
    
    # 检查文件
    for filename in ["calc.py", "test_calc.py"]:
        filepath = Path(test_dir) / filename
        if filepath.exists():
            print(f"\n✅ Created: {filename}")
        else:
            print(f"\n❌ Missing: {filename}")
    
    return result


def test_code_modification():
    """测试 3: 修改已有代码"""
    print("\n" + "="*60)
    print("TEST 3: Code modification")
    print("="*60)
    
    test_dir = setup_test_project()
    
    # 创建初始文件
    initial_code = """def greet(name):
    return f"Hello, {name}!"

if __name__ == "__main__":
    print(greet("World"))
"""
    
    greet_py = Path(test_dir) / "greet.py"
    greet_py.write_text(initial_code)
    
    # Commit 初始文件
    subprocess.run(["git", "add", "greet.py"], cwd=test_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add greet.py"], cwd=test_dir, capture_output=True)
    
    print(f"📝 Initial greet.py created")
    
    # 让 agent 修改
    config = AgentConfig(
        tool="claude",
        auto_approve=True,
        timeout=120,
        idle_timeout=8,
        debug=True,
    )
    
    agent = CodingAgent(config)
    
    result = agent.execute(
        task="Add error handling to greet.py: check if name is empty and raise ValueError",
        workdir=test_dir
    )
    
    print_result(result, test_dir)
    
    # 显示修改
    print(f"\n📄 Modified greet.py:")
    print(greet_py.read_text())
    
    return result


def print_result(result, test_dir):
    """打印结果"""
    print("\n" + "-"*60)
    print(f"Status: {result.status}")
    print(f"Duration: {result.duration:.1f}s")
    print(f"Files changed: {result.files_changed or '(none detected)'}")
    
    if result.error_message:
        print(f"Error: {result.error_message}")
    
    # 保存日志
    log_file = Path(test_dir) / "agent-output.log"
    log_file.write_text(result.output_log)
    print(f"\n📋 Full log: {log_file}")
    
    # 显示输出摘要
    clean_log = result.output_log[-500:]  # 最后 500 字符
    print(f"\n📊 Output (last 500 chars):")
    print(clean_log)
    print("-"*60)


def main():
    """主函数"""
    print("🧪 Coding Agent Wrapper - Test Suite")
    print("="*60)
    
    tests = [
        ("Simple Task", test_simple_task),
        ("Multiple Files", test_multiple_files),
        ("Code Modification", test_code_modification),
    ]
    
    # 让用户选择测试
    print("\nAvailable tests:")
    for i, (name, _) in enumerate(tests, 1):
        print(f"{i}. {name}")
    print(f"{len(tests)+1}. Run all")
    
    try:
        choice = input("\nSelect test (1-{}): ".format(len(tests)+1)).strip()
        choice = int(choice)
        
        if choice == len(tests) + 1:
            # 运行所有测试
            for name, test_func in tests:
                print(f"\n{'='*60}")
                print(f"Running: {name}")
                print(f"{'='*60}")
                test_func()
        elif 1 <= choice <= len(tests):
            # 运行选定测试
            name, test_func = tests[choice-1]
            test_func()
        else:
            print("Invalid choice")
            
    except KeyboardInterrupt:
        print("\n\n⚠️ Test interrupted")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
