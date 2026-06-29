/**
 * Appearance settings — menu + output font sizes (mirrors Desktop Appearance tab).
 */
import {
  MOBILE_UI_FONT_KEY,
  MOBILE_OUTPUT_FONT_KEY,
  MOBILE_UI_MIN,
  MOBILE_UI_MAX,
  MOBILE_UI_DEFAULT,
  MOBILE_OUTPUT_MIN,
  MOBILE_OUTPUT_MAX,
  MOBILE_OUTPUT_DEFAULT,
  readMobileUiFont,
  readMobileOutputFont,
  applyMobileAppearance,
} from '../shared/mobile-appearance.js';

export function renderAppearanceSettings(container) {
  container.innerHTML = `
    <div class="appearance-settings-panel">
      <div class="page-header">
        <h2>Appearance</h2>
      </div>
      <form id="settings-form-appearance" class="form settings-section">
        <p class="settings-help">
          <strong>Menu</strong> — navigation, lists, terminal chrome. Uses Material-style type scale (14/12/11px tiers); scales together.<br>
          <strong>Output</strong> — xterm + capture text only. A± / pinch adjusts terminal font, not the toolbar.
        </p>
        <div class="form-group appearance-row">
          <label for="appearance-menu-font">Menu &amp; UI font size</label>
          <div class="appearance-control">
            <input type="range" id="appearance-menu-font"
              min="${MOBILE_UI_MIN}" max="${MOBILE_UI_MAX}" step="1">
            <span class="appearance-value" id="appearance-menu-font-val">${MOBILE_UI_DEFAULT} px</span>
          </div>
        </div>
        <div class="form-group appearance-row">
          <label for="appearance-output-font">Output &amp; terminal font size</label>
          <div class="appearance-control">
            <input type="range" id="appearance-output-font"
              min="${MOBILE_OUTPUT_MIN}" max="${MOBILE_OUTPUT_MAX}" step="0.5">
            <span class="appearance-value" id="appearance-output-font-val">${MOBILE_OUTPUT_DEFAULT} px</span>
          </div>
        </div>
        <div class="form-actions">
          <button type="button" class="btn-secondary btn-full" id="appearance-reset">Reset to defaults</button>
        </div>
        <div class="settings-status" id="settings-status-appearance" aria-live="polite"></div>
      </form>
    </div>
  `;

  const menuInput = container.querySelector('#appearance-menu-font');
  const menuVal = container.querySelector('#appearance-menu-font-val');
  const outInput = container.querySelector('#appearance-output-font');
  const outVal = container.querySelector('#appearance-output-font-val');
  const resetBtn = container.querySelector('#appearance-reset');
  const statusEl = container.querySelector('#settings-status-appearance');

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function syncFromStorage() {
    const ui = readMobileUiFont();
    const out = readMobileOutputFont();
    if (menuInput) menuInput.value = String(ui);
    if (menuVal) menuVal.textContent = `${ui} px`;
    if (outInput) outInput.value = String(out);
    if (outVal) outVal.textContent = `${out % 1 === 0 ? out : out.toFixed(1)} px`;
  }

  function commitMenu() {
    if (!menuInput) return;
    let n = Number.parseInt(menuInput.value, 10);
    if (!Number.isFinite(n)) n = MOBILE_UI_DEFAULT;
    n = Math.min(MOBILE_UI_MAX, Math.max(MOBILE_UI_MIN, n));
    menuInput.value = String(n);
    if (menuVal) menuVal.textContent = `${n} px`;
    try { localStorage.setItem(MOBILE_UI_FONT_KEY, String(n)); } catch {}
    applyMobileAppearance();
    setStatus(`Menu font set to ${n}px.`, 'is-ok');
  }

  function commitOutput() {
    if (!outInput) return;
    let n = Number.parseFloat(outInput.value);
    if (!Number.isFinite(n)) n = MOBILE_OUTPUT_DEFAULT;
    n = Math.min(MOBILE_OUTPUT_MAX, Math.max(MOBILE_OUTPUT_MIN, Math.round(n * 2) / 2));
    outInput.value = String(n);
    if (outVal) outVal.textContent = `${n % 1 === 0 ? n : n.toFixed(1)} px`;
    try { localStorage.setItem(MOBILE_OUTPUT_FONT_KEY, n.toFixed(1)); } catch {}
    applyMobileAppearance();
    setStatus(`Output font set to ${n % 1 === 0 ? n : n.toFixed(1)}px.`, 'is-ok');
  }

  syncFromStorage();

  menuInput?.addEventListener('input', commitMenu);
  outInput?.addEventListener('input', commitOutput);

  resetBtn?.addEventListener('click', () => {
    try {
      localStorage.removeItem(MOBILE_UI_FONT_KEY);
      localStorage.removeItem(MOBILE_OUTPUT_FONT_KEY);
    } catch {}
    applyMobileAppearance();
    syncFromStorage();
    setStatus('Reset to defaults.', 'is-ok');
  });

  return () => {};
}
