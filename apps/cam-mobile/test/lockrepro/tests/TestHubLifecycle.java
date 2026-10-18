import com.cam.app.MobileEmbeddedHub;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;

public final class TestHubLifecycle {
    public static void main(String[] args) throws Exception {
        android.content.Context ctx = new android.content.Context(new java.io.File(args[0]));
        MobileEmbeddedHub hub = new MobileEmbeddedHub(ctx);
        require(hub.start().optBoolean("ok", false), "initial hub start");
        requireHttpOk(hub.currentPort());
        require(hub.restart().optBoolean("ok", false), "hub restart after executor shutdown");
        requireHttpOk(hub.currentPort());

        try (Socket socket = new Socket("127.0.0.1", hub.currentPort())) {
            OutputStream out = socket.getOutputStream();
            String request = "GET /term?token=wrong HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                + "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n";
            out.write(request.getBytes(StandardCharsets.US_ASCII));
            out.flush();
            BufferedReader reader = new BufferedReader(new InputStreamReader(
                socket.getInputStream(), StandardCharsets.US_ASCII));
            String status = reader.readLine();
            require(status != null && status.contains("401"), "terminal upgrade rejects bad token: " + status);
        }

        hub.stop();
        require(!hub.isRunning(), "hub stop closes listener");
        require(hub.activeTerminalSessionCount() == 0, "hub stop closes terminal sessions");
        System.out.println(">>> LIFECYCLE VERIFIED: restart, HTTP accept, terminal auth and stop cleanup");
    }

    private static void requireHttpOk(int port) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(
            "http://127.0.0.1:" + port + "/api/system/health").openConnection();
        conn.setConnectTimeout(3000);
        conn.setReadTimeout(3000);
        require(conn.getResponseCode() == 200, "HTTP health after (re)start");
        conn.disconnect();
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
