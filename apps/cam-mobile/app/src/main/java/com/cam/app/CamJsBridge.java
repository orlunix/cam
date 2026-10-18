package com.cam.app;

import android.content.Intent;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

/** WebView JavaScript bridge — must stay a named @JavascriptInterface class. */
public class CamJsBridge {

    private static final String TAG = "CAM";
    private static final int KEY_PICK_REQUEST = 1002;

    private final MainActivity activity;
    private final WebView webView;
    private final MobileEmbeddedHub embeddedHub;
    private volatile boolean webTerminalKeepAlive;

    public CamJsBridge(MainActivity activity, WebView webView, MobileEmbeddedHub embeddedHub) {
        this.activity = activity;
        this.webView = webView;
        this.embeddedHub = embeddedHub;
    }

    void filesCallback(String cbId, boolean ok, String detail) {
        if (webView == null) return;
        try {
            JSONObject payload = new JSONObject();
            payload.put("ok", ok);
            if (detail != null) payload.put(ok ? "path" : "detail", detail);
            String json = payload.toString();
            webView.post(() -> webView.evaluateJavascript(
                "window.__camFilesCb(" + JSONObject.quote(cbId) + "," + ok + ","
                    + JSONObject.quote(json) + ")", null));
        } catch (Exception ignored) {}
    }

    /** ssh_config import: open the SAF picker; result lands in app storage. */
    @JavascriptInterface
    public void files_pickFile(String cbId, String payloadJson) {
        try {
            activity.openConfigFilePicker(cbId);
        } catch (Exception e) {
            filesCallback(cbId, false, e.getMessage());
        }
    }

    /** Export helper: share text (e.g. ssh_config) via the Android share sheet. */
    @JavascriptInterface
    public void files_saveText(String cbId, String payloadJson) {
        try {
            JSONObject p = payloadJson != null && !payloadJson.isEmpty()
                ? new JSONObject(payloadJson) : new JSONObject();
            String name = p.optString("defaultName", "");
            if (name.isEmpty()) name = p.optString("filename", "cam-export.txt");
            String content = p.optString("content", "");
            Intent send = new Intent(Intent.ACTION_SEND);
            send.setType("text/plain");
            send.putExtra(Intent.EXTRA_TEXT, content);
            send.putExtra(Intent.EXTRA_SUBJECT, name);
            send.putExtra(Intent.EXTRA_TITLE, name);
            activity.startActivity(Intent.createChooser(send, name));
            filesCallback(cbId, true, name);
        } catch (Exception e) {
            filesCallback(cbId, false, e.getMessage());
        }
    }

    /** Relay terminals are JavaScript-backed, so expose their attached state to Activity. */
    @JavascriptInterface
    public void setTerminalBackgroundKeepAlive(boolean keepAlive) {
        webTerminalKeepAlive = keepAlive;
        activity.runOnUiThread(activity::syncTerminalForegroundState);
    }

    boolean shouldKeepTerminalAliveInBackground() {
        return webTerminalKeepAlive || embeddedHub.hasActiveTerminalSessions();
    }

    private void directHubCallback(String cbId, boolean ok, JSONObject payload) {
        if (webView == null) return;
        String json = payload != null ? payload.toString() : "null";
        webView.post(() -> webView.evaluateJavascript(
            "window.__camDirectHubCb(" + JSONObject.quote(cbId) + ","
                + ok + "," + JSONObject.quote(json) + ")", null));
    }

    @JavascriptInterface
    public void directHub_check(String cbId) {
        new Thread(() -> directHubCallback(cbId, true, embeddedHub.check())).start();
    }

    @JavascriptInterface
    public void directHub_start(String cbId) {
        new Thread(() -> {
            JSONObject res = embeddedHub.start();
            directHubCallback(cbId, res.optBoolean("ok", false), res);
        }).start();
    }

    @JavascriptInterface
    public void directHub_stop(String cbId) {
        new Thread(() -> directHubCallback(cbId, true, embeddedHub.stop())).start();
    }

    @JavascriptInterface
    public void directHub_restart(String cbId) {
        new Thread(() -> {
            JSONObject res = embeddedHub.restart();
            directHubCallback(cbId, res.optBoolean("ok", false), res);
        }).start();
    }

    @JavascriptInterface
    public void directHub_logs(String cbId) {
        new Thread(() -> directHubCallback(cbId, true, embeddedHub.logs())).start();
    }

    @JavascriptInterface
    public void directHub_getProfile(String cbId) {
        new Thread(() -> directHubCallback(cbId, true, embeddedHub.getProfile())).start();
    }

    @JavascriptInterface
    public void directHub_request(String cbId, String method, String path, String bodyJson, String token) {
        new Thread(() -> {
            String auth = (token != null && !token.isEmpty()) ? ("Bearer " + token) : "";
            JSONObject res = embeddedHub.apiRequest(method, path, auth, bodyJson != null ? bodyJson : "");
            directHubCallback(cbId, res.optBoolean("ok", false), res);
        }).start();
    }

    @JavascriptInterface
    public void setLoopbackMixedContentEnabled(boolean enabled) {
        activity.runOnUiThread(() -> activity.setLoopbackMixedContentEnabled(enabled));
    }

    @JavascriptInterface
    public void notifyAppReady() {
        activity.runOnUiThread(() -> {
            Log.d(TAG, "notifyAppReady()");
            if (webView != null) {
                webView.requestFocus(android.view.View.FOCUS_DOWN);
            }
            activity.resetWebLayoutPublic();
        });
    }

    @JavascriptInterface
    public void restartApp(String route) {
        activity.runOnUiThread(() -> {
            Log.d(TAG, "restartApp() route=" + route);
            Intent intent = activity.getPackageManager()
                .getLaunchIntentForPackage(activity.getPackageName());
            if (intent != null) {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
                intent.putExtra("route", route != null ? route : "#/");
                activity.startActivity(intent);
                activity.finish();
                android.os.Process.killProcess(android.os.Process.myPid());
            }
        });
    }

    @JavascriptInterface
    public String getAppVersion() {
        try {
            return activity.getPackageManager()
                .getPackageInfo(activity.getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "unknown";
        }
    }

    @JavascriptInterface
    public int getAppVersionCode() {
        try {
            return activity.getPackageManager()
                .getPackageInfo(activity.getPackageName(), 0).versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    @JavascriptInterface
    public void pickPrivateKey() {
        activity.runOnUiThread(() -> activity.openPrivateKeyPicker());
    }

    public static int keyPickRequestCode() {
        return KEY_PICK_REQUEST;
    }
}
