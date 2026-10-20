package com.cam.app;

import android.util.Base64;

import com.jcraft.jsch.HostKey;
import com.jcraft.jsch.HostKeyRepository;
import com.jcraft.jsch.UserInfo;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.util.Arrays;
import java.util.Properties;
import java.util.concurrent.ConcurrentHashMap;

/** Persistent trust-on-first-use repository; any later key change is rejected. */
final class MobileHostKeyRepository implements HostKeyRepository {
    private static final ConcurrentHashMap<String, Object> LOCKS = new ConcurrentHashMap<>();

    private final File file;

    MobileHostKeyRepository(File file) {
        this.file = file;
    }

    private static Object lockFor(File file) {
        return LOCKS.computeIfAbsent(file.getAbsolutePath(), k -> new Object());
    }

    @Override
    public int check(String host, byte[] key) {
        if (host == null || host.isEmpty() || key == null || key.length == 0) return CHANGED;
        synchronized (lockFor(file)) {
            Properties store = load(file);
            String encoded = Base64.encodeToString(key, Base64.NO_WRAP);
            String existing = store.getProperty(host);
            if (existing == null) {
                store.setProperty(host, encoded);
                save(file, store);
                return OK;
            }
            return Arrays.equals(Base64.decode(existing, Base64.NO_WRAP), key) ? OK : CHANGED;
        }
    }

    @Override
    public void add(HostKey hostKey, UserInfo userInfo) {
        if (hostKey == null) return;
        check(hostKey.getHost(), Base64.decode(hostKey.getKey(), Base64.NO_WRAP));
    }

    @Override
    public void remove(String host, String type) {
        synchronized (lockFor(file)) {
            Properties store = load(file);
            store.remove(host);
            save(file, store);
        }
    }

    @Override
    public void remove(String host, String type, byte[] key) {
        synchronized (lockFor(file)) {
            Properties store = load(file);
            String existing = store.getProperty(host);
            if (existing != null && Arrays.equals(Base64.decode(existing, Base64.NO_WRAP), key)) {
                store.remove(host);
                save(file, store);
            }
        }
    }

    static void forget(File file, String host, int port) {
        String name = hostKeyName(host, port);
        synchronized (lockFor(file)) {
            Properties store = load(file);
            store.remove(name);
            save(file, store);
        }
    }

    static String hostKeyName(String host, int port) {
        int p = port > 0 ? port : 22;
        return p == 22 ? host : "[" + host + "]:" + p;
    }

    @Override
    public String getKnownHostsRepositoryID() {
        return file.getAbsolutePath();
    }

    @Override
    public HostKey[] getHostKey() {
        return new HostKey[0];
    }

    @Override
    public HostKey[] getHostKey(String host, String type) {
        return new HostKey[0];
    }

    private static Properties load(File file) {
        Properties store = new Properties();
        if (!file.isFile()) return store;
        try (FileInputStream in = new FileInputStream(file)) {
            store.load(in);
            return store;
        } catch (Exception e) {
            throw new IllegalStateException("Cannot read SSH host-key trust store", e);
        }
    }

    private static void save(File file, Properties store) {
        try {
            File parent = file.getParentFile();
            if (parent != null && !parent.exists() && !parent.mkdirs()) {
                throw new IllegalStateException("Cannot create SSH host-key trust directory");
            }
            File tmp = new File(file.getAbsolutePath() + ".tmp");
            try (FileOutputStream out = new FileOutputStream(tmp)) {
                store.store(out, "CAM SSH TOFU host keys");
            }
            tmp.setReadable(true, true);
            tmp.setWritable(true, true);
            if (file.exists() && !file.delete()) {
                throw new IllegalStateException("Cannot replace SSH host-key trust store");
            }
            if (!tmp.renameTo(file)) throw new IllegalStateException("Cannot save SSH host-key trust store");
            file.setReadable(true, true);
            file.setWritable(true, true);
        } catch (Exception e) {
            throw new IllegalStateException("Cannot save SSH host-key trust store", e);
        }
    }
}
