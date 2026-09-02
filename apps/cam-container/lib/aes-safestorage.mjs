/* AES-256-GCM facade with Electron safeStorage's exact 3-method contract
 * (isEncryptionAvailable / encryptString → Buffer / decryptString(Buffer)),
 * injected into apps/cam-desktop/electron/credential-store.cjs via its
 * configure({ safeStorage }) seam.
 *
 * Key source: CAM_SECRET_KEY env (32 bytes as 64-hex or base64), else an
 * auto-generated 0600 key file <dataDir>/.secret-key (created on first
 * boot). Weaker than the desktop's OS-keychain binding — anyone with the
 * key file AND the credentials file can decrypt; the data volume is
 * sensitive, per README. */

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export function createAesSafeStorage({ dataDir, env = process.env } = {}) {
  let key = null;
  const raw = (env.CAM_SECRET_KEY || '').trim();
  if (raw) {
    key = /^[0-9a-fA-F]{64}$/.test(raw) ? Buffer.from(raw, 'hex') : Buffer.from(raw, 'base64');
    if (key.length !== 32) throw new Error('CAM_SECRET_KEY must decode to 32 bytes (64-hex or base64)');
  } else {
    const keyPath = path.join(dataDir, '.secret-key');
    try {
      const existing = fs.readFileSync(keyPath);
      if (existing.length === 32) key = existing;
    } catch (_) { /* absent → generate below */ }
    if (!key) {
      key = crypto.randomBytes(32);
      fs.mkdirSync(dataDir, { recursive: true });
      fs.writeFileSync(keyPath, key, { mode: 0o600 });
    }
  }
  return {
    isEncryptionAvailable: () => true,
    encryptString(plain) {
      const iv = crypto.randomBytes(12);
      const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
      const ct = Buffer.concat([cipher.update(String(plain), 'utf8'), cipher.final()]);
      return Buffer.concat([iv, cipher.getAuthTag(), ct]);
    },
    decryptString(blob) {
      const buf = Buffer.isBuffer(blob) ? blob : Buffer.from(blob);
      if (buf.length < 29) throw new Error('bad blob');
      const decipher = crypto.createDecipheriv('aes-256-gcm', key, buf.subarray(0, 12));
      decipher.setAuthTag(buf.subarray(12, 28));
      return Buffer.concat([decipher.update(buf.subarray(28)), decipher.final()]).toString('utf8');
    },
  };
}
