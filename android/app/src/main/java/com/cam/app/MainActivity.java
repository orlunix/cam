package com.cam.app;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.StrictMode;
import android.util.Base64;
import android.util.Log;
import android.view.View;
import android.view.ViewTreeObserver;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import android.content.ContentResolver;
import android.database.Cursor;
import android.provider.OpenableColumns;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

public class MainActivity extends Activity {

    private static final String TAG = "CAM";
    private static final int FILE_CHOOSER_REQUEST = 1001;
    private static final int KEY_PICK_REQUEST = 1002;
    private static final int KEY_PICK_MAX_BYTES = 256 * 1024;
    private static final int CAM_BG = Color.parseColor("#111111");
    private static final String RESET_LAYOUT_JS =
        "if(window.__camScheduleLayoutResets){window.__camScheduleLayoutResets();}"
        + "else if(window.__camResetLayout){window.__camResetLayout();}else{"
        + "var m=document.querySelector('meta[name=viewport]');"
        + "if(m){m.setAttribute('content','width=device-width,initial-scale=1.0,minimum-scale=1.0,maximum-scale=1.0,user-scalable=no,viewport-fit=cover');}"
        + "var a=document.getElementById('app');"
        + "if(a){a.style.height='';a.style.width='';a.style.transform='';a.style.marginTop='';}"
        + "document.documentElement.style.height='';document.documentElement.style.width='';"
        + "document.body.style.height='';document.body.style.width='';"
        + "window.scrollTo(0,0);try{window.dispatchEvent(new Event('resize'));}catch(e){}}";

    private WebView webView;
    private CamAssetLoader assetLoader;
    private MobileEmbeddedHub embeddedHub;
    private ValueCallback<Uri[]> fileUploadCallback;
    private int lastLayoutW = -1;
    private int lastLayoutH = -1;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        StrictMode.VmPolicy.Builder builder = new StrictMode.VmPolicy.Builder();
        StrictMode.setVmPolicy(builder.build());

