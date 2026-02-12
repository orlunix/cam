#!/usr/bin/env python3
"""
CM Agent Server - Remote execution agent with WebSocket communication
Runs on remote machine, manages TMUX sessions, and pushes state changes
"""

import asyncio
import websockets
import json
import subprocess
import time
import re
import os
import sys
from typing import Dict, Set, Optional
from datetime import datetime

class TmuxSession:
    """Represents a TMUX session"""
    
    def __init__(self, session_id: str, socket: str, tool: str, context_path: str):
        self.session_id = session_id
        self.socket = socket
        self.tool = tool
        self.context_path = context_path
        self.state = "starting"
        self.created_at = time.time()
        self.last_output = ""
        self.monitor_task = None
    
    def execute(self, cmd: str) -> str:
        """Execute tmux command"""
        try:
            result = subprocess.run(
                cmd, shell=True,
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )
            return result.stdout
        except Exception as e:
            return f"Error: {e}"
    
    def send_keys(self, keys: str):
        """Send keys to session"""
        # Use -l for literal text
        cmd = f"tmux -S {self.socket} send-keys -t {self.session_id}:0.0 -l -- '{keys}'"
        self.execute(cmd)
        time.sleep(0.1)
        # Send Enter
        cmd = f"tmux -S {self.socket} send-keys -t {self.session_id}:0.0 Enter"
        self.execute(cmd)
    
    def capture_pane(self, lines: int = 50) -> str:
        """Capture pane output"""
        cmd = f"tmux -S {self.socket} capture-pane -p -J -t {self.session_id}:0.0 -S -{lines}"
        return self.execute(cmd)
    
    def exists(self) -> bool:
        """Check if session exists"""
        cmd = f"tmux -S {self.socket} has-session -t {self.session_id} 2>/dev/null"
        return subprocess.run(cmd, shell=True).returncode == 0
    
    def kill(self):
        """Kill session"""
        cmd = f"tmux -S {self.socket} kill-session -t {self.session_id} 2>/dev/null"
        subprocess.run(cmd, shell=True)


