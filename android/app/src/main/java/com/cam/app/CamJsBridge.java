package com.cam.app;

import android.content.Intent;
import android.net.Uri;
import android.util.Base64;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;

/** WebView JavaScript bridge — must stay a named @JavascriptInterface class. */
public class CamJsBridge {

    private static final String TAG = "CAM";
    private static final int KEY_PICK_REQUEST = 1002;

    private final MainActivity activity;
    private final WebView webView;
    private final MobileEmbeddedHub embeddedHub;
    private final MobileTerminalManager terminalManager;

    public CamJsBridge(MainActivity activity, WebView webView, MobileEmbeddedHub embeddedHub) {
        this.activity = activity;
        this.webView = webView;
        this.embeddedHub = embeddedHub;
        this.terminalManager = new MobileTerminalManager(embeddedHub, webView);
    }

    private void termCallback(String cbId, JSONObject payload) {
        if (webView == null) return;
        String json = payload != null ? payload.toString() : "null";
        webView.post(() -> webView.evaluateJavascript(
            "window.__camTermCb(" + JSONObject.quote(cbId) + "," + JSONObject.quote(json) + ")", null));
    }

    @JavascriptInterface
    public void term_open(String cbId, String payloadJson) {
        new Thread(() -> {
            try {
                JSONObject p = payloadJson != null && !payloadJson.isEmpty()
                    ? new JSONObject(payloadJson) : new JSONObject();
                String agentId = p.optString("agentId", "");
                int cols = p.optInt("cols", 80);
                int rows = p.optInt("rows", 24);
                JSONObject hints = new JSONObject();
                if (p.has("machine_host") && !p.optString("machine_host", "").isEmpty()) {
                    hints.put("machine_host", p.optString("machine_host", ""));
                }
                if (p.has("machine_user")) hints.put("machine_user", p.optString("machine_user", ""));
                if (p.has("machine_port") && !p.isNull("machine_port")) {
                    hints.put("machine_port", p.optString("machine_port", ""));
                }
                JSONObject hintArg = hints.length() > 0 ? hints : null;
                termCallback(cbId, terminalManager.open(agentId, cols, rows, hintArg));
            } catch (Exception e) {
                try {
                    termCallback(cbId, new JSONObject()
                        .put("ok", false).put("error", "internal_error").put("detail", e.getMessage()));
                } catch (Exception ignored) {}
            }
        }).start();
    }

    @JavascriptInterface
    public void term_input(String cbId, String payloadJson) {
        new Thread(() -> {
            try {
                JSONObject p = payloadJson != null && !payloadJson.isEmpty()
                    ? new JSONObject(payloadJson) : new JSONObject();
                termCallback(cbId, terminalManager.input(
                    p.optString("sessionId", ""), p.optString("data", "")));
            } catch (Exception e) {
                try {
                    termCallback(cbId, new JSONObject()
                        .put("ok", false).put("error", "internal_error").put("detail", e.getMessage()));
                } catch (Exception ignored) {}
            }
        }).start();
    }

    @JavascriptInterface
    public void term_resize(String cbId, String payloadJson) {
        new Thread(() -> {
            try {
                JSONObject p = payloadJson != null && !payloadJson.isEmpty()
                    ? new JSONObject(payloadJson) : new JSONObject();
                termCallback(cbId, terminalManager.resize(
                    p.optString("sessionId", ""), p.optInt("cols", 80), p.optInt("rows", 24)));
            } catch (Exception e) {
                try {
                    termCallback(cbId, new JSONObject()
                        .put("ok", false).put("error", "internal_error").put("detail", e.getMessage()));
                } catch (Exception ignored) {}
            }
        }).start();
    }

    @JavascriptInterface
    public void term_close(String cbId, String payloadJson) {
        new Thread(() -> {
            try {
                JSONObject p = payloadJson != null && !payloadJson.isEmpty()
                    ? new JSONObject(payloadJson) : new JSONObject();
                termCallback(cbId, terminalManager.close(p.optString("sessionId", "")));
            } catch (Exception e) {
                try {
                    termCallback(cbId, new JSONObject()
                        .put("ok", false).put("error", "internal_error").put("detail", e.getMessage()));
                } catch (Exception ignored) {}
            }
        }).start();
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
    public boolean installApk(String base64Data) {
        try {
            byte[] apkBytes = Base64.decode(base64Data, Base64.DEFAULT);
            File apkFile = new File(activity.getCacheDir(), "cam-update.apk");
            FileOutputStream fos = new FileOutputStream(apkFile);
            fos.write(apkBytes);
            fos.close();
            apkFile.setReadable(true, false);
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(Uri.fromFile(apkFile),
                "application/vnd.android.package-archive");
            intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            activity.startActivity(intent);
            return true;
        } catch (Exception e) {
            Log.e(TAG, "installApk failed", e);
            return false;
        }
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
