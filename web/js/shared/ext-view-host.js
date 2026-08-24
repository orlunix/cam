/* ext-view-host.js — iframe host for an extension view (SPEC §6).
 * The view is served by the loopback hub at /ext/<name>/<file>?token=…
 * inside a sandboxed iframe (scripts allowed; no same-origin, no forms,
 * no top navigation).
 *
 * The query token is the per-launch VIEW token (fetched from
 * /api/extensions/view-token) — never the API token: a hostile view
 * could otherwise read its own location and call /api/* directly,
 * bypassing the bridge capability gate. */

import { registerExtFrame, unregisterExtFrame } from './ext-bridge.js';

let _viewTokenPromise = null;
function _viewToken(api) {
  if (!_viewTokenPromise) {
    _viewTokenPromise = api.request('GET', '/api/extensions/view-token')
      .then(r => (r && r.token) || '')
      .catch(() => '');
  }
  return _viewTokenPromise;
}

/** Mount an extension view into `container`. Returns an unmount function.
 *  `bindContext` (optional) attaches a per-agent binding:
 *  { agentId, contextName } — the view reads it via app.context(). */
export function mountExtView(container, api, ext, bindContext = null) {
  container.innerHTML = '';
  const iframe = document.createElement('iframe');
  iframe.className = 'ext-view-frame';
  iframe.setAttribute('sandbox', 'allow-scripts');
  container.appendChild(iframe);
  const view = ext.viewFile || 'index.html';
  _viewToken(api).then((tok) => {
    // Cache-bust the view so updates to extension files are picked up on
    // the next mount (extension views are served by the loopback hub with
    // Cache-Control: no-store, but Chromium's iframe cache can still serve
    // a stale entry across app restarts).
    iframe.src = `${api.serverUrl}/ext/${encodeURIComponent(ext.name)}/${view}?token=${encodeURIComponent(tok)}&_=${Date.now()}`;
  });
  iframe.addEventListener('load', () => {
    if (iframe.contentWindow) {
      registerExtFrame(iframe.contentWindow, {
        name: ext.name,
        capabilities: ext.capabilities || [],
        context: bindContext,
      });
    }
  });
  return () => {
    if (iframe.contentWindow) unregisterExtFrame(iframe.contentWindow);
    iframe.remove();
  };
}
