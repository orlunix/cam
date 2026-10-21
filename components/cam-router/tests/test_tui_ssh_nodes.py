"""Real OpenSSH + isolated SSH protocol server + real CAM Server and UI.

The SSH fixture authenticates disposable credentials, then runs the actual
remote commands under a private HOME. No user SSH config/known_hosts, system
password database, production Server or agent is modified.
"""
import base64
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid

import paramiko

import test_router_e2e as routing
import test_tui_e2e as baseline
from test_tui_features import UI


class SSHService:
    def __init__(self, node, user, password=None, key=None, shell="/bin/sh"):
        self.node, self.user, self.password, self.key = node, user, password, key
        self.shell = shell
        self.commands = []
        self.logins, self.transports, self.processes = [], [], []
        self.stop = threading.Event()
        self.hostkey = paramiko.RSAKey.generate(2048)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(8)
        self.listener.settimeout(.2)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.connection, args=(client,), daemon=True).start()

    def connection(self, client):
        fixture = self
        class Auth(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return "publickey" if fixture.key else "password"

            def check_auth_password(self, username, password):
                if username == fixture.user and password == fixture.password:
                    fixture.logins.append(username)
                    return paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED

            def check_auth_publickey(self, username, key):
                if username == fixture.user and fixture.key and key == fixture.key:
                    fixture.logins.append(username)
                    return paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED

            def check_channel_request(self, kind, channel_id):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_exec_request(self, channel, command):
                threading.Thread(target=fixture.execute, args=(channel, command), daemon=True).start()
                return True

        transport = paramiko.Transport(client)
        self.transports.append(transport)
        transport.add_server_key(self.hostkey)
        try:
            transport.start_server(server=Auth())
            channels = []
            while transport.is_active() and not self.stop.is_set():
                channel = transport.accept(.2)
                if channel is not None:
                    channels.append(channel)
                channels = [channel for channel in channels if not channel.closed]
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def execute(self, channel, command):
        self.commands.append(command.decode())
        process = subprocess.Popen([self.shell, "-c", command.decode()], env=self.node.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        def upload():
            try:
                while True:
                    data = channel.recv(65536)
                    if not data:
                        break
                    process.stdin.write(data)
                    process.stdin.flush()
            except (OSError, EOFError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        def download(stream, send):
            try:
                while True:
                    data = os.read(stream.fileno(), 65536)
                    if not data:
                        return
                    send(data)
            except (OSError, EOFError):
                pass
        threads = [threading.Thread(target=upload, daemon=True),
                   threading.Thread(target=download, args=(process.stdout, channel.sendall), daemon=True),
                   threading.Thread(target=download, args=(process.stderr, channel.sendall_stderr), daemon=True)]
        for thread in threads:
            thread.start()
        code = process.wait()
        for thread in threads[1:]:
            thread.join(timeout=2)
        try:
            # A Client may have already closed a multiplexed session. Never
            # send exit-status after Paramiko has acknowledged CHANNEL_CLOSE.
            with channel.lock:
                if channel.closed:
                    return
                channel.send_exit_status(code)
            channel.shutdown_write()
        finally:
            channel.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        for transport in self.transports:
            transport.close()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.thread.join(timeout=3)


class SSHNodeTests(unittest.TestCase):
    setUp = baseline.TuiTests.setUp
    tearDown = baseline.TuiTests.tearDown
    spawn = baseline.TuiTests.spawn
    until = baseline.TuiTests.until
    node = baseline.TuiTests.node
    managed_node = baseline.TuiTests.managed_node
    rpc = baseline.TuiTests.rpc

    def scenario(self, encrypted_key):
        node = self.managed_node()
        config_dir = node.home / ".config/cam-server"
        config_dir.mkdir(parents=True)
        node.env["XDG_CONFIG_HOME"] = str(node.home / ".config")
        routing.private_json(config_dir / (socket.gethostname() + ".json"), node.config)
        remote_bin = node.home / ".local/bin"
        remote_bin.mkdir(parents=True)
        shutil.copy2(routing.SERVER, remote_bin / "cam-server")
        user = "test-ssh-user" if encrypted_key else pwd.getpwuid(os.getuid()).pw_name
        secret = "disposable-" + uuid.uuid4().hex
        key = paramiko.RSAKey.generate(2048) if encrypted_key else None
        private_key = self.base / "ssh-key"
        if key:
            key.write_private_key_file(str(private_key), password=secret)
            private_key.chmod(0o600)
        service = SSHService(node, user, None if key else secret, key,
                             shell="/bin/tcsh" if key and Path('/bin/tcsh').exists() else "/bin/sh")
        self.addCleanup(service.close)
        # Only this wrapper isolates OpenSSH's config/known_hosts; all SSH
        # authentication and multiplexing run in the actual system binary.
        ssh_config = self.base / "ssh-config"
        ssh_config.write_text("Host *\n  UserKnownHostsFile %s\n  IdentitiesOnly yes\n%s" %
                              (self.base / "known-hosts", "" if key else "  PubkeyAuthentication no\n"))
        wrapper = self.base / "client-bin"
        wrapper.mkdir()
        (wrapper / "ssh").write_text('#!/bin/sh\nexec /usr/bin/ssh -F "$CAM_TEST_SSH_CONFIG" "$@"\n')
        (wrapper / "ssh").chmod(0o700)
        self.env.update(PATH=str(wrapper) + ":" + os.environ["PATH"], CAM_TEST_SSH_CONFIG=str(ssh_config), SSH_AUTH_SOCK="")
        # A fresh Client identity exercises automatic live pairing rather than
        # relying on the fixture's preconfigured Client grants.
        routing.private_json(self.client_config, {"client_id":"ssh-tui-" + uuid.uuid4().hex,
            "runtime_dir":str(self.runtime), "endpoints":[], "poll_seconds":1})
        self.daemon = self.spawn([str(baseline.DAEMON), "serve", "--config", str(self.client_config)], self.env)
        self.until(self.client_socket.exists)
        original_config = node.config_path.read_bytes()
        epoch = node.status()["server_epoch"]
        ui = UI(self, controlling_tty=True)
        ui.key(b"\x1bOQ")
        ui.click("+ Add")
        ui.field("Name (optional)", "SSH test")
        ui.field("Host", "127.0.0.1")
        ui.field("Port", str(service.port))
        if key:
            ui.field("Username (optional)", user)
            ui.field("SSH key (optional)", str(private_key))
        ui.save()
        ui.until(lambda: b"Are you sure you want to continue connecting" in ui.raw)
        ui.key(b"yes\n")
        prompt = b"passphrase for key" if key else b"password:"
        ui.until(lambda: prompt in bytes(ui.raw).lower())
        ui.key(secret.encode() + b"\n")
        ui.until(lambda: any(n["node_id"] == node.node_id and n["ready"] for n in self.rpc("snapshot")["nodes"]), timeout=25)
        self.assertIn(user, service.logins)
        self.assertEqual(node.status()["server_epoch"], epoch)
        self.assertEqual(node.config_path.read_bytes(), original_config)
        config = json.loads(self.client_config.read_text())
        self.assertEqual(config["endpoints"][0]["node_id"], node.node_id)
        self.assertNotIn(secret, self.client_config.read_text())
        self.assertNotIn(secret.encode(), bytes(ui.raw))
        command = config["endpoints"][0]["command"]
        if not key:
            self.assertNotIn("-l", command, "blank Username must preserve OpenSSH defaults")
        ui.key(b"\x1bOQ")
        ui.click("SSH test · tunnel")
        ui.click("Edit", exact=True)
        ui.field("Name (optional)", "Renamed SSH")
        ui.save()
        ui.until(lambda: any(e.get("name") == "Renamed SSH" for e in self.rpc("snapshot")["connections"])
                 and self.rpc("snapshot")["nodes"][0]["ready"])
        ui.key(b"\x1bOQ")
        ui.click("Renamed SSH · tunnel")
        ui.click("Disconnect", exact=True)
        ui.until(lambda: not self.rpc("snapshot")["nodes"][0]["ready"])
        ui.click("Connect", exact=True)
        ui.until(lambda: self.rpc("snapshot")["nodes"][0]["ready"])
        ui.close()

    def test_ssh_password_and_optional_username(self):
        self.scenario(False)

    def test_ssh_encrypted_key_and_explicit_username(self):
        self.scenario(True)
