/**
 * Settings page — composes frozen Relay UI + new Direct tab.
 * Relay implementation lives in settings-relay.js (do not edit for Direct).
 */
import { renderRelaySettings } from './settings-relay.js';
import { renderDirectSettings } from './settings-direct.js';
import { renderAppearanceSettings } from './settings-appearance.js';

export { connectRelay, readRelayConfig, hasRelayConfig } from './settings-relay.js';
export { connectMobileDirect } from './settings-direct.js';

const SETTINGS_TAB_KEY = 'cam_mobile_settings_tab';
const VALID_TABS = ['relay', 'direct', 'appearance'];

export function renderSettings(container) {
  let tab = 'relay';
  try {
    const saved = localStorage.getItem(SETTINGS_TAB_KEY);
    if (VALID_TABS.includes(saved)) tab = saved;
  } catch {}

  container.innerHTML = `
    <div class="settings-mode-tabs">
      <button type="button" class="settings-mode-tab${tab === 'relay' ? ' active' : ''}" data-tab="relay">Relay</button>
      <button type="button" class="settings-mode-tab${tab === 'direct' ? ' active' : ''}" data-tab="direct">Direct</button>
      <button type="button" class="settings-mode-tab${tab === 'appearance' ? ' active' : ''}" data-tab="appearance">Appearance</button>
    </div>
    <div id="settings-panel-relay" class="settings-tab-panel"${tab !== 'relay' ? ' hidden' : ''}></div>
    <div id="settings-panel-direct" class="settings-tab-panel"${tab !== 'direct' ? ' hidden' : ''}></div>
    <div id="settings-panel-appearance" class="settings-tab-panel"${tab !== 'appearance' ? ' hidden' : ''}></div>
  `;

  const relayPanel = container.querySelector('#settings-panel-relay');
  const directPanel = container.querySelector('#settings-panel-direct');
  const appearancePanel = container.querySelector('#settings-panel-appearance');
  let directCleanup = null;
  let appearanceCleanup = null;

  function paintRelay() {
    renderRelaySettings(relayPanel);
  }
  function paintDirect() {
    if (directCleanup) directCleanup();
    directCleanup = renderDirectSettings(directPanel) || null;
  }
  function paintAppearance() {
    if (appearanceCleanup) appearanceCleanup();
    appearanceCleanup = renderAppearanceSettings(appearancePanel) || null;
  }

  function switchTab(name) {
    tab = VALID_TABS.includes(name) ? name : 'relay';
    try { localStorage.setItem(SETTINGS_TAB_KEY, tab); } catch {}
    container.querySelectorAll('.settings-mode-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    relayPanel.classList.toggle('hidden', tab !== 'relay');
    directPanel.classList.toggle('hidden', tab !== 'direct');
    appearancePanel.classList.toggle('hidden', tab !== 'appearance');
    if (tab === 'relay') paintRelay();
    else if (tab === 'direct') paintDirect();
    else paintAppearance();
  }

  container.querySelectorAll('.settings-mode-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  relayPanel.addEventListener('cam-relay-settings-changed', () => {
    if (tab === 'relay') paintRelay();
  });

  if (tab === 'direct') paintDirect();
  else if (tab === 'appearance') paintAppearance();
  else paintRelay();

  return () => {
    if (directCleanup) directCleanup();
    if (appearanceCleanup) appearanceCleanup();
  };
}
