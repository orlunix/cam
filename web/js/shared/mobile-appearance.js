/** Mobile appearance — menu (UI scale) + output font sizes (localStorage + CSS vars). */

export const MOBILE_UI_FONT_KEY = 'cam_mobile_font_ui';
export const MOBILE_OUTPUT_FONT_KEY = 'cam_output_font_size';

export const MOBILE_UI_MIN = 11;
export const MOBILE_UI_MAX = 18;
export const MOBILE_UI_DEFAULT = 14;

export const MOBILE_OUTPUT_MIN = 10;
export const MOBILE_OUTPUT_MAX = 22;
export const MOBILE_OUTPUT_DEFAULT = 12;

function readInt(key, def, min, max) {
  try {
    const n = Number.parseInt(localStorage.getItem(key), 10);
    if (Number.isFinite(n) && n >= min && n <= max) return n;
  } catch {}
  return def;
}

function readFloat(key, def, min, max) {
  try {
    const n = Number.parseFloat(localStorage.getItem(key));
    if (Number.isFinite(n) && n >= min && n <= max) return Math.round(n * 2) / 2;
  } catch {}
  return def;
}

export function readMobileUiFont() {
  return readInt(MOBILE_UI_FONT_KEY, MOBILE_UI_DEFAULT, MOBILE_UI_MIN, MOBILE_UI_MAX);
}

export function readMobileOutputFont() {
  return readFloat(
    MOBILE_OUTPUT_FONT_KEY,
    MOBILE_OUTPUT_DEFAULT,
    MOBILE_OUTPUT_MIN,
    MOBILE_OUTPUT_MAX,
  );
}

/** Apply saved appearance to CSS custom properties. Idempotent. */
export function applyMobileAppearance() {
  if (typeof document === 'undefined' || !document.body) return;
  const ui = readMobileUiFont();
  const out = readMobileOutputFont();
  const outPx = `${out.toFixed(1)}px`;
  document.body.style.setProperty('--ui-font-size', `${ui}px`);
  document.body.style.setProperty('--output-font-size', outPx);
  if (document.documentElement) {
    document.documentElement.style.setProperty('--ui-font-size', `${ui}px`);
    document.documentElement.style.setProperty('--output-font-size', outPx);
  }
  try {
    localStorage.setItem(MOBILE_OUTPUT_FONT_KEY, out.toFixed(1));
  } catch {}
  clearTerminalChromeInlineFont();
  try {
    window.dispatchEvent(new CustomEvent('cam-mobile-appearance', { detail: { ui, output: out } }));
  } catch {}
}

/** Drop legacy inline font overrides on terminal chrome (Output sync experiment). */
export function clearTerminalChromeInlineFont() {
  if (typeof document === 'undefined') return;
  const sel = [
    '#content.agent-detail-active .detail-header .detail-title h2',
    '#content.agent-detail-active .detail-header .back-btn',
    '#content.agent-detail-active .detail-header .overflow-menu-btn',
    '#terminal-meta-bar',
    '#terminal-meta-bar .terminal-meta-left',
    '#terminal-meta-bar .terminal-meta-conn',
  ].join(', ');
  document.querySelectorAll(sel).forEach((el) => { el.style.fontSize = ''; });
}

if (typeof document !== 'undefined') {
  if (document.body) applyMobileAppearance();
  else document.addEventListener('DOMContentLoaded', applyMobileAppearance, { once: true });
}
