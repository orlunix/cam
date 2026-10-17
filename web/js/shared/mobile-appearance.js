/** Mobile appearance — theme + menu (UI scale) + output font sizes (localStorage + CSS vars). */

export const MOBILE_THEME_KEY = 'cam_mobile_theme';
export const MOBILE_UI_FONT_KEY = 'cam_mobile_font_ui';
export const MOBILE_OUTPUT_FONT_KEY = 'cam_output_font_size';

export const MOBILE_VALID_THEMES = ['dark', 'light', 'system'];

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

export function readMobileTheme() {
  try {
    const v = localStorage.getItem(MOBILE_THEME_KEY);
    if (MOBILE_VALID_THEMES.includes(v)) return v;
  } catch {}
  return 'dark';
}

function systemPrefersLight() {
  try {
    return !!(typeof window !== 'undefined' && window.matchMedia
      && window.matchMedia('(prefers-color-scheme: light)').matches);
  } catch { return false; }
}

export function effectiveMobileTheme(setting = readMobileTheme()) {
  if (setting === 'light') return 'light';
  if (setting === 'dark') return 'dark';
  return systemPrefersLight() ? 'light' : 'dark';
}

let themeMediaHooked = false;
function hookSystemThemeListener() {
  if (themeMediaHooked || typeof window === 'undefined' || !window.matchMedia) return;
  themeMediaHooked = true;
  try {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
      if (readMobileTheme() === 'system') applyMobileAppearance();
    });
  } catch {}
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
  hookSystemThemeListener();
  // `data-theme` is only set when light; dark is the unattributed default.
  const themeSetting = readMobileTheme();
  const theme = effectiveMobileTheme(themeSetting);
  if (theme === 'light') document.body.setAttribute('data-theme', 'light');
  else document.body.removeAttribute('data-theme');
  document.body.setAttribute('data-theme-setting', themeSetting);
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
    window.dispatchEvent(new CustomEvent('cam-mobile-appearance', { detail: { ui, output: out, theme } }));
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
