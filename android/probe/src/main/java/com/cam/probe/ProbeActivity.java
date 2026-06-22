package com.cam.probe;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.webkit.ConsoleMessage;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.cam.app.CamAssetLoader;

/**
 * Minimal WebView probe — pure WebAppCapsule-style shell (no native chrome).
 * Side-by-side with CamUI V2 for A/B/C drift testing.
 */
public class ProbeActivity extends Activity {

    private static final String TAG = "CamProbe";
    private CamAssetLoader assetLoader;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        WebView webView = new WebView(this);
        webView.setBackgroundColor(Color.parseColor("#111111"));
        setContentView(webView);

        WebView.setWebContentsDebuggingEnabled(true);
        assetLoader = new CamAssetLoader(this, "web");

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                WebResourceResponse local = assetLoader.shouldInterceptRequest(request);
                return local != null ? local : super.shouldInterceptRequest(view, request);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                view.requestFocus();
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage cm) {
                Log.d(TAG, cm.message());
                return true;
            }
        });

        webView.loadUrl(CamAssetLoader.entryUrl("probe-launcher.html"));
    }

    @Override
    protected void onResume() {
        super.onResume();
        View v = findViewById(android.R.id.content);
        if (v instanceof android.view.ViewGroup) {
            android.view.ViewGroup vg = (android.view.ViewGroup) v;
            if (vg.getChildCount() > 0 && vg.getChildAt(0) instanceof WebView) {
                WebView w = (WebView) vg.getChildAt(0);
                w.onResume();
                w.resumeTimers();
            }
        }
    }

    @Override
    protected void onPause() {
        View v = findViewById(android.R.id.content);
        if (v instanceof android.view.ViewGroup) {
            android.view.ViewGroup vg = (android.view.ViewGroup) v;
            if (vg.getChildCount() > 0 && vg.getChildAt(0) instanceof WebView) {
                WebView w = (WebView) vg.getChildAt(0);
                w.onPause();
                w.pauseTimers();
            }
        }
        super.onPause();
    }
}
