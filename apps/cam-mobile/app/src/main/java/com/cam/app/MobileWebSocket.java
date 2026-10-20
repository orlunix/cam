package com.cam.app;

import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** Minimal RFC6455 transport for the loopback terminal endpoint. */
final class MobileWebSocket {
    static final int OP_TEXT = 0x1;
    static final int OP_BINARY = 0x2;
    private static final int OP_CLOSE = 0x8;
    private static final int OP_PING = 0x9;
    private static final int OP_PONG = 0xA;
    private static final int MAX_MESSAGE_BYTES = 1024 * 1024;
    private static final String WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

    private MobileWebSocket() {}

    static final class Message {
        final int opcode;
        final byte[] payload;

        Message(int opcode, byte[] payload) {
            this.opcode = opcode;
            this.payload = payload;
        }
    }

    static final class Connection {
        private final Socket socket;
        private final InputStream in;
        private final OutputStream out;
        private final Object writeLock = new Object();
        private volatile boolean closed;

        Connection(Socket socket, InputStream in, OutputStream out) {
            this.socket = socket;
            this.in = in;
            this.out = out;
        }

        boolean isOpen() {
            return !closed && socket.isConnected() && !socket.isClosed();
        }

        void sendText(String text) throws IOException {
            sendFrame(OP_TEXT, text.getBytes(StandardCharsets.UTF_8));
        }

        void sendBinary(byte[] data, int offset, int length) throws IOException {
            if (length <= 0) return;
            byte[] payload;
            if (offset == 0 && length == data.length) {
                payload = data;
            } else {
                payload = new byte[length];
                System.arraycopy(data, offset, payload, 0, length);
            }
            sendFrame(OP_BINARY, payload);
        }

        private void sendFrame(int opcode, byte[] payload) throws IOException {
            synchronized (writeLock) {
                if (closed) throw new IOException("WebSocket closed");
                writeFrame(out, opcode, payload);
            }
        }

        /** Reads one complete text/binary message, answering ping and close inline. */
        Message readMessage() throws IOException {
            ByteArrayOutputStream message = null;
            int messageOpcode = 0;
            while (isOpen()) {
                Frame frame = readFrame(in);
                if (frame.opcode == OP_PING) {
                    sendFrame(OP_PONG, frame.payload);
                    continue;
                }
                if (frame.opcode == OP_PONG) continue;
                if (frame.opcode == OP_CLOSE) {
                    try { sendFrame(OP_CLOSE, frame.payload); } catch (IOException ignored) {}
                    closed = true;
                    return null;
                }
                if (frame.opcode == 0) {
                    if (message == null) throw new IOException("Unexpected continuation frame");
                } else if (frame.opcode == OP_TEXT || frame.opcode == OP_BINARY) {
                    if (message != null) throw new IOException("Nested fragmented message");
                    message = new ByteArrayOutputStream();
                    messageOpcode = frame.opcode;
                } else {
                    throw new IOException("Unsupported opcode " + frame.opcode);
                }
                if (message.size() + frame.payload.length > MAX_MESSAGE_BYTES) {
                    throw new IOException("WebSocket message too large");
                }
                message.write(frame.payload, 0, frame.payload.length);
                if (frame.fin) return new Message(messageOpcode, message.toByteArray());
            }
            return null;
        }

        void close() {
            if (closed) {
                try { socket.close(); } catch (IOException ignored) {}
                return;
            }
            closed = true;
            synchronized (writeLock) {
                try { writeFrame(out, OP_CLOSE, new byte[0]); } catch (IOException ignored) {}
                try { socket.close(); } catch (IOException ignored) {}
            }
        }
    }

    private static final class Frame {
        final boolean fin;
        final int opcode;
        final byte[] payload;

        Frame(boolean fin, int opcode, byte[] payload) {
            this.fin = fin;
            this.opcode = opcode;
            this.payload = payload;
        }
    }

    static Connection upgrade(Socket socket, InputStream in, OutputStream out, String key)
            throws Exception {
        if (key == null || key.trim().isEmpty()) throw new IOException("Missing Sec-WebSocket-Key");
        MessageDigest digest = MessageDigest.getInstance("SHA-1");
        String accept = Base64.encodeToString(
            digest.digest((key.trim() + WS_GUID).getBytes(StandardCharsets.US_ASCII)), Base64.NO_WRAP);
        String response = "HTTP/1.1 101 Switching Protocols\r\n"
            + "Upgrade: websocket\r\n"
            + "Connection: Upgrade\r\n"
            + "Sec-WebSocket-Accept: " + accept + "\r\n\r\n";
        out.write(response.getBytes(StandardCharsets.US_ASCII));
        out.flush();
        return new Connection(socket, in, out);
    }

    private static Frame readFrame(InputStream in) throws IOException {
        int b0 = in.read();
        int b1 = in.read();
        if (b0 < 0 || b1 < 0) throw new EOFException("WebSocket EOF");
        if ((b0 & 0x70) != 0) throw new IOException("WebSocket RSV bits not supported");
        boolean fin = (b0 & 0x80) != 0;
        int opcode = b0 & 0x0F;
        boolean masked = (b1 & 0x80) != 0;
        if (!masked) throw new IOException("Client frames must be masked");
        long length = b1 & 0x7F;
        if (length == 126) {
            length = ((long) readRequired(in) << 8) | readRequired(in);
        } else if (length == 127) {
            length = 0;
            for (int i = 0; i < 8; i++) length = (length << 8) | readRequired(in);
        }
        if (length < 0 || length > MAX_MESSAGE_BYTES) throw new IOException("Invalid frame length");
        if (opcode >= 0x8 && (!fin || length > 125)) throw new IOException("Invalid control frame");
        byte[] mask = readBytes(in, 4);
        byte[] payload = readBytes(in, (int) length);
        for (int i = 0; i < payload.length; i++) payload[i] = (byte) (payload[i] ^ mask[i % 4]);
        return new Frame(fin, opcode, payload);
    }

    private static int readRequired(InputStream in) throws IOException {
        int b = in.read();
        if (b < 0) throw new EOFException("WebSocket EOF");
        return b;
    }

    private static byte[] readBytes(InputStream in, int length) throws IOException {
        byte[] data = new byte[length];
        int offset = 0;
        while (offset < length) {
            int n = in.read(data, offset, length - offset);
            if (n < 0) throw new EOFException("WebSocket EOF");
            offset += n;
        }
        return data;
    }

    private static void writeFrame(OutputStream out, int opcode, byte[] payload) throws IOException {
        int length = payload.length;
        out.write(0x80 | opcode);
        if (length <= 125) {
            out.write(length);
        } else if (length <= 65535) {
            out.write(126);
            out.write((length >>> 8) & 0xFF);
            out.write(length & 0xFF);
        } else {
            out.write(127);
            for (int shift = 56; shift >= 0; shift -= 8) out.write((int) ((long) length >>> shift) & 0xFF);
        }
        out.write(payload);
        out.flush();
    }
}
