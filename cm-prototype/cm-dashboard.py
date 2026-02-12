#!/usr/bin/env python3
"""
CM Dashboard - Real-time Status Dashboard
自动更新 Code Manager 状态到 Discord 消息
"""

import subprocess
import datetime
import json
import time
import sys

class CMDashboard:
    """Code Manager Dashboard"""
    
    def __init__(self, message_id: str = None):
        self.message_id = message_id
        self.cm_path = "/home/hren/.openclaw/workspace/cm-prototype"
    
    def get_sessions(self):
        """获取所有 sessions"""
        result = subprocess.run(
            ['python3', 'cm-cli.py', 'status'],
            capture_output=True,
            text=True,
            cwd=self.cm_path
        )
        
        # 解析输出
        lines = result.stdout.strip().split('\n')
        sessions = {'running': [], 'pending': []}
        
        for line in lines:
            if 'running' in line.lower():
                parts = line.split()
                if len(parts) >= 5:
                    sessions['running'].append({
                        'id': parts[0],
                        'tool': parts[1],
                        'mode': parts[2]
                    })
            elif 'pending' in line.lower():
                parts = line.split()
                if len(parts) >= 5:
                    sessions['pending'].append({
                        'id': parts[0],
                        'tool': parts[1],
                        'mode': parts[2]
                    })
        
        return sessions
    
    def get_contexts(self):
        """获取所有 contexts"""
        result = subprocess.run(
            ['python3', 'cm-cli.py', 'ctx', 'list'],
            capture_output=True,
            text=True,
            cwd=self.cm_path
        )
        
        contexts = []
        lines = result.stdout.strip().split('\n')
        
        for line in lines[3:]:  # Skip header
            if line.strip() and not line.startswith('-'):
                parts = line.split()
                if len(parts) >= 4:
                    contexts.append({
                        'id': parts[0],
                        'name': parts[1],
                        'type': parts[2]
                    })
        
        return contexts
    
    def get_ssh_masters(self):
        """获取活跃的 SSH master 连接数"""
        result = subprocess.run(
            ['bash', '-c', 'ps aux | grep "ssh.*ControlMaster" | grep -v grep | wc -l'],
            capture_output=True,
            text=True
        )
        return int(result.stdout.strip())
    
    def format_dashboard(self):
        """格式化仪表板内容"""
        sessions = self.get_sessions()
        contexts = self.get_contexts()
        ssh_count = self.get_ssh_masters()
        
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S PST')
        
        # 构建消息
        lines = [
            "📊 **Code Manager - Real-time Dashboard**",
            "",
            f"🕐 **Last Updated**: {timestamp}",
            "",
            "---",
            "",
            f"## 📋 Active Sessions ({len(sessions['running']) + len(sessions['pending'])} total)",
            ""
        ]
        
        # Running sessions
        if sessions['running']:
            lines.append(f"**✅ Running** ({len(sessions['running'])}):")
            for s in sessions['running'][:10]:  # Limit to 10
                lines.append(f"• `{s['id']}` - {s['mode']} - {s['tool']}")
            lines.append("")
        
        # Pending sessions
        if sessions['pending']:
            lines.append(f"**⏳ Pending** ({len(sessions['pending'])}):")
            for s in sessions['pending'][:10]:  # Limit to 10
                lines.append(f"• `{s['id']}` - {s['mode']}")
            lines.append("")
        
        # SSH connections
        lines.extend([
            "---",
            "",
            "## 🌐 SSH Connections",
            "",
            f"ControlMaster processes: **{ssh_count}**",
            "",
            "---",
            "",
            f"## 📍 Contexts ({len(contexts)} total)",
            ""
        ])
        
        # Contexts (top 5)
        for ctx in contexts[:5]:
            lines.append(f"• `{ctx['name']}` → {ctx['type']}")
        
        if len(contexts) > 5:
            lines.append(f"• ... and {len(contexts) - 5} more")
        
        lines.extend([
            "",
            "---",
            "",
            "💡 **Quick Commands:**",
            "```",
            "cm-cli.py status         # View all",
            "cm-cli.py logs <id>      # View logs",
            "cm-cli.py kill <id>      # Kill session",
            "```",
            "",
            "📌 **Pin this message!** Scroll up to check status anytime.",
            "🔄 **Say \"refresh dashboard\"** to update this message."
        ])
        
        return '\n'.join(lines)
    
    def print_dashboard(self):
        """打印仪表板（用于测试）"""
        print(self.format_dashboard())
    
    def update_discord_message(self):
        """更新 Discord 消息（需要 OpenClaw message tool）"""
        # 这里需要 OpenClaw 的 message.edit 功能
        # 返回格式化的内容供 OpenClaw 使用
        return self.format_dashboard()


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='CM Dashboard')
    parser.add_argument('--message-id', help='Discord message ID to update')
    parser.add_argument('--watch', action='store_true', help='Watch mode (auto-refresh)')
    parser.add_argument('--interval', type=int, default=60, help='Refresh interval (seconds)')
    
    args = parser.parse_args()
    
    dashboard = CMDashboard(message_id=args.message_id)
    
    if args.watch:
        print(f"📊 CM Dashboard - Watch Mode (refresh every {args.interval}s)")
        print("Press Ctrl+C to stop")
        print()
        
        try:
            while True:
                dashboard.print_dashboard()
                print("\n" + "="*60 + "\n")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n\n👋 Dashboard stopped")
    else:
        dashboard.print_dashboard()


if __name__ == '__main__':
    main()
