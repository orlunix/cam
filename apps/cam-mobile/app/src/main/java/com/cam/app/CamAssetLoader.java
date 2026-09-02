package com.cam.app;

import android.content.Context;
import android.content.res.AssetManager;
import android.net.Uri;
import android.webkit.MimeTypeMap;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;

import java.io.InputStream;
import java.util.HashMap;
import java.util.Map;

/**
 * Serve bundled web assets over https://appassets.androidplatform.net/web/…
 * Avoids file:// viewport bugs on cold start (WebAppCapsule pattern).
 */
public final class CamAssetLoader {

    public static final String DOMAIN = "appassets.androidplatform.net";
    private static final String HTTPS = "https://" + DOMAIN + "/";

    private final AssetManager assets;
    private final String prefix;

    public CamAssetLoader(Context ctx, String assetSubdir) {
        this.assets = ctx.getAssets();
        this.prefix = assetSubdir.endsWith("/") ? assetSubdir : assetSubdir + "/";
    }

    /** Entry URL for a page under assets/web/ */
    public static String entryUrl(String page) {
        String p = page.startsWith("/") ? page.substring(1) : page;
        return HTTPS + "web/" + p + "?native=1";
    }

    public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
        if (request == null) return null;
        Uri uri = request.getUrl();
        if (uri == null || !DOMAIN.equals(uri.getHost())) return null;

        String path = uri.getPath();
        if (path == null || path.isEmpty()) return null;
        if (!path.startsWith("/web/")) return null;

        String rel = path.substring("/web/".length());
        if (rel.isEmpty()) rel = "mobile.html";
        String assetPath = prefix + rel;

        try {
            InputStream in = assets.open(assetPath);
            String mime = guessMime(assetPath);
            Map<String, String> headers = new HashMap<>();
            headers.put("Access-Control-Allow-Origin", "*");
            return new WebResourceResponse(mime, "UTF-8", 200, "OK", headers, in);
        } catch (Exception e) {
            return null;
        }
    }

    private static String guessMime(String path) {
        int dot = path.lastIndexOf('.');
        if (dot < 0) return "text/plain";
        String ext = path.substring(dot + 1).toLowerCase();
        switch (ext) {
            case "html": return "text/html";
            case "css": return "text/css";
            case "js": return "application/javascript";
            case "mjs": return "application/javascript";
            case "json": return "application/json";
            case "png": return "image/png";
            case "jpg":
            case "jpeg": return "image/jpeg";
            case "gif": return "image/gif";
            case "webp": return "image/webp";
            case "svg": return "image/svg+xml";
            case "woff2": return "font/woff2";
            case "woff": return "font/woff";
            default:
                String m = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
                return m != null ? m : "application/octet-stream";
        }
    }
}
