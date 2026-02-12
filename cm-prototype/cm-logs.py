#!/usr/bin/env python3
"""
CM Logs - Session 日志查看工具
"""

import os
import sys
import time
import argparse

def tail_file(filepath, lines=50, follow=False):
    """查看文件尾部"""
    if not os.path.exists(filepath):
        print(f"❌ Log file not found: {filepath}")
        return
    
    # 读取最后 N 行
    with open(filepath, 'r') as f:
        content = f.readlines()
        if lines > 0:
            content = content[-lines:]
        print(''.join(content), end='')
    
    if follow:
        # Follow 模式
        print("\n--- Following log (Ctrl+C to stop) ---\n")
        with open(filepath, 'r') as f:
            # 移到文件末尾
            f.seek(0, 2)
            
            try:
                while True:
                    line = f.readline()
                    if line:
                        print(line, end='')
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                print("\n\n--- Stopped ---")


def main():
    parser = argparse.ArgumentParser(description='View CM session logs')
    parser.add_argument('session_id', help='Session ID')
    parser.add_argument('-f', '--follow', action='store_true', help='Follow log output')
    parser.add_argument('-n', '--lines', type=int, default=50, help='Number of lines to show')
    
    args = parser.parse_args()
    
    # 查找日志文件
    log_file = os.path.expanduser(f'~/.cm/sessions/active/{args.session_id}.log')
    
    if not os.path.exists(log_file):
        print(f"❌ No log file for session: {args.session_id}")
        print(f"   Expected: {log_file}")
        return
    
    tail_file(log_file, args.lines, args.follow)


if __name__ == '__main__':
    main()
