/* ext-view-host.js — iframe host for an extension view (SPEC §6).
 * The view is served by the loopback hub at /ext/<name>/<file>?token=…
 * inside a sandboxed iframe (scripts allowed; no same-origin, no forms,
 * no top navigation). */

import { registerExtFrame, unregisterExtFrame } from './ext-bridge.js';

/** Mount an extension view into `container`. `api` must carry the hub
 *  base URL + token (CamApi). Returns an unmount function. */
export function mountExtView(container, api, ext) {
  container.innerHTML = '';
  const iframe = document.createElement('iframe');
  iframe.className = 'ext-view-frame';
  iframe.setAttribute('sandbox', 'allow-scripts');
  const view = ext.viewFile || 'index.html';
  iframe.src = `${api.serverUrl}/ext/${encodeURIComponent(ext.name)}/${view}?token=${encodeURIComponent(api.token || '')}`;
  container.appendChild(iframe);
  iframe.addEventListener('load', () => {
    if (iframe.contentWindow) {
      registerExtFrame(iframe.contentWindow, { name: ext.name, capabilities: ext.capabilities || [] });
    }
  });
  return () => {
    if (iframe.contentWindow) unregisterExtFrame(iframe.contentWindow);
    iframe.remove();
  };
}
