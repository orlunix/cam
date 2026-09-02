package com.cam.app;

import org.json.JSONObject;

import java.io.File;
import java.util.concurrent.ConcurrentHashMap;

/** JVM stub: in-memory credential store with the real class's API surface. */
public final class MobileCredentialStore {
    private final ConcurrentHashMap<String, String> map = new ConcurrentHashMap<>();

    public MobileCredentialStore(File dataDir) {}

    public boolean available() { return true; }

    public JSONObject put(String ref, String kind, String secret) throws Exception {
        if (ref == null || ref.trim().isEmpty()) {
            return new JSONObject().put("error", "invalid_ref").put("detail", "credential ref is required");
        }
        if (secret == null || secret.isEmpty()) {
            return new JSONObject().put("error", "invalid_secret").put("detail", "secret is required");
        }
        map.put(ref, secret);
        return new JSONObject()
            .put("ok", true)
            .put("ref", ref)
            .put("kind", kind != null ? kind : "password")
            .put("saved_at", "stub");
    }

    public String get(String ref) { return map.get(ref); }

    public void removeForContext(String contextId) throws Exception {
        map.keySet().removeIf(k -> k.startsWith(contextId + ":"));
    }
}
