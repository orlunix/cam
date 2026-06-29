package com.cam.app;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONObject;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.security.KeyStore;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Iterator;
import java.util.Locale;
import java.util.TimeZone;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Encrypted credential store for remembered SSH passwords/passphrases.
 * Secrets never appear in embedded-hub.json — only opaque refs on machine records.
 */
public final class MobileCredentialStore {

    private static final String KEY_ALIAS = "cam_hub_credentials";
    private static final int GCM_TAG_BITS = 128;

    private final File storePath;
    private JSONObject store;

    public MobileCredentialStore(File dataDir) {
        File dir = dataDir != null ? dataDir : new File(".");
        if (!dir.exists()) dir.mkdirs();
        storePath = new File(dir, "embedded-hub-credentials.json");
        load();
    }

    public boolean available() {
        try {
            getOrCreateKey();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** Persist a secret. Returns { ok, ref, kind, saved_at } or { error, detail }. */
    public JSONObject put(String ref, String kind, String secret) throws Exception {
        if (ref == null || ref.trim().isEmpty()) {
            return new JSONObject().put("error", "invalid_ref").put("detail", "credential ref is required");
        }
        if (secret == null || secret.isEmpty()) {
            return new JSONObject().put("error", "invalid_secret").put("detail", "secret is required");
        }
        if (!available()) {
            return new JSONObject()
                .put("error", "credential_store_unavailable")
                .put("detail", "Android Keystore is not available; cannot remember secrets.");
        }
        SecretKey key = getOrCreateKey();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key);
        byte[] iv = cipher.getIV();
        byte[] ct = cipher.doFinal(secret.getBytes(StandardCharsets.UTF_8));

        String savedAt = nowIso();
        JSONObject item = new JSONObject();
        item.put("kind", kind != null ? kind : "password");
        item.put("iv_b64", Base64.encodeToString(iv, Base64.NO_WRAP));
        item.put("ct_b64", Base64.encodeToString(ct, Base64.NO_WRAP));
        item.put("saved_at", savedAt);
        store.getJSONObject("items").put(ref, item);
        save();

        return new JSONObject()
            .put("ok", true)
            .put("ref", ref)
            .put("kind", kind)
            .put("saved_at", savedAt);
    }

    public String get(String ref) {
        if (ref == null || ref.isEmpty()) return null;
        try {
            JSONObject items = store.optJSONObject("items");
            if (items == null || !items.has(ref)) return null;
            JSONObject item = items.getJSONObject(ref);
            byte[] iv = Base64.decode(item.getString("iv_b64"), Base64.NO_WRAP);
            byte[] ct = Base64.decode(item.getString("ct_b64"), Base64.NO_WRAP);
            SecretKey key = getOrCreateKey();
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(GCM_TAG_BITS, iv));
            byte[] plain = cipher.doFinal(ct);
            return new String(plain, StandardCharsets.UTF_8);
        } catch (Exception e) {
            return null;
        }
    }

    public void removeForContext(String contextId) throws Exception {
        if (contextId == null || contextId.isEmpty()) return;
        JSONObject items = store.optJSONObject("items");
        if (items == null) return;
        String prefix = contextId + ":";
        List<String> drop = new ArrayList<>();
        Iterator<String> keys = items.keys();
        while (keys.hasNext()) {
            String k = keys.next();
            if (k.startsWith(prefix)) drop.add(k);
        }
        for (String k : drop) items.remove(k);
        save();
    }

    private void load() {
        try {
            if (storePath.exists()) {
                String raw = readFile(storePath);
                store = new JSONObject(raw);
            } else {
                store = emptyStore();
                save();
            }
        } catch (Exception e) {
            store = emptyStore();
        }
        if (!store.has("items")) {
            try { store.put("items", new JSONObject()); } catch (Exception ignored) {}
        }
    }

    private JSONObject emptyStore() {
        try {
            return new JSONObject().put("version", 1).put("items", new JSONObject());
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private void save() throws Exception {
        File tmp = new File(storePath.getAbsolutePath() + ".tmp");
        writeFile(tmp, store.toString(2));
        if (storePath.exists() && !storePath.delete()) {
            throw new IOException("failed to replace credential store");
        }
        if (!tmp.renameTo(storePath)) {
            throw new IOException("failed to rename credential store");
        }
    }

    private SecretKey getOrCreateKey() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (!ks.containsAlias(KEY_ALIAS)) {
            KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            KeyGenParameterSpec spec = new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build();
            kg.init(spec);
            kg.generateKey();
        }
        KeyStore.Entry entry = ks.getEntry(KEY_ALIAS, null);
        return ((KeyStore.SecretKeyEntry) entry).getSecretKey();
    }

    private static String nowIso() {
        SimpleDateFormat fmt = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US);
        fmt.setTimeZone(TimeZone.getTimeZone("UTC"));
        return fmt.format(new Date());
    }

    private static String readFile(File f) throws java.io.IOException {
        java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
        try (java.io.FileInputStream in = new java.io.FileInputStream(f)) {
            byte[] b = new byte[4096];
            int n;
            while ((n = in.read(b)) >= 0) buf.write(b, 0, n);
        }
        return buf.toString(StandardCharsets.UTF_8.name());
    }

    private static void writeFile(File f, String text) throws java.io.IOException {
        try (java.io.FileOutputStream out = new java.io.FileOutputStream(f)) {
            out.write(text.getBytes(StandardCharsets.UTF_8));
        }
    }
}
