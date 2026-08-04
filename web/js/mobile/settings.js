/** Global Settings page — Relay, Direct, and Appearance. Workspace choices live with their endpoint. */
import { renderRelaySettings } from './settings-relay.js';
import { renderDirectSettings } from './settings-direct.js';
import { renderAppearanceSettings } from './settings-appearance.js';

export { connectRelay, readRelayConfig, hasRelayConfig } from './settings-relay.js';
export { connectMobileDirect } from './settings-direct.js';

const SETTINGS_TAB_KEY = 'cam_mobile_settings_tab';
const VALID_TABS = ['relay', 'direct', 'appearance'];

export function renderSettings(container) {
  let tab = 'direct';
  try { const saved = localStorage.getItem(SETTINGS_TAB_KEY); if (VALID_TABS.includes(saved)) tab = saved; } catch {}
  container.innerHTML = `<div class="settings-mode-tabs">
    <button type="button" class="settings-mode-tab${tab === 'direct' ? ' active' : ''}" data-tab="direct">Direct</button>
    <button type="button" class="settings-mode-tab${tab === 'relay' ? ' active' : ''}" data-tab="relay">Relay (deprecated)</button>
    <button type="button" class="settings-mode-tab${tab === 'appearance' ? ' active' : ''}" data-tab="appearance">Appearance</button>
  </div>
  <div id="settings-panel-relay" class="settings-tab-panel${tab !== 'relay' ? ' hidden' : ''}"></div>
  <div id="settings-panel-direct" class="settings-tab-panel${tab !== 'direct' ? ' hidden' : ''}"></div>
  <div id="settings-panel-appearance" class="settings-tab-panel${tab !== 'appearance' ? ' hidden' : ''}"></div>`;
  const panels = { relay: container.querySelector('#settings-panel-relay'), direct: container.querySelector('#settings-panel-direct'), appearance: container.querySelector('#settings-panel-appearance') };
  let cleanups = { direct: null, appearance: null };
  const paint = (name) => {
    if (name === 'relay') return renderRelaySettings(panels.relay);
    if (cleanups[name]) cleanups[name]();
    cleanups[name] = name === 'direct' ? renderDirectSettings(panels.direct) : renderAppearanceSettings(panels.appearance);
  };
  const switchTab = (name) => {
    tab = VALID_TABS.includes(name) ? name : 'direct';
    try { localStorage.setItem(SETTINGS_TAB_KEY, tab); } catch {}
    container.querySelectorAll('.settings-mode-tab').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
    Object.entries(panels).forEach(([name, panel]) => panel.classList.toggle('hidden', name !== tab));
    paint(tab);
  };
  container.querySelectorAll('.settings-mode-tab').forEach(button => button.addEventListener('click', () => switchTab(button.dataset.tab)));
  panels.relay.addEventListener('cam-relay-settings-changed', () => { if (tab === 'relay') paint('relay'); });
  paint(tab);
  return () => { Object.values(cleanups).forEach(cleanup => { if (cleanup) cleanup(); }); };
}
