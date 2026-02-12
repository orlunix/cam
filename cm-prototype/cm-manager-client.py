#!/usr/bin/env python3
"""
CM Manager Client - Connects to remote Agent Server
"""

import asyncio
import websockets
import json
import subprocess
import time
import signal
import sys
from typing import Optional, Callable

class SSHTunnel:
    """Manages SSH tunnel to remote Agent"""
    
    def __init__(self, host: str, user: str, remote_port: int = 9876, local_port: int = 9876):
        self.host = host
        self.user = user
        self.remote_port = remote_port
        self.local_port = local_port
        self.process = None
        self.control_path = f"/tmp/cm-ssh-{user}@{host}"
    
    def start(self) -> bool:
        """Start SSH tunnel"""
        print(f"🔌 Establishing SSH tunnel to {self.user}@{self.host}...")
        
        cmd = [
            'ssh', '-N',
            '-L', f'{self.local_port}:localhost:{self.remote_port}',
            f'{self.user}@{self.host}',
            '-o', 'ControlMaster=auto',
            '-o', f'ControlPath={self.control_path}',
            '-o', 'ControlPersist=24h',
            '-o', 'ServerAliveInterval=60',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ConnectTimeout=10'
        ]
        
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )
            
            # Wait for tunnel to establish
            time.sleep(2)
            
            if self.is_alive():
                print(f"✅ SSH tunnel established")
                return True
            else:
                print(f"❌ SSH tunnel failed")
                return False
        
        except Exception as e:
            print(f"❌ Failed to start SSH tunnel: {e}")
            return False
    
    def is_alive(self) -> bool:
        """Check if tunnel is alive"""
        if not self.process or self.process.poll() is not None:
            return False
        
        # Try to connect to local port
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(('localhost', self.local_port))
            s.close()
            return True
        except:
            return False
    
    def stop(self):
        """Stop SSH tunnel"""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except:
                self.process.kill()
            print(f"🔌 SSH tunnel closed")


