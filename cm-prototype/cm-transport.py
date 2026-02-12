#!/usr/bin/env python3
"""
CM Remote Transport Layer
支持 SSH 和 OpenClaw Node 两种远程连接方式
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
import subprocess
import json
import os

class RemoteTransport(ABC):
    """远程传输抽象基类"""
    
    @abstractmethod
    def execute(self, command: str, timeout: int = 30) -> str:
        """执行远程命令并返回输出"""
        pass
    
    @abstractmethod
    def send_keys(self, session: str, keys: str) -> bool:
        """向远程 TMUX session 发送按键"""
        pass
    
    @abstractmethod
    def capture_pane(self, session: str, lines: int = 50) -> str:
        """捕获远程 TMUX pane 的输出"""
        pass
    
    @abstractmethod
    def test_connection(self) -> bool:
        """测试连接是否正常"""
        pass
    
    @abstractmethod
    def get_latency(self) -> int:
        """获取网络延迟（毫秒）"""
        pass


class SSHTransport(RemoteTransport):
    """SSH 传输实现"""
    
    def __init__(self, host: str, user: str, port: int = 22, 
                 key_file: Optional[str] = None):
        self.host = host
        self.user = user
        self.port = port
        self.key_file = key_file or os.path.expanduser("~/.ssh/id_rsa")
        self._base_cmd = self._build_base_cmd()
    
    def _build_base_cmd(self) -> list:
        """构建 SSH 基础命令"""
        cmd = ["ssh"]
        
        # ControlMaster 配置（复用连接，减少延迟）
        control_path = f"/tmp/cm-ssh-{self.user}@{self.host}:{self.port}"
        cmd.extend([
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=" + control_path,
            "-o", "ControlPersist=10m",
        ])
        
        # 其他配置
        cmd.extend([
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.port),
        ])
        
        if self.key_file and os.path.exists(self.key_file):
            cmd.extend(["-i", self.key_file])
        
        cmd.append(f"{self.user}@{self.host}")
        return cmd
    
    def execute(self, command: str, timeout: int = 30) -> str:
        """执行远程命令"""
        cmd = self._base_cmd + [command]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"SSH command failed: {e.stderr}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH command timeout after {timeout}s")
    
    def send_keys(self, session: str, keys: str) -> bool:
        """向远程 TMUX session 发送按键"""
        socket = f"/tmp/cm-tmux-sockets/{session}.sock"
        # 使用 -l 选项字面发送，避免特殊字符问题
        cmd = f"tmux -S {socket} send-keys -t {session}:0.0 -l -- '{keys}'"
        try:
            self.execute(cmd)
            # 发送 Enter（如果需要）
            if not keys.endswith('\n'):
                enter_cmd = f"tmux -S {socket} send-keys -t {session}:0.0 Enter"
                self.execute(enter_cmd)
            return True
        except RuntimeError:
            return False
    
    def capture_pane(self, session: str, lines: int = 50) -> str:
        """捕获远程 TMUX pane 输出"""
        socket = f"/tmp/cm-tmux-sockets/{session}.sock"
        cmd = f"tmux -S {socket} capture-pane -p -J -t {session}:0.0 -S -{lines}"
        try:
            return self.execute(cmd)
        except RuntimeError:
            return ""
    
    def test_connection(self) -> bool:
        """测试 SSH 连接"""
        try:
            result = self.execute("echo 'OK'", timeout=5)
            return result.strip() == "OK"
        except:
            return False
    
    def get_latency(self) -> int:
        """测量 SSH 延迟（毫秒）"""
        import time
        try:
            start = time.time()
            self.execute("echo test", timeout=5)
            end = time.time()
            return int((end - start) * 1000)
        except:
            return -1


class NodeTransport(RemoteTransport):
    """OpenClaw Node 传输实现"""
    
    def __init__(self, node_id: str, gateway_url: Optional[str] = None,
                 gateway_token: Optional[str] = None):
        self.node_id = node_id
        self.gateway_url = gateway_url
        self.gateway_token = gateway_token
    
    def execute(self, command: str, timeout: int = 30) -> str:
        """通过 OpenClaw Node 执行命令"""
        cmd = [
            "openclaw", "nodes", "run",
            "--node", self.node_id,
            "--timeout", str(timeout * 1000),  # ms
        ]
        
        if self.gateway_url:
            cmd.extend(["--gateway-url", self.gateway_url])
        if self.gateway_token:
            cmd.extend(["--gateway-token", self.gateway_token])
        
        cmd.append(command)
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Node command failed: {e.stderr}")
    
    def send_keys(self, session: str, keys: str) -> bool:
        """向远程 TMUX session 发送按键"""
        socket = f"/tmp/cm-tmux-sockets/{session}.sock"
        cmd = f"tmux -S {socket} send-keys -t {session}:0.0 -l -- '{keys}' && " \
              f"sleep 0.1 && tmux -S {socket} send-keys -t {session}:0.0 Enter"
        try:
            self.execute(cmd)
            return True
        except RuntimeError:
            return False
    
    def capture_pane(self, session: str, lines: int = 50) -> str:
        """捕获远程 TMUX pane 输出"""
        socket = f"/tmp/cm-tmux-sockets/{session}.sock"
        cmd = f"tmux -S {socket} capture-pane -p -J -t {session}:0.0 -S -{lines}"
        try:
            return self.execute(cmd)
        except RuntimeError:
            return ""
    
    def test_connection(self) -> bool:
        """测试 Node 连接"""
        try:
            result = self.execute("echo 'OK'", timeout=5)
            return result.strip() == "OK"
        except:
            return False
    
    def get_latency(self) -> int:
        """测量 Node 延迟（毫秒）"""
        import time
        try:
            start = time.time()
            self.execute("echo test", timeout=5)
            end = time.time()
            return int((end - start) * 1000)
        except:
            return -1


class LocalTransport(RemoteTransport):
    """本地传输实现（用于统一接口）"""
    
    def execute(self, command: str, timeout: int = 30) -> str:
        """执行本地命令"""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Local command failed: {e.stderr}")
    
    def send_keys(self, session: str, keys: str) -> bool:
        """向本地 TMUX session 发送按键"""
        socket = f"/tmp/cm-tmux-sockets/{session}.sock"
        cmd = f"tmux -S {socket} send-keys -t {session}:0.0 -l -- '{keys}'"
        try:
            subprocess.run(cmd, shell=True, check=True, capture_output=True)
            # Send Enter
            enter_cmd = f"tmux -S {socket} send-keys -t {session}:0.0 Enter"
            subprocess.run(enter_cmd, shell=True, check=True, capture_output=True)
            return True
        except:
            return False
    
    def capture_pane(self, session: str, lines: int = 50) -> str:
        """捕获本地 TMUX pane 输出"""
        socket = f"/tmp/cm-tmux-sockets/{session}.sock"
        cmd = f"tmux -S {socket} capture-pane -p -J -t {session}:0.0 -S -{lines}"
        try:
            result = subprocess.run(
                cmd, shell=True, 
                capture_output=True, 
                text=True, 
                check=True
            )
            return result.stdout
        except:
            return ""
    
    def test_connection(self) -> bool:
        """本地连接始终可用"""
        return True
    
    def get_latency(self) -> int:
        """本地延迟为 0"""
        return 0


class TransportFactory:
    """传输层工厂"""
    
    @staticmethod
    def create_from_config(machine_config: Dict[str, Any]) -> RemoteTransport:
        """根据配置创建相应的 Transport"""
        if not isinstance(machine_config, dict):
            # 简单字符串，判断是 local 还是 SSH 简写
            if machine_config == "local":
                return LocalTransport()
            else:
                # 假设是 user@host 格式
                if "@" in machine_config:
                    user, host = machine_config.split("@", 1)
                    return SSHTransport(host=host, user=user)
                else:
                    raise ValueError(f"Invalid machine config: {machine_config}")
        
        # 字典配置
        machine_type = machine_config.get("type", "local")
        
        if machine_type == "local":
            return LocalTransport()
        
        elif machine_type == "ssh":
            return SSHTransport(
                host=machine_config["host"],
                user=machine_config["user"],
                port=machine_config.get("port", 22),
                key_file=machine_config.get("keyFile")
            )
        
        elif machine_type == "openclaw-node":
            return NodeTransport(
                node_id=machine_config["nodeId"],
                gateway_url=machine_config.get("gatewayUrl"),
                gateway_token=machine_config.get("token")
            )
        
        else:
            raise ValueError(f"Unknown machine type: {machine_type}")


# 测试代码
if __name__ == "__main__":
    print("🧪 Testing Transport Layer\n")
    
    # 测试本地
    print("1️⃣ Testing LocalTransport...")
    local = LocalTransport()
    assert local.test_connection()
    result = local.execute("echo 'Hello Local'")
    print(f"   Result: {result.strip()}")
    print(f"   Latency: {local.get_latency()}ms")
    print("   ✅ LocalTransport OK\n")
    
    # 测试工厂
    print("2️⃣ Testing TransportFactory...")
    t1 = TransportFactory.create_from_config("local")
    assert isinstance(t1, LocalTransport)
    print("   ✅ Create from 'local' string")
    
    t2 = TransportFactory.create_from_config({
        "type": "ssh",
        "host": "example.com",
        "user": "deploy"
    })
    assert isinstance(t2, SSHTransport)
    print("   ✅ Create from SSH config")
    
    t3 = TransportFactory.create_from_config({
        "type": "openclaw-node",
        "nodeId": "my-node"
    })
    assert isinstance(t3, NodeTransport)
    print("   ✅ Create from Node config")
    
    print("\n✅ All tests passed!")
