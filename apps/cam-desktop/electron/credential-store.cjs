/**
 * CAM Desktop — main-process credential store (CAM-DESK-DIRECT-018).
 *
 * Wraps Electron's `safeStorage` API to persist optional remembered
 * password/passphrase secrets for SSH node records. Stays main-only;
 * neither the renderer nor the embedded HTTP server ever sees the
 * decrypted secret directly — the renderer only receives metadata
 * (kind + saved_at + ref), and the embedded server only receives raw
 * input briefly during a create/update before this module hands it
 * to safeStorage.
 *
 * Storage: a single JSON file at
 *   <userData>/embedded-hub-credentials.json
 * with shape
 *   { version: 1, items: { <ref>: { kind, blob_b64, saved_at } } }
 * where `blob_b64` is base64 of `safeStorage.encryptString(secret)`.
 *
 * No native dependency. If safeStorage reports `isEncryptionAvailable()
 * === false` (e.g. headless Linux without gnome-keyring/kwallet), this
 * module refuses to save remembered secrets and returns
 * `{ ok: false, error: 'safe_storage_unavailable' }` so the caller can
 * surface a clear 400 to the renderer. We never fall back to plaintext.
 *
 * Test stub: the module exports `setSafeStorage(stub)` so the focused
 * Node smoke can inject a deterministic encrypt/decrypt without
 * pulling in Electron.
 */

'use strict';

const fs   = require('node:fs');
const path = require('node:path');

let _safeStorage = null;
const state = {
  storePath: null,
  store:     null,
};

function _emptyStore() {
  return { version: 1, items: {} };
}

function _load() {
  if (!state.storePath) return;
  try {
    const raw = fs.readFileSync(state.storePath, 'utf8');
    const parsed = JSON.parse(raw);
    state.store = {
      ..._emptyStore(),
      ...parsed,
      items: (parsed && parsed.items && typeof parsed.items === 'object')
        ? parsed.items
        : {},
    };
  } catch (e) {
    state.store = _emptyStore();
    if (e.code !== 'ENOENT') {
      // best-effort: rewrite a fresh store so subsequent saves don't
      // race the corrupted file.
      try { fs.writeFileSync(state.storePath, JSON.stringify(state.store, null, 2)); }
      catch {}
    }
  }
}

/** Atomically rewrite the credential store on disk. Returns
 *  `{ ok:true }` on success, `{ ok:false, error, detail }` on
 *  failure so callers can surface a clear error rather than treating
 *  a write failure as a quiet success. The previous "best-effort"
 *  shape masked disk errors; review msg#c067d6f4 asked for explicit
 *  propagation so a context create that would orphan an unsaved
 *  secret can fail loudly. */
function _save() {
  if (!state.storePath || !state.store) {
    return { ok: false, error: 'store_not_configured' };
  }
  const tmp = state.storePath + '.tmp';
  try {
    fs.writeFileSync(tmp, JSON.stringify(state.store, null, 2), { mode: 0o600 });
    fs.renameSync(tmp, state.storePath);
    return { ok: true };
  } catch (e) {
    // Make sure a stale tmp doesn't linger and confuse the next save.
    try { fs.unlinkSync(tmp); } catch {}
    return { ok: false, error: 'store_write_failed', detail: e && e.message };
  }
}

/** Configure once at app startup. `safeStorage` is the Electron
 *  `safeStorage` import (or a test stub with the same shape). */
function configure({ safeStorage, dataDir }) {
  _safeStorage = safeStorage || null;
  if (dataDir) {
    try { fs.mkdirSync(dataDir, { recursive: true }); } catch {}
    state.storePath = path.join(dataDir, 'embedded-hub-credentials.json');
    _load();
  }
}

/** Inject a different safeStorage at runtime (used by the focused
 *  Node smoke). */
function setSafeStorage(stub) {
  _safeStorage = stub || null;
}

function available() {
  if (!_safeStorage) return false;
  try {
    return !!_safeStorage.isEncryptionAvailable();
  } catch (_) {
    return false;
  }
}

/** Persist a secret. Returns `{ ok, error?, ref?, kind?, saved_at? }`.
 *  Refuses to save if encryption is unavailable. */
function put(ref, kind, secret) {
  if (!ref || typeof ref !== 'string') {
    return { ok: false, error: 'invalid_ref' };
  }
  if (typeof secret !== 'string' || secret.length === 0) {
    return { ok: false, error: 'invalid_secret' };
  }
  if (!available()) {
    return { ok: false, error: 'safe_storage_unavailable' };
  }
  if (!state.store) state.store = _emptyStore();
  let encrypted;
  try { encrypted = _safeStorage.encryptString(secret); }
  catch (e) { return { ok: false, error: 'encrypt_failed', detail: e && e.message }; }

  const blob_b64 = Buffer.isBuffer(encrypted)
    ? encrypted.toString('base64')
    : Buffer.from(encrypted).toString('base64');

  const item = {
    kind,
    blob_b64,
    saved_at: new Date().toISOString(),
  };
  state.store.items[ref] = item;
  const wrote = _save();
  if (!wrote.ok) {
    // Roll back the in-memory entry so a retry doesn't think the
    // secret is already stored and so future reads can't return a
    // value that isn't actually on disk.
    delete state.store.items[ref];
    return { ok: false, error: wrote.error || 'store_write_failed', detail: wrote.detail };
  }
  return { ok: true, ref, kind, saved_at: item.saved_at };
}

/** Return metadata only — never returns the raw secret. */
function metadata(ref) {
  if (!state.store || !state.store.items) return null;
  const item = state.store.items[ref];
  if (!item) return null;
  return { ref, kind: item.kind, saved_at: item.saved_at };
}

/** Decrypted secret for a ref. Only intended for main-process use
 *  (e.g. the future test-connection flow). Returns null on miss. */
function get(ref) {
  if (!available()) return null;
  if (!state.store || !state.store.items) return null;
  const item = state.store.items[ref];
  if (!item) return null;
  try {
    const buf = Buffer.from(item.blob_b64, 'base64');
    return _safeStorage.decryptString(buf);
  } catch (_) {
    return null;
  }
}

function remove(ref) {
  if (!state.store || !state.store.items) return;
  if (state.store.items[ref]) {
    delete state.store.items[ref];
    _save();
  }
}

/** Cascade-remove the well-known refs for a context id. Embedded Hub
 *  uses `${contextId}:password` and `${contextId}:passphrase` as
 *  refs, so this is enough to clean up on context delete or auth-
 *  method change. */
function removeForContext(contextId) {
  if (!contextId || !state.store || !state.store.items) return;
  for (const k of [`${contextId}:password`, `${contextId}:passphrase`]) {
    if (state.store.items[k]) delete state.store.items[k];
  }
  _save();
}

module.exports = {
  configure,
  setSafeStorage,
  available,
  put,
  get,
  metadata,
  remove,
  removeForContext,
};