class CMManagerClient:
    """Manager client - connects to Agent Server"""
    
    def __init__(self, host: str, user: str, auth_token: str,
                 agent_port: int = 9876, use_tunnel: bool = True):
        self.host = host
        self.user = user
        self.auth_token = auth_token
        self.agent_port = agent_port
        self.use_tunnel = use_tunnel
        
        self.tunnel: Optional[SSHTunnel] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.message_handlers = {}
        self.receive_task = None
    
    async def connect(self) -> bool:
        """Connect to Agent Server"""
        try:
            # Establish SSH tunnel if needed
            if self.use_tunnel:
                self.tunnel = SSHTunnel(self.host, self.user, self.agent_port)
                if not self.tunnel.start():
                    return False
                ws_url = f"ws://localhost:{self.agent_port}"
            else:
                ws_url = f"ws://{self.host}:{self.agent_port}"
            
            # Connect WebSocket
            print(f"🔗 Connecting to Agent Server...")
            self.ws = await websockets.connect(ws_url)
            
            # Authenticate
            await self.ws.send(json.dumps({
                'auth_token': self.auth_token
            }))
            
            # Wait for auth response
            response = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            auth_result = json.loads(response)
            
            if auth_result.get('status') == 'authenticated':
                print(f"✅ Connected and authenticated")
                self.connected = True
                
                # Start receiving messages
                self.receive_task = asyncio.create_task(self._receive_messages())
                
                return True
            else:
                print(f"❌ Authentication failed: {auth_result.get('error')}")
                return False
        
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            return False
    
    async def _receive_messages(self):
        """Receive messages from Agent (background task)"""
        try:
            async for message in self.ws:
                try:
                    msg = json.loads(message)
                    await self._handle_message(msg)
                except Exception as e:
                    print(f"❌ Message handling error: {e}")
        except websockets.exceptions.ConnectionClosed:
            print(f"📱 Connection closed")
            self.connected = False
        except Exception as e:
            print(f"❌ Receive error: {e}")
            self.connected = False
    
    async def _handle_message(self, msg: dict):
        """Handle message from Agent"""
        msg_type = msg.get('type')
        
        # Call registered handlers
        handler = self.message_handlers.get(msg_type)
        if handler:
            await handler(msg)
        else:
            # Default handling
            if msg_type == 'state_change':
                session_id = msg.get('sessionId')
                state = msg.get('state')
                print(f"   [{session_id}] State: {state}")
            
            elif msg_type == 'auto_confirmed':
                session_id = msg.get('sessionId')
                print(f"   [{session_id}] Auto-confirmed")
            
            elif msg_type == 'session_completed':
                session_id = msg.get('sessionId')
                state = msg.get('state')
                print(f"   [{session_id}] Completed: {state}")
    
    def on(self, msg_type: str, handler: Callable):
        """Register message handler"""
        self.message_handlers[msg_type] = handler
    
    async def send_command(self, action: str, **kwargs) -> dict:
        """Send command to Agent and wait for response"""
        if not self.connected:
            raise ConnectionError("Not connected to Agent")
        
        cmd = {'action': action, **kwargs}
        await self.ws.send(json.dumps(cmd))
        
        # Wait for response
        response = await self.ws.recv()
        return json.loads(response)
    
    async def create_session(self, tool: str, task: str, context: dict) -> str:
        """Create remote session"""
        print(f"🚀 Creating remote session...")
        print(f"   Tool: {tool}")
        print(f"   Task: {task[:50]}...")
        
        response = await self.send_command(
            'create_session',
            tool=tool,
            task=task,
            context=context
        )
        
        if 'error' in response:
            raise RuntimeError(f"Failed to create session: {response['error']}")
        
        session_id = response.get('sessionId')
        print(f"✅ Session created: {session_id}")
        return session_id
    
    async def send_keys(self, session_id: str, keys: str):
        """Send keys to remote session"""
        await self.send_command('send_keys', sessionId=session_id, keys=keys)
    
    async def capture_pane(self, session_id: str, lines: int = 50) -> str:
        """Capture pane output"""
        response = await self.send_command(
            'capture_pane',
            sessionId=session_id,
            lines=lines
        )
        return response.get('output', '')
    
    async def list_sessions(self) -> list:
        """List all sessions"""
        response = await self.send_command('list_sessions')
        return response.get('sessions', [])
    
    async def kill_session(self, session_id: str):
        """Kill remote session"""
        await self.send_command('kill_session', sessionId=session_id)
    
    async def disconnect(self):
        """Disconnect from Agent"""
        if self.receive_task:
            self.receive_task.cancel()
        
        if self.ws:
            await self.ws.close()
        
        if self.tunnel:
            self.tunnel.stop()
        
        self.connected = False
        print(f"👋 Disconnected")


async def demo():
    """Demo usage"""
    print("=" * 60)
    print("CM Manager Client Demo")
    print("=" * 60)
    print()
    
    # Configuration
    host = input("Remote host (e.g., localhost): ").strip() or "localhost"
    user = input("Remote user (default: current user): ").strip() or subprocess.check_output(['whoami']).decode().strip()
    token = input("Auth token (default: default-token): ").strip() or "default-token"
    
    # Create client
    client = CMManagerClient(
        host=host,
        user=user,
        auth_token=token,
        use_tunnel=(host != 'localhost')
    )
    
    # Connect
    if not await client.connect():
        print("❌ Failed to connect")
        return
    
    try:
        # Create session
        session_id = await client.create_session(
            tool='claude',
            task='创建一个测试文件 agent-test.txt，内容是 "Agent Server Works!"',
            context={'path': '/home/hren/.openclaw/workspace'}
        )
        
        # Wait for completion
        print(f"\n⏳ Waiting for session to complete...")
        print(f"   (State changes will be pushed automatically)\n")
        
        # Keep alive for 60 seconds
        await asyncio.sleep(60)
        
        # List sessions
        sessions = await client.list_sessions()
        print(f"\n📋 Active sessions: {len(sessions)}")
        for s in sessions:
            print(f"   - {s['sessionId']}: {s['state']} ({s['tool']})")
    
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted")
    
    finally:
        # Disconnect
        await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(demo())
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
        sys.exit(0)
