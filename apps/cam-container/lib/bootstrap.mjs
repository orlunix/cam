/* Assembly for cam-container: wires the desktop app's plain-Node backends
 * (credential-store / ssh-transport / embedded-hub / assistant-host —
 * all Electron-free by design) into one process, mirroring main.cjs's
 * _ensureBackendsConfigured + localStart (apps/cam-desktop/electron/
 * main.cjs:195-248).
 *
 * Token precedence: CAM_API_TOKEN env → CAMUI_API_TOKEN env → persisted
 * <dataDir>/.api-token (generated once, 0600) — so container restarts
 * keep a stable URL even without an env var. Fingerprints only in logs.
 */

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { createAesSafeStorage } from './aes-safestorage.mjs';

const _require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..', '..'); // apps/cam-container/lib → repo root
const ELECTRON_DIR = path.join(REPO_ROOT, 'apps', 'cam-desktop', 'electron');

const credentialStore = _require(path.join(ELECTRON_DIR, 'credential-store.cjs'));
const sshTransport = _require(path.join(ELECTRON_DIR, 'ssh-transport.cjs'));
const embeddedHub = _require(path.join(ELECTRON_DIR, 'embedded-hub.cjs'));
const assistantHost = _require(path.join(ELECTRON_DIR, 'assistant-host.cjs'));

function _loadOrCreateToken(dataDir) {
  const f = path.join(dataDir, '.api-token');
  try {
    const t = fs.readFileSync(f, 'utf8').trim();
    if (t) return { token: t, source: 'file' };
  } catch (_) { /* absent */ }
  const token = crypto.randomBytes(24).toString('base64url');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(f, token + '\n', { mode: 0o600 });
  return { token, source: 'generated' };
}

export async function bootstrap({ env = process.env, onAssistantEvent, logger } = {}) {
  const dataDir = path.resolve(env.CAM_DATA_DIR || path.join(REPO_ROOT, 'apps', 'cam-container', 'data'));
  fs.mkdirSync(dataDir, { recursive: true });

  const logFile = path.join(dataDir, 'cam-container.log');
  const log = (m) => {
    const line = `${new Date().toISOString()} ${m}`;
    try { fs.appendFileSync(logFile, line + '\n'); } catch (_) {}
    if (logger) logger(line);
  };

  const safeStorage = createAesSafeStorage({ dataDir, env });
  credentialStore.configure({ safeStorage, dataDir });
  embeddedHub.configure({ credentialStore, sshTransport });
  assistantHost.configure({
    dataDir,
    credentialStore,
    logger: (m) => log(`[assistant] ${m}`),
    onEvent: (ev) => { try { onAssistantEvent && onAssistantEvent(ev); } catch (_) {} },
  });
  if (sshTransport && typeof sshTransport.setLogger === 'function') {
    sshTransport.setLogger((m) => log(`[ssh] ${m}`));
  }

  const envToken = (env.CAM_API_TOKEN || env.CAMUI_API_TOKEN || '').trim();
  const { token: apiToken, source: tokenSource } = envToken
    ? { token: envToken, source: 'env' }
    : _loadOrCreateToken(dataDir);

  const hubInfo = await embeddedHub.start({ dataDir, apiToken, appVersion: 'webui' });
  if (!hubInfo || hubInfo.ok !== true) {
    throw new Error(`embedded hub failed to start: ${(hubInfo && hubInfo.error) || 'unknown'}`);
  }
  // Feed the assistant the hub pair (same as main.cjs:234) so its `cam`
  // tool reaches the hub in-process.
  assistantHost.setHub({ url: hubInfo.apiUrl, token: hubInfo.apiToken });

  log(`hub up at ${hubInfo.apiUrl} (token ${tokenSource}: sha256:${crypto.createHash('sha256').update(apiToken).digest('hex').slice(0, 24)})`);

  return {
    dataDir,
    apiToken,
    hubApiUrl: hubInfo.apiUrl,
    hubPort: Number(new URL(hubInfo.apiUrl).port),
    log,
    logFile,
    embeddedHub,
    assistantHost,
    sshTransport,
    credentialStore,
  };
}