        Window window = getWindow();
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
        window.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        window.setStatusBarColor(CAM_BG);
        window.setNavigationBarColor(CAM_BG);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            WindowManager.LayoutParams lp = window.getAttributes();
            lp.layoutInDisplayCutoutMode =
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            window.setAttributes(lp);
        }

        setContentView(R.layout.activity_main);
        WebView.setWebContentsDebuggingEnabled(true);

        assetLoader = new CamAssetLoader(this, "web");
        embeddedHub = new MobileEmbeddedHub(this);

        webView = findViewById(R.id.webview);
        webView.setBackgroundColor(CAM_BG);
        webView.setOverScrollMode(View.OVER_SCROLL_NEVER);
        webView.setVerticalScrollBarEnabled(false);
        webView.setHorizontalScrollBarEnabled(false);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setDatabaseEnabled(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(false);
        settings.setTextZoom(100);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                WebResourceResponse local = assetLoader.shouldInterceptRequest(request);
                return local != null ? local : super.shouldInterceptRequest(view, request);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view,
                    android.webkit.WebResourceRequest request) {
                Uri uri = request.getUrl();
                String scheme = uri.getScheme();
                if ("http".equals(scheme) || "https".equals(scheme)) {
                    if (CamAssetLoader.DOMAIN.equals(uri.getHost())) {
                        return false;
                    }
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                    return true;
                }
                return false;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                view.requestFocus(View.FOCUS_DOWN);
                resetWebLayout();
                view.evaluateJavascript(
                    "(function(){try{if(window.__camInstallBridge)window.__camInstallBridge();}catch(e){}})();",
                    null);
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage cm) {
                Log.d(TAG, cm.message() + " [" + cm.sourceId() + ":" + cm.lineNumber() + "]");
                return true;
            }

            @Override
            public boolean onShowFileChooser(WebView view,
                    ValueCallback<Uri[]> callback,
                    FileChooserParams params) {
                if (fileUploadCallback != null) {
                    fileUploadCallback.onReceiveValue(null);
                }
                fileUploadCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_CHOOSER_REQUEST);
                } catch (Exception e) {
                    fileUploadCallback = null;
                    return false;
                }
                return true;
            }
        });

        webView.getViewTreeObserver().addOnGlobalLayoutListener(new ViewTreeObserver.OnGlobalLayoutListener() {
            @Override
            public void onGlobalLayout() {
                if (webView == null) return;
                int w = webView.getWidth();
                int h = webView.getHeight();
                if (w <= 0 || h <= 0) return;
                if (w == lastLayoutW && h == lastLayoutH) return;
                lastLayoutW = w;
                lastLayoutH = h;
                resetWebLayoutOnce();
            }
        });

        webView.addJavascriptInterface(
            new CamJsBridge(this, webView, embeddedHub), "CamBridge");

        String route = getIntent().getStringExtra("route");
        String url = CamAssetLoader.entryUrl("mobile.html");
        if (route != null && !route.isEmpty()) {
            url = url + (route.startsWith("#") ? route : "#" + route);
        }
        webView.loadUrl(url);
    }

    private void notifyKeyPickError(String message) {
        if (webView == null) return;
        webView.evaluateJavascript(
            "window.__camOnKeyPickError(" + JSONObject.quote(message) + ")", null);
    }

    private void notifyKeyPicked(JSONObject payload) {
        if (webView == null) return;
        webView.evaluateJavascript(
            "window.__camOnKeyPicked(" + JSONObject.quote(payload.toString()) + ")", null);
    }

    private String queryDisplayName(Uri uri) {
        String name = "private-key";
        ContentResolver resolver = getContentResolver();
        try (Cursor cursor = resolver.query(uri, null, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) name = cursor.getString(idx);
            }
        } catch (Exception e) {
            Log.w(TAG, "queryDisplayName failed", e);
        }
        return name;
    }

    private void handlePrivateKeyPick(Intent data) {
        if (data == null || data.getData() == null) {
            notifyKeyPickError("Import cancelled");
            return;
        }
        Uri uri = data.getData();
        try {
            final int takeFlags = data.getFlags()
                & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            getContentResolver().takePersistableUriPermission(uri, takeFlags);
        } catch (Exception e) {
            Log.w(TAG, "takePersistableUriPermission failed", e);
        }

        String label = queryDisplayName(uri);
        File keysDir = new File(getFilesDir(), "ssh-keys");
        if (!keysDir.exists() && !keysDir.mkdirs()) {
            notifyKeyPickError("Could not create key storage");
            return;
        }
        String safeName = label.replaceAll("[^a-zA-Z0-9._-]", "_");
        if (safeName.isEmpty()) safeName = "private-key";
        File dest = new File(keysDir, safeName);

        try (InputStream in = getContentResolver().openInputStream(uri)) {
            if (in == null) {
                notifyKeyPickError("Could not read selected file");
                return;
            }
            byte[] buf = new byte[8192];
            int total = 0;
            try (OutputStream out = new FileOutputStream(dest)) {
                int n;
                while ((n = in.read(buf)) >= 0) {
                    total += n;
                    if (total > KEY_PICK_MAX_BYTES) {
                        dest.delete();
                        notifyKeyPickError("Key file too large");
                        return;
                    }
                    if (n > 0) out.write(buf, 0, n);
                }
            }
            dest.setReadable(true, true);
            JSONObject obj = new JSONObject();
            obj.put("path", dest.getAbsolutePath());
            obj.put("label", label);
            notifyKeyPicked(obj);
        } catch (Exception e) {
            Log.e(TAG, "handlePrivateKeyPick failed", e);
            notifyKeyPickError(e.getMessage() != null ? e.getMessage() : "Import failed");
        }
    }

    void resetWebLayoutPublic() {
        resetWebLayout();
    }

    void openPrivateKeyPicker() {
        try {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("*/*");
            startActivityForResult(intent, CamJsBridge.keyPickRequestCode());
        } catch (Exception e) {
            Log.e(TAG, "pickPrivateKey failed", e);
            notifyKeyPickError("Could not open file picker");
        }
    }

    private void resetWebLayout() {
        if (webView == null) return;
        webView.evaluateJavascript(RESET_LAYOUT_JS, null);
        webView.getSettings().setTextZoom(100);
        webView.post(() -> {
            webView.requestLayout();
            webView.invalidate();
        });
        webView.postDelayed(this::resetWebLayoutOnce, 50);
        webView.postDelayed(this::resetWebLayoutOnce, 200);
        webView.postDelayed(this::resetWebLayoutOnce, 500);
        webView.postDelayed(this::resetWebLayoutOnce, 1000);
    }

    private void resetWebLayoutOnce() {
        if (webView == null) return;
        webView.evaluateJavascript(RESET_LAYOUT_JS, null);
        webView.requestLayout();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) resetWebLayout();
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == FILE_CHOOSER_REQUEST) {
            if (fileUploadCallback != null) {
                Uri[] results = null;
                if (resultCode == RESULT_OK && data != null && data.getData() != null) {
                    results = new Uri[]{data.getData()};
                }
                fileUploadCallback.onReceiveValue(results);
                fileUploadCallback = null;
            }
            return;
        }
        if (requestCode == KEY_PICK_REQUEST || requestCode == CamJsBridge.keyPickRequestCode()) {
            if (resultCode == RESULT_OK) {
                handlePrivateKeyPick(data);
            } else {
                notifyKeyPickError("Import cancelled");
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
        webView.resumeTimers();
        webView.requestFocus(View.FOCUS_DOWN);
        resetWebLayout();
    }

    @Override
    protected void onPause() {
        webView.onPause();
        webView.pauseTimers();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        webView.destroy();
        super.onDestroy();
    }
}
