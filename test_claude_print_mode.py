#!/usr/bin/env python3
"""
测试 claude -p 模式（跳过安全检查）
"""

import subprocess
import tempfile
import os

# 创建测试目录
test_dir = tempfile.mkdtemp(prefix="claude-print-test-")
subprocess.run(["git", "init"], cwd=test_dir, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=test_dir, capture_output=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=test_dir, capture_output=True)

print(f"📁 Test directory: {test_dir}")
print("🚀 Running claude with -p mode...")
print("="*60)

# 使用 -p 模式（跳过安全对话框）
result = subprocess.run(
    ["claude", "-p", "Create a simple hello.py file that prints 'Hello World'"],
    cwd=test_dir,
    capture_output=True,
    text=True,
    timeout=60,
)

print("📤 STDOUT:")
print(result.stdout)

print("\n📤 STDERR:")
print(result.stderr)

print("\n" + "="*60)
print(f"Exit code: {result.returncode}")

# 检查文件
hello_py = os.path.join(test_dir, "hello.py")
if os.path.exists(hello_py):
    print(f"\n✅ File created: hello.py")
    with open(hello_py, 'r') as f:
        content = f.read()
    print(f"\n📄 Content:\n{'-'*60}\n{content}\n{'-'*60}")
else:
    print(f"\n❌ File NOT created")
    print(f"\n📂 Directory contents:")
    for item in os.listdir(test_dir):
        if not item.startswith('.'):
            print(f"  - {item}")

print(f"\n💡 Test directory: {test_dir}")
