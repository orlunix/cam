package com.cam.app;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Random;
import java.util.concurrent.atomic.AtomicReference;

public final class TestMobileWebSocket {
    public static void main(String[] args) throws Exception {
        AtomicReference<Throwable> serverFailure = new AtomicReference<>();
        try (ServerSocket listener = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            Thread server = new Thread(() -> {
                try (Socket socket = listener.accept()) {
                    InputStream in = socket.getInputStream();
                    OutputStream out = socket.getOutputStream();
                    BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.US_ASCII));
                    String key = null;
                    String line;
                    while ((line = reader.readLine()) != null && !line.isEmpty()) {
                        if (line.toLowerCase().startsWith("sec-websocket-key:")) {
                            key = line.substring(line.indexOf(':') + 1).trim();
                        }
                    }
                    MobileWebSocket.Connection ws = MobileWebSocket.upgrade(socket, in, out, key);
                    MobileWebSocket.Message first = ws.readMessage();
                    require(first.opcode == MobileWebSocket.OP_TEXT, "first message is text");
                    require("hello".equals(new String(first.payload, StandardCharsets.UTF_8)), "fragmented text reassembled");
                    MobileWebSocket.Message second = ws.readMessage();
                    require(second.opcode == MobileWebSocket.OP_BINARY, "second message is binary");
                    require(second.payload.length == 256 * 1024, "large binary message preserved");
                    for (int i = 0; i < second.payload.length; i++) {
                        if (second.payload[i] != (byte) (i % 251)) throw new AssertionError("binary mismatch at " + i);
                    }
                    ws.sendText("{\"ok\":true}");
                    ws.sendBinary(new byte[] { 0, (byte) 0xC3, (byte) 0xA9 }, 0, 3);
                    MobileWebSocket.Message closed = ws.readMessage();
                    require(closed == null, "close handshake completes");
                    ws.close();
                } catch (Throwable e) {
                    serverFailure.set(e);
                }
            }, "test-ws-server");
            server.start();

            try (Socket client = new Socket("127.0.0.1", listener.getLocalPort())) {
                OutputStream out = client.getOutputStream();
                InputStream in = client.getInputStream();
                String request = "GET /term?token=test HTTP/1.1\r\n"
                    + "Host: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    + "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n";
                out.write(request.getBytes(StandardCharsets.US_ASCII));
                out.flush();
                BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.US_ASCII));
                String status = reader.readLine();
                require(status.contains("101"), "upgrade accepted: " + status);
                boolean acceptOk = false;
                String line;
                while ((line = reader.readLine()) != null && !line.isEmpty()) {
                    if (line.equals("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")) acceptOk = true;
                }
                require(acceptOk, "RFC6455 accept hash");

                byte[] hello = "hello".getBytes(StandardCharsets.UTF_8);
                sendFrame(out, 0x1, Arrays.copyOfRange(hello, 0, 2), false);
                sendFrame(out, 0x0, Arrays.copyOfRange(hello, 2, 5), true);
                byte[] large = new byte[256 * 1024];
                for (int i = 0; i < large.length; i++) large[i] = (byte) (i % 251);
                sendFrame(out, 0x2, large, true);

                Frame text = readFrame(in);
                require(text.opcode == 0x1 && "{\"ok\":true}".equals(new String(text.payload, StandardCharsets.UTF_8)),
                    "server text result");
                Frame binary = readFrame(in);
                require(binary.opcode == 0x2 && Arrays.equals(binary.payload, new byte[] { 0, (byte) 0xC3, (byte) 0xA9 }),
                    "server binary bytes are not string-transcoded");

                sendFrame(out, 0x9, new byte[] { 7 }, true);
                Frame pong = readFrame(in);
                require(pong.opcode == 0xA && pong.payload[0] == 7, "ping receives pong");
                sendFrame(out, 0x8, new byte[0], true);
            }
            server.join(5000);
            require(!server.isAlive(), "server exits after close");
        }
        if (serverFailure.get() != null) throw new AssertionError("WebSocket server failed", serverFailure.get());
        verifyBackpressure();
        System.out.println(">>> WEBSOCKET VERIFIED: handshake, fragmentation, ordered binary, backpressure, ping and close");
    }

    private static void verifyBackpressure() throws Exception {
        try (ServerSocket listener = new ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"));
             Socket client = new Socket("127.0.0.1", listener.getLocalPort());
             Socket accepted = listener.accept()) {
            client.setReceiveBufferSize(1024);
            accepted.setSendBufferSize(1024);
            MobileWebSocket.Connection ws = new MobileWebSocket.Connection(
                accepted, accepted.getInputStream(), accepted.getOutputStream());
            byte[] payload = new byte[256 * 1024];
            AtomicReference<Throwable> failure = new AtomicReference<>();
            Thread writer = new Thread(() -> {
                try { ws.sendBinary(payload, 0, payload.length); }
                catch (Throwable e) { failure.set(e); }
            }, "test-ws-backpressure");
            writer.setDaemon(true);
            writer.start();
            Thread.sleep(100);
            require(writer.isAlive(), "large write waits for a slow reader instead of queueing");
            Frame frame = readFrame(client.getInputStream());
            require(frame.opcode == 0x2 && frame.payload.length == payload.length,
                "slow reader receives the complete frame");
            writer.join(3000);
            require(!writer.isAlive(), "writer unblocks after the reader drains the socket");
            if (failure.get() != null) throw new AssertionError("backpressure writer failed", failure.get());
            ws.close();
        }
    }

    private static final class Frame {
        final int opcode;
        final byte[] payload;
        Frame(int opcode, byte[] payload) { this.opcode = opcode; this.payload = payload; }
    }

    private static Frame readFrame(InputStream in) throws Exception {
        int b0 = in.read();
        int b1 = in.read();
        if (b0 < 0 || b1 < 0) throw new AssertionError("unexpected EOF");
        int length = b1 & 0x7F;
        if (length == 126) length = (in.read() << 8) | in.read();
        else if (length == 127) {
            long n = 0;
            for (int i = 0; i < 8; i++) n = (n << 8) | in.read();
            require(n <= Integer.MAX_VALUE, "test frame too large");
            length = (int) n;
        }
        byte[] payload = in.readNBytes(length);
        require(payload.length == length, "complete frame payload");
        return new Frame(b0 & 0x0F, payload);
    }

    private static void sendFrame(OutputStream out, int opcode, byte[] payload, boolean fin) throws Exception {
        ByteArrayOutputStream frame = new ByteArrayOutputStream();
        frame.write((fin ? 0x80 : 0) | opcode);
        int length = payload.length;
        if (length <= 125) {
            frame.write(0x80 | length);
        } else if (length <= 65535) {
            frame.write(0x80 | 126);
            frame.write((length >>> 8) & 0xFF);
            frame.write(length & 0xFF);
        } else {
            frame.write(0x80 | 127);
            for (int shift = 56; shift >= 0; shift -= 8) frame.write((int) ((long) length >>> shift) & 0xFF);
        }
        byte[] mask = new byte[4];
        new Random(42).nextBytes(mask);
        frame.write(mask);
        for (int i = 0; i < length; i++) frame.write(payload[i] ^ mask[i % 4]);
        out.write(frame.toByteArray());
        out.flush();
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
