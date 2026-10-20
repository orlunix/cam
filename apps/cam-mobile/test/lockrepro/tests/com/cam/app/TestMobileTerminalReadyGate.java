package com.cam.app;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

public final class TestMobileTerminalReadyGate {
    public static void main(String[] args) throws Exception {
        ByteArrayOutputStream wire = new ByteArrayOutputStream();
        Socket socket = new Socket();
        try {
            MobileWebSocket.Connection client = new MobileWebSocket.Connection(
                socket, new ByteArrayInputStream(new byte[0]), wire);
            Class<?> entryClass = Class.forName("com.cam.app.MobileTerminalManager$Entry");
            Constructor<?> constructor = entryClass.getDeclaredConstructor();
            constructor.setAccessible(true);
            Object entry = constructor.newInstance();
            Field clientField = entryClass.getDeclaredField("client");
            clientField.setAccessible(true);
            clientField.set(entry, client);

            MobileTerminalManager manager = new MobileTerminalManager(null);
            Method emitData = MobileTerminalManager.class.getDeclaredMethod(
                "emitData", entryClass, byte[].class, int.class);
            emitData.setAccessible(true);
            Method markStreamReady = MobileTerminalManager.class.getDeclaredMethod(
                "markStreamReady", entryClass);
            markStreamReady.setAccessible(true);

            byte[] initial = "TOP_SENTINEL\r\nBOTTOM_SENTINEL\r\n".getBytes(StandardCharsets.UTF_8);
            byte[] live = "LIVE_SENTINEL\r\n".getBytes(StandardCharsets.UTF_8);
            emitData.invoke(manager, entry, initial, initial.length);
            require(wire.size() == 0, "pre-ready output remains buffered");

            markStreamReady.invoke(manager, entry);
            List<byte[]> frames = readBinaryFrames(wire);
            require(frames.size() == 1, "ready flushes the initial frame without later output");
            require(equal(initial, frames.get(0)), "initial top and bottom bytes survive ready");

            markStreamReady.invoke(manager, entry);
            frames = readBinaryFrames(wire);
            require(frames.size() == 1, "a repeated resize does not duplicate the initial frame");

            emitData.invoke(manager, entry, live, live.length);
            frames = readBinaryFrames(wire);
            require(frames.size() == 2, "live output follows the flushed initial frame");
            require(equal(initial, frames.get(0)) && equal(live, frames.get(1)),
                "initial and live output retain their original order");
        } finally {
            socket.close();
        }
        verifyFailureCleanup();
        System.out.println(">>> TERMINAL READY-GATE VERIFIED: ordered initial flush and failure cleanup");
    }

    @SuppressWarnings("unchecked")
    private static void verifyFailureCleanup() throws Exception {
        java.io.OutputStream broken = new java.io.OutputStream() {
            @Override
            public void write(int value) throws java.io.IOException {
                throw new java.io.IOException("broken terminal socket");
            }
        };
        Socket socket = new Socket();
        try {
            MobileWebSocket.Connection client = new MobileWebSocket.Connection(
                socket, new ByteArrayInputStream(new byte[0]), broken);
            Class<?> entryClass = Class.forName("com.cam.app.MobileTerminalManager$Entry");
            Constructor<?> constructor = entryClass.getDeclaredConstructor();
            constructor.setAccessible(true);
            Object entry = constructor.newInstance();
            Field clientField = entryClass.getDeclaredField("client");
            clientField.setAccessible(true);
            clientField.set(entry, client);

            MobileTerminalManager manager = new MobileTerminalManager(null);
            Field sessionsField = MobileTerminalManager.class.getDeclaredField("sessions");
            sessionsField.setAccessible(true);
            java.util.Map<String, Object> sessions =
                (java.util.Map<String, Object>) sessionsField.get(manager);
            sessions.put("broken", entry);
            Method emitData = MobileTerminalManager.class.getDeclaredMethod(
                "emitData", entryClass, byte[].class, int.class);
            emitData.setAccessible(true);
            byte[] initial = "BUFFERED\r\n".getBytes(StandardCharsets.UTF_8);
            emitData.invoke(manager, entry, initial, initial.length);

            org.json.JSONObject result = manager.resize("broken", 47, 45);
            require(!result.optBoolean("ok", true)
                && "resize_failed".equals(result.optString("error")), "flush failure is reported");
            require(!sessions.containsKey("broken"), "flush failure drops the unusable terminal entry");
        } finally {
            socket.close();
        }
    }

    private static List<byte[]> readBinaryFrames(ByteArrayOutputStream wire) throws Exception {
        DataInputStream in = new DataInputStream(new ByteArrayInputStream(wire.toByteArray()));
        List<byte[]> frames = new ArrayList<>();
        while (in.available() > 0) {
            int b0 = in.readUnsignedByte();
            int b1 = in.readUnsignedByte();
            require((b0 & 0x0F) == MobileWebSocket.OP_BINARY, "terminal output uses binary frames");
            require((b1 & 0x80) == 0, "server frames are unmasked");
            long length = b1 & 0x7F;
            if (length == 126) {
                length = (in.readUnsignedByte() << 8) | in.readUnsignedByte();
            } else if (length == 127) {
                length = 0;
                for (int i = 0; i < 8; i++) length = (length << 8) | in.readUnsignedByte();
            }
            require(length >= 0 && length <= Integer.MAX_VALUE, "test frame length is valid");
            byte[] payload = in.readNBytes((int) length);
            require(payload.length == (int) length, "complete frame payload");
            frames.add(payload);
        }
        return frames;
    }

    private static boolean equal(byte[] a, byte[] b) {
        return java.util.Arrays.equals(a, b);
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