class CMAgentServer:
    """CM Agent Server - manages TMUX sessions and communicates with Manager"""
    
    def __init__(self, port: int = 9876, auth_token: Optional[str] = None):
        self.port = port
        self.auth_token = auth_token or os.environ.get('CM_AGENT_TOKEN', 'default-token')
        self.sessions: Dict[str, TmuxSession] = {}
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.socket_dir = "/tmp/cm-tmux-sockets"
        os.makedirs(self.socket_dir, exist_ok=True)
        
        print(f"🚀 CM Agent Server v1.0")
        print(f"   Port: {self.port}")
        print(f"   Auth: {'enabled' if self.auth_token else 'disabled'}")
        print(f"   Socket dir: {self.socket_dir}")
    
    async def handle_client(self, websocket, path):
        """Handle Manager connection"""
        client_addr = websocket.remote_address
        print(f"📱 Client connected: {client_addr}")
        
        # Authentication
        try:
            auth_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            auth_data = json.loads(auth_msg)
            
            # Debug logging
            print(f"🔍 Auth received: {auth_data}")
            print(f"🔍 Client token: {auth_data.get('auth_token')}")
            print(f"🔍 Server token: {self.auth_token}")
            print(f"🔍 Match: {auth_data.get('auth_token') == self.auth_token}")
            
            if auth_data.get('auth_token') != self.auth_token:
                await websocket.send(json.dumps({
                    'error': 'Unauthorized',
                    'message': 'Invalid authentication token'
                }))
                await websocket.close()
                print(f"❌ Auth failed: {client_addr}")
                return
            
            print(f"✅ Auth success: {client_addr}")
            await websocket.send(json.dumps({'status': 'authenticated'}))
            
        except Exception as e:
            print(f"❌ Auth error: {e}")
            return
        
        # Add to clients
        self.clients.add(websocket)
        
        try:
            # Handle messages
            async for message in websocket:
                await self.handle_message(websocket, message)
        except websockets.exceptions.ConnectionClosed:
            print(f"📱 Client disconnected: {client_addr}")
        except Exception as e:
            print(f"❌ Client error: {e}")
        finally:
            self.clients.discard(websocket)
    
    async def handle_message(self, websocket, message: str):
        """Handle command from Manager"""
        try:
            cmd = json.loads(message)
            action = cmd.get('action')
            
            print(f"📨 Command: {action}")
            
            if action == 'create_session':
                await self.create_session(websocket, cmd)
            
            elif action == 'send_keys':
                await self.send_keys(websocket, cmd)
            
            elif action == 'capture_pane':
                await self.capture_pane(websocket, cmd)
            
            elif action == 'list_sessions':
                await self.list_sessions(websocket)
            
            elif action == 'kill_session':
                await self.kill_session(websocket, cmd)
            
            else:
                await websocket.send(json.dumps({
                    'error': f'Unknown action: {action}'
                }))
        
        except Exception as e:
            print(f"❌ Message handling error: {e}")
            await websocket.send(json.dumps({
                'error': str(e)
            }))
    
    async def create_session(self, websocket, cmd):
        """Create new TMUX session"""
        tool = cmd.get('tool', 'claude')
        task = cmd.get('task', '')
        context = cmd.get('context', {})
        context_path = context.get('path', os.getcwd())
        
        # Generate session ID
        session_id = f"cm-{int(time.time())}"
        socket = f"{self.socket_dir}/{session_id}.sock"
        
        print(f"🚀 Creating session: {session_id}")
        print(f"   Tool: {tool}")
        print(f"   Path: {context_path}")
        
        try:
            # Create TMUX session
            subprocess.run([
                'tmux', '-S', socket,
                'new-session', '-d', '-s', session_id
            ], check=True)
            
            # Create session object
            tmux_session = TmuxSession(session_id, socket, tool, context_path)
            self.sessions[session_id] = tmux_session
            
            # Start tool
            tool_cmd = f"cd '{context_path}' && {tool}"
            tmux_session.send_keys(tool_cmd)
            
            # Wait for tool to start
            await asyncio.sleep(2)
            
            # Send task (for Claude)
            if tool == 'claude' and task:
                await asyncio.sleep(1)
                
                # Check for trust prompt
                output = tmux_session.capture_pane(30)
                if 'trust this folder' in output.lower():
                    print(f"   [AUTO] Trust folder prompt detected")
                    tmux_session.send_keys('')  # Just Enter
                    await asyncio.sleep(1)
                
                # Send task
                print(f"   Sending task: {task[:50]}...")
                tmux_session.send_keys(task)
            
            # Start monitoring (Python 3.6 compatibility)
            loop = asyncio.get_event_loop()
            tmux_session.monitor_task = loop.create_task(
                self.monitor_session(session_id)
            )
            
            # Respond
            await websocket.send(json.dumps({
                'type': 'session_created',
                'sessionId': session_id,
                'socket': socket
            }))
            
            print(f"✅ Session created: {session_id}")
        
        except Exception as e:
            print(f"❌ Session creation failed: {e}")
            await websocket.send(json.dumps({
                'error': f'Failed to create session: {e}'
            }))
    
    async def monitor_session(self, session_id: str):
        """Monitor session and push state changes"""
        session = self.sessions.get(session_id)
        if not session:
            return
        
        print(f"👀 Monitoring: {session_id}")
        
        iteration = 0
        while session.exists():
            iteration += 1
            
            try:
                # Capture output
                output = session.capture_pane(50)
                
                # Check if changed
                if output == session.last_output:
                    await asyncio.sleep(2)
                    continue
                
                session.last_output = output
                
                # Detect state
                old_state = session.state
                session.state = self.detect_state(output)
                
                # State change?
                if session.state != old_state:
                    print(f"   [{session_id}] State: {old_state} → {session.state}")
                    await self.broadcast({
                        'type': 'state_change',
                        'sessionId': session_id,
                        'state': session.state,
                        'timestamp': time.time()
                    })
                
                # Auto-confirm check
                if self.should_auto_confirm(output):
                    print(f"   [{session_id}] Auto-confirming prompt")
                    
                    # Detect prompt type
                    if re.search(r'❯.*1\..*Yes|^\s*1\.', output[-500:], re.MULTILINE):
                        # Option selection - just Enter
                        cmd = f"tmux -S {session.socket} send-keys -t {session_id}:0.0 Enter"
                        session.execute(cmd)
                    else:
                        # Yes/no - send 'y'
                        session.send_keys('y')
                    
                    await self.broadcast({
                        'type': 'auto_confirmed',
                        'sessionId': session_id,
                        'timestamp': time.time()
                    })
                
                # Check completion
                if session.state in ['done', 'failed']:
                    print(f"   [{session_id}] Completed with state: {session.state}")
                    await self.broadcast({
                        'type': 'session_completed',
                        'sessionId': session_id,
                        'state': session.state,
                        'timestamp': time.time()
                    })
                    break
                
            except Exception as e:
                print(f"❌ Monitor error [{session_id}]: {e}")
            
            await asyncio.sleep(2)
        
        print(f"👋 Stopped monitoring: {session_id}")
    
    def detect_state(self, output: str) -> str:
        """Detect session state from output"""
        last_lines = output[-1000:]
        
        if re.search(r'(Planning|Thinking|Analyzing)', last_lines, re.I):
            return 'planning'
        elif re.search(r'(Editing|Writing|Making changes)', last_lines, re.I):
            return 'editing'
        elif re.search(r'(Running tests|Testing)', last_lines, re.I):
            return 'testing'
        elif re.search(r'(Done|Completed|Finished)', last_lines, re.I):
            return 'done'
        elif re.search(r'(Error|Failed)', last_lines, re.I):
            return 'failed'
        else:
            return 'running'
    
    def should_auto_confirm(self, output: str) -> bool:
        """Check if output contains confirmation prompt"""
        last_lines = output[-500:]
        
        patterns = [
            r'\(y/n\)',
            r'\[Y/n\]',
            r'Continue\?',
            r'Do you want',
            r'Apply.*\?',
            r'Accept.*\?',
            r'❯.*1\..*Yes',
        ]
        
        return any(re.search(p, last_lines, re.I) for p in patterns)
    
    async def send_keys(self, websocket, cmd):
        """Send keys to session"""
        session_id = cmd.get('sessionId')
        keys = cmd.get('keys', '')
        
        session = self.sessions.get(session_id)
        if not session:
            await websocket.send(json.dumps({
                'error': f'Session not found: {session_id}'
            }))
            return
        
        session.send_keys(keys)
        await websocket.send(json.dumps({
            'type': 'keys_sent',
            'sessionId': session_id
        }))
    
    async def capture_pane(self, websocket, cmd):
        """Capture pane output"""
        session_id = cmd.get('sessionId')
        lines = cmd.get('lines', 50)
        
        session = self.sessions.get(session_id)
        if not session:
            await websocket.send(json.dumps({
                'error': f'Session not found: {session_id}'
            }))
            return
        
        output = session.capture_pane(lines)
        await websocket.send(json.dumps({
            'type': 'pane_output',
            'sessionId': session_id,
            'output': output
        }))
    
    async def list_sessions(self, websocket):
        """List all sessions"""
        sessions_info = []
        for sid, session in self.sessions.items():
            sessions_info.append({
                'sessionId': sid,
                'tool': session.tool,
                'state': session.state,
                'path': session.context_path,
                'uptime': int(time.time() - session.created_at),
                'alive': session.exists()
            })
        
        await websocket.send(json.dumps({
            'type': 'sessions_list',
            'sessions': sessions_info
        }))
    
    async def kill_session(self, websocket, cmd):
        """Kill a session"""
        session_id = cmd.get('sessionId')
        
        session = self.sessions.get(session_id)
        if not session:
            await websocket.send(json.dumps({
                'error': f'Session not found: {session_id}'
            }))
            return
        
        # Stop monitoring
        if session.monitor_task:
            session.monitor_task.cancel()
        
        # Kill TMUX session
        session.kill()
        
        # Remove from dict
        del self.sessions[session_id]
        
        await websocket.send(json.dumps({
            'type': 'session_killed',
            'sessionId': session_id
        }))
        
        print(f"💀 Session killed: {session_id}")
    
    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        if not self.clients:
            return
        
        msg_json = json.dumps(message)
        results = await asyncio.gather(
            *[client.send(msg_json) for client in self.clients],
            return_exceptions=True
        )
        
        # Log errors
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                print(f"❌ Broadcast error to client {i}: {result}")
    
    async def start(self):
        """Start the Agent Server"""
        print(f"\n🎯 Starting WebSocket server on 0.0.0.0:{self.port}")
        print(f"   Waiting for connections...\n")
        
        async with websockets.serve(
            self.handle_client,
            "0.0.0.0",
            self.port,
            ping_interval=30,
            ping_timeout=10
        ):
            await asyncio.Future()  # Run forever


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='CM Agent Server')
    parser.add_argument('--port', type=int, default=9876,
                        help='WebSocket port (default: 9876)')
    parser.add_argument('--token', type=str,
                        help='Authentication token (or use CM_AGENT_TOKEN env)')
    
    args = parser.parse_args()
    
    server = CMAgentServer(
        port=args.port,
        auth_token=args.token
    )
    
    try:
        # Python 3.6 compatibility: asyncio.run() not available
        loop = asyncio.get_event_loop()
        loop.run_until_complete(server.start())
    except KeyboardInterrupt:
        print("\n\n👋 Shutting down...")
        sys.exit(0)


if __name__ == '__main__':
    main()
