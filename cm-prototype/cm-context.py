#!/usr/bin/env python3
"""
CM Context Manager - 管理本地和远程工作上下文
"""

import json
import os
from typing import Dict, Optional, List
from datetime import datetime
import sys

# 添加 transport 模块路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from cm_transport import TransportFactory, RemoteTransport
except ImportError:
    print("Warning: cm_transport not available, using mock")
    TransportFactory = None
    RemoteTransport = None


class Context:
    """代表一个工作上下文（本地或远程）"""
    
    def __init__(self, config: dict):
        self.id = config.get('id')
        self.name = config['name']
        self.path = config['path']
        self.machine = config.get('machine', 'local')
        self.tags = config.get('tags', [])
        self.created = config.get('created')
        self.last_used = config.get('lastUsed')
        
        # 创建 transport
        if TransportFactory:
            self.transport = TransportFactory.create_from_config(self.machine)
        else:
            self.transport = None
    
    def is_remote(self) -> bool:
        """是否是远程 context"""
        return self.machine != 'local'
    
    def is_agent(self) -> bool:
        """是否使用 Agent Server"""
        if isinstance(self.machine, dict):
            return self.machine.get('type') == 'agent'
        return False
    
    def get_display_name(self) -> str:
        """获取显示名称"""
        if self.is_remote():
            if isinstance(self.machine, dict):
                host = self.machine.get('host', 'unknown')
                user = self.machine.get('user', '')
                return f"{self.name} ({user}@{host})" if user else f"{self.name} ({host})"
            else:
                return f"{self.name} ({self.machine})"
        return self.name
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'id': self.id,
            'name': self.name,
            'path': self.path,
            'machine': self.machine,
            'tags': self.tags,
            'created': self.created,
            'lastUsed': self.last_used
        }
    
    def test_connection(self) -> tuple[bool, str]:
        """测试连接"""
        if not self.transport:
            return False, "Transport not available"
        
        try:
            result = self.transport.test_connection()
            if result:
                return True, "Connection OK"
            else:
                return False, "Connection failed"
        except Exception as e:
            return False, f"Error: {e}"


class ContextManager:
    """Context 管理器"""
    
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or os.path.expanduser('~/.cm')
        self.contexts_file = os.path.join(self.data_dir, 'contexts.json')
        self.contexts: Dict[str, Context] = {}
        
        # 确保目录存在
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 加载 contexts
        self.load()
    
    def load(self):
        """加载 contexts"""
        if os.path.exists(self.contexts_file):
            try:
                with open(self.contexts_file, 'r') as f:
                    data = json.load(f)
                    
                for ctx_data in data.get('contexts', {}).values():
                    ctx = Context(ctx_data)
                    self.contexts[ctx.id] = ctx
            except Exception as e:
                print(f"Warning: Failed to load contexts: {e}")
    
    def save(self):
        """保存 contexts"""
        data = {
            'version': 1,
            'contexts': {
                ctx_id: ctx.to_dict()
                for ctx_id, ctx in self.contexts.items()
            }
        }
        
        with open(self.contexts_file, 'w') as f:
            json.dump(data, f, indent=2)
    
    def add(self, name: str, path: str, machine: str or dict = 'local',
            tags: List[str] = None) -> Context:
        """添加 context"""
        # 生成 ID
        ctx_id = f"ctx-{len(self.contexts) + 1:03d}"
        
        # 创建 context
        ctx_data = {
            'id': ctx_id,
            'name': name,
            'path': path,
            'machine': machine,
            'tags': tags or [],
            'created': datetime.now().isoformat(),
            'lastUsed': None
        }
        
        ctx = Context(ctx_data)
        self.contexts[ctx_id] = ctx
        
        # 保存
        self.save()
        
        return ctx
    
    def get(self, name_or_id: str) -> Optional[Context]:
        """获取 context（通过名称或 ID）"""
        # 先尝试 ID
        if name_or_id in self.contexts:
            return self.contexts[name_or_id]
        
        # 再尝试名称
        for ctx in self.contexts.values():
            if ctx.name == name_or_id:
                return ctx
        
        return None
    
    def list(self, tags: List[str] = None) -> List[Context]:
        """列出 contexts"""
        contexts = list(self.contexts.values())
        
        # 过滤 tags
        if tags:
            contexts = [
                ctx for ctx in contexts
                if any(tag in ctx.tags for tag in tags)
            ]
        
        return contexts
    
    def remove(self, name_or_id: str) -> bool:
        """删除 context"""
        ctx = self.get(name_or_id)
        if not ctx:
            return False
        
        del self.contexts[ctx.id]
        self.save()
        return True
    
    def update_last_used(self, name_or_id: str):
        """更新最后使用时间"""
        ctx = self.get(name_or_id)
        if ctx:
            ctx.last_used = datetime.now().isoformat()
            self.save()


def main():
    """测试和演示"""
    print("=" * 60)
    print("CM Context Manager - Demo")
    print("=" * 60)
    print()
    
    mgr = ContextManager()
    
    # 添加本地 context
    print("1. Adding local context...")
    ctx1 = mgr.add(
        name='local-project',
        path='/home/hren/.openclaw/workspace',
        machine='local',
        tags=['local', 'test']
    )
    print(f"   ✅ Added: {ctx1.get_display_name()}")
    print()
    
    # 添加远程 SSH context
    print("2. Adding remote SSH context...")
    ctx2 = mgr.add(
        name='remote-server',
        path='/var/www/app',
        machine={
            'type': 'ssh',
            'host': 'example.com',
            'user': 'deploy',
            'port': 22
        },
        tags=['remote', 'production']
    )
    print(f"   ✅ Added: {ctx2.get_display_name()}")
    print()
    
    # 添加 Agent Server context
    print("3. Adding Agent Server context...")
    ctx3 = mgr.add(
        name='agent-server',
        path='/home/user/project',
        machine={
            'type': 'agent',
            'host': 'agent.example.com',
            'user': 'deploy',
            'agentPort': 9876,
            'authToken': 'secret-token'
        },
        tags=['remote', 'agent']
    )
    print(f"   ✅ Added: {ctx3.get_display_name()}")
    print()
    
    # 列出所有 contexts
    print("4. Listing all contexts...")
    print(f"{'ID':<12} {'Name':<20} {'Machine':<20} {'Path':<30}")
    print("-" * 82)
    for ctx in mgr.list():
        machine_str = 'local' if not ctx.is_remote() else \
                     ('agent' if ctx.is_agent() else 'ssh')
        print(f"{ctx.id:<12} {ctx.name:<20} {machine_str:<20} {ctx.path:<30}")
    print()
    
    # 获取特定 context
    print("5. Getting context by name...")
    ctx = mgr.get('local-project')
    if ctx:
        print(f"   ✅ Found: {ctx.get_display_name()}")
        print(f"      Path: {ctx.path}")
        print(f"      Type: {'Local' if not ctx.is_remote() else 'Remote'}")
        print(f"      Tags: {', '.join(ctx.tags)}")
    print()
    
    # 测试连接
    print("6. Testing connection...")
    success, msg = ctx.test_connection()
    print(f"   {'✅' if success else '❌'} {msg}")
    print()
    
    print("=" * 60)
    print("Demo complete!")
    print(f"Contexts file: {mgr.contexts_file}")
    print("=" * 60)


if __name__ == '__main__':
    main()
