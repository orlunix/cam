#!/usr/bin/env python3
"""
Coding Agent Wrapper - 简化的 Python 版本

直接使用 pexpect.expect() 而不是日志文件轮询
"""

import pexpect
import sys
import os
import time
import re


def execute_claude_task(workdir: str, task: str, timeout: int = 300, debug: bool = True):
    """
    执行 Claude Code 任务并自动确认
    
    Args:
        workdir: 工作目录
        task: 任务描述
        timeout: 总超时（秒）
        debug: 是否打印调试信息
        
    Returns:
        dict: 执行结果
    """
    workdir = os.path.abspath(workdir)
    
    if not os.path.isdir(workdir):
        return {'success': False, 'error': f'Directory not found: {workdir}'}
    
    if debug:
        print(f"🚀 Coding Agent Wrapper")
        print("=" * 60)
        print(f"📁 Workdir: {workdir}")
        print(f"📝 Task: {task}")
        print("=" * 60)
    
    try:
        # 启动 Claude Code
        child = pexpect.spawn(
            'claude',
            args=[task],
            cwd=workdir,
            timeout=10,  # 每次 expect 的超时
            encoding='utf-8',
        )
        
        # 可选：打印输出到终端
        if debug:
            child.logfile_read = sys.stdout
        
        auto_confirm_count = 0
        idle_checks = 0
        start_time = time.time()
        
        while True:
            # 检查总超时
            if time.time() - start_time > timeout:
                if debug:
                    print(f"\n⏰ Total timeout ({timeout}s)")
                break
            
            try:
                # 等待多种模式
                index = child.expect([
                    r'Is this a project',  # 安全提示
                    r'1\..*Yes',  # 选项 1
                    r'Do.*you.*want',  # 确认提示
                    r'Created.*\.(py|js|ts|java)',  # 文件创建完成
                    pexpect.TIMEOUT,
                    pexpect.EOF,
                ], timeout=10)
                
                if index <= 2:  # 需要确认
                    if auto_confirm_count < 50:
                        if debug:
                            print(f"\n✅ Auto-confirm #{auto_confirm_count} (pattern {index})")
                        child.send('1')  # 不带换行
                        auto_confirm_count += 1
                        idle_checks = 0
                    else:
                        if debug:
                            print("\n⚠️ Too many confirmations")
                        break
                        
                elif index == 3:  # 文件创建完成
                    if debug:
                        print(f"\n✅ File created!")
                    # 继续等待几秒，确保真的完成
                    time.sleep(5)
                    break
                    
                elif index == 4:  # TIMEOUT
                    idle_checks += 1
                    if idle_checks > 20:  # 200秒无动作
                        if debug:
                            print(f"\n⏰ Idle timeout ({idle_checks * 10}s)")
                        break
                    # 继续循环
                    
                elif index == 5:  # EOF
                    if debug:
                        print("\n✅ Process ended")
                    break
                    
            except pexpect.TIMEOUT:
                idle_checks += 1
                if idle_checks > 20:
                    if debug:
                        print(f"\n⏰ Idle timeout ({idle_checks * 10}s)")
                    break
            
            except pexpect.EOF:
                if debug:
                    print("\n✅ Process ended (EOF)")
                break
        
        # 关闭
        if child.isalive():
            child.close()
        
        if debug:
            print("=" * 60)
            print("📊 Statistics:")
            print(f"  - Auto-confirmations: {auto_confirm_count}")
            print(f"  - Duration: {time.time() - start_time:.1f}s")
            print("=" * 60)
        
        return {
            'success': True,
            'auto_confirms': auto_confirm_count,
            'duration': time.time() - start_time,
        }
        
    except Exception as e:
        if debug:
            print(f"\n❌ Error: {e}")
        return {
            'success': False,
            'error': str(e),
        }


def main():
    """命令行使用"""
    if len(sys.argv) < 3:
        print("Usage: python3 coding_agent_simple.py <workdir> <task>")
        print("")
        print("Example:")
        print('  python3 coding_agent_simple.py /tmp/project "Create hello.py"')
        sys.exit(1)
    
    workdir = sys.argv[1]
    task = sys.argv[2]
    
    result = execute_claude_task(workdir, task, timeout=300, debug=True)
    
    if result['success']:
        print(f"\n✅ Success!")
        sys.exit(0)
    else:
        print(f"\n❌ Failed: {result.get('error', 'Unknown')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
