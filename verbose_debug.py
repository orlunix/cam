#!/usr/bin/env python3
"""
详细调试 - 显示每次状态更新和输出chunk
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from coding_agent_wrapper import CodingAgent, AgentConfig, OutputParser
import tempfile
import subprocess

# Monkey-patch _read_output 来打印每个 chunk
original_read = CodingAgent._read_output

def verbose_read(self, timeout):
    output = original_read(self, timeout)
    if output:
        print(f"\n📥 [RAW CHUNK {len(output)} bytes]")
        print(repr(output[:200]))
        
        clean = OutputParser.strip_ansi(output)
        print(f"\n📄 [CLEAN {len(clean)} bytes]")
        print(clean[:200])
    return output

CodingAgent._read_output = verbose_read

# 创建测试目录
test_dir = tempfile.mkdtemp(prefix="verbose-debug-")
subprocess.run(["git", "init"], cwd=test_dir, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=test_dir, capture_output=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=test_dir, capture_output=True)

print(f"📁 Test dir: {test_dir}")
print("="*60)

config = AgentConfig(
    tool="claude",
    auto_approve=True,
    timeout=60,  # 短一点
    idle_timeout=8,
    debug=True,
)

agent = CodingAgent(config)

result = agent.execute(
    task="Create hello.py that prints 'Hello'",
    workdir=test_dir
)

print("\n" + "="*60)
print(f"Result: {result.status}")
print(f"Duration: {result.duration:.1f}s")
