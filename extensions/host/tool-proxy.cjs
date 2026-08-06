/* extensions/host/tool-proxy.cjs — deploy + invoke extension remote tools.
 *
 * main.py runs ONLY on remote SSH hosts (SPEC §3 iron rule: the hub
 * never runs Python). Deploy reuses the hardened transport path
 * (chunked SFTP upload, $HOME-anchored install); a per-extension
 * content hash marker avoids re-uploading unchanged tools.
 */

'use strict';

const fs     = require('node:fs');
const path   = require('node:path');
const crypto = require('node:crypto');

const EXT_REMOTE_ROOT = '.cam/extensions';  // under $HOME on the host

function _q(s) { return `'${String(s).replace(/'/g, `'\\''`)}'`; }

function _localToolHash(toolFile) {
  const content = fs.readFileSync(toolFile);
  return { content, hash: crypto.createHash('sha256').update(content).digest('hex').slice(0, 16) };
}

/** Deploy main.py to the host when missing or content changed.
 *  Returns { ok, deployed|present, remote } or { ok:false, error, detail }. */
async function ensureToolDeployed(sshTransport, baseOpts, extName, localDir) {
  const toolFile = path.join(localDir, 'main.py');
  if (!fs.existsSync(toolFile)) {
    return { ok: false, error: 'tool_missing', detail: `${extName}: main.py not found` };
  }
  const local = _localToolHash(toolFile);
  const dir = `$HOME/${EXT_REMOTE_ROOT}/${extName}`;
  const probe = await sshTransport.execRemote({
    ...baseOpts,
    command: `cat ${dir}/.hash 2>/dev/null || true`,
  });
  const remoteHash = probe && probe.ok ? String(probe.stdout || '').trim() : '';
  if (remoteHash && remoteHash === local.hash) {
    return { ok: true, present: true, remote: `${dir}/main.py` };
  }
  const mkdir = await sshTransport.execRemote({ ...baseOpts, command: `mkdir -p ${dir}` });
  if (!mkdir || !mkdir.ok) {
    return { ok: false, error: mkdir && mkdir.error || 'remote_mkdir_failed', detail: mkdir && (mkdir.detail || mkdir.stderr) || `mkdir -p ${dir} failed` };
  }
  const up = await sshTransport.writeRemoteFile({
    ...baseOpts,
    timeout_ms: 60000,
    remotePath: `${EXT_REMOTE_ROOT}/${extName}/main.py.tmp`,
    content: local.content,
  });
  if (!up || !up.ok) {
    return { ok: false, error: up && up.error || 'tool_upload_failed', detail: up && up.detail || 'failed to upload main.py' };
  }
  const install = await sshTransport.execRemote({
    ...baseOpts,
    command: `mv "$HOME/${EXT_REMOTE_ROOT}/${extName}/main.py.tmp" "${dir}/main.py" && printf %s ${local.hash} > "${dir}/.hash"`,
  });
  if (!install || !install.ok) {
    return { ok: false, error: install && install.error || 'tool_install_failed', detail: install && (install.detail || install.stderr) || 'failed to install main.py' };
  }
  return { ok: true, deployed: true, remote: `${dir}/main.py` };
}

/** Invoke `main.py <method> <json-args>` on the host; parse the JSON
 *  contract (SPEC §3). Returns the parsed object or { ok:false, ... }. */
async function callTool(sshTransport, baseOpts, extName, localDir, method, args, { timeoutMs = 30000 } = {}) {
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(String(method || ''))) {
    return { ok: false, error: 'invalid_method', detail: 'method must be an identifier' };
  }
  const deployed = await ensureToolDeployed(sshTransport, baseOpts, extName, localDir);
  if (!deployed.ok) return deployed;
  const payload = JSON.stringify(args == null ? {} : args);
  const cmd = `python3 "$HOME/${EXT_REMOTE_ROOT}/${extName}/main.py" ${_q(method)} ${_q(payload)}`;
  const r = await sshTransport.execRemote({ ...baseOpts, command: cmd, timeout_ms: timeoutMs });
  if (!r || !r.ok) {
    return { ok: false, error: r && r.error || 'exec_failed', detail: r && (r.detail || r.stderr) || 'remote tool call failed' };
  }
  const out = String(r.stdout || '').trim();
  let parsed;
  try { parsed = JSON.parse(out.split('\n').pop() || '{}'); }
  catch (e) {
    return { ok: false, error: 'tool_bad_output', detail: `tool did not return JSON: ${out.slice(0, 200)}` };
  }
  if (parsed && parsed.error) {
    return { ok: false, error: parsed.error, detail: parsed.detail || '' };
  }
  return { ok: true, result: parsed };
}

module.exports = { ensureToolDeployed, callTool, EXT_REMOTE_ROOT };
