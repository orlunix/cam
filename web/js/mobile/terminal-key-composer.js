/** Terminal key modifier composition (no DOM dependencies, unit-testable). */

const BASE_KEYS = {
  Enter: '\r',
  Escape: '\x1b',
  BSpace: '\x7f',
  Tab: '\t',
  BTab: '\x1b[Z',
  'C-c': '\x03',
};

const CSI_LETTERS = { Up: 'A', Down: 'B', Right: 'C', Left: 'D', Home: 'H', End: 'F' };
const CSI_TILDE = { DC: '3', PPage: '5', NPage: '6' };
const SHIFT_CHARS = { '1': '!', '2': '@', '3': '#', '4': '$', '/': '?' };
const MOD_LABELS = { ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift' };

function modifierCode(mods) {
  return 1 + (mods.shift ? 1 : 0) + (mods.alt ? 2 : 0) + (mods.ctrl ? 4 : 0);
}

function previewFor(key, mods) {
  const prefix = ['ctrl', 'alt', 'shift']
    .filter(name => mods[name])
    .map(name => MOD_LABELS[name]);
  return [...prefix, key].join('+');
}

function shiftedChar(ch) {
  return SHIFT_CHARS[ch] || ch.toUpperCase();
}

function ctrlChar(ch) {
  const code = ch.toLowerCase().charCodeAt(0);
  if (code >= 97 && code <= 122) return String.fromCharCode(code - 96);
  const specials = { '@': '\x00', '[': '\x1b', '\\': '\x1c', ']': '\x1d', '^': '\x1e', '_': '\x1f', '?': '\x7f' };
  return specials[ch] || ch;
}

/** Encode one keybar key with the active modifiers. kind is 'char' or 'key'. */
export function encodeTerminalKey(key, mods = {}, kind = 'char') {
  const preview = previewFor(key, mods);
  if (kind === 'char') {
    let ch = String(key || '');
    if (ch.length !== 1) throw new Error(`Unknown character key: ${key}`);
    if (mods.shift) ch = shiftedChar(ch);
    let bytes = mods.ctrl ? ctrlChar(ch) : ch;
    if (mods.alt) bytes = `\x1b${bytes}`;
    return { bytes, preview };
  }

  if (key === 'Tab') {
    const code = modifierCode(mods);
    if (code === 2) return { bytes: BASE_KEYS.BTab, preview };
    if (code > 1) return { bytes: `\x1b[1;${code}I`, preview };
    return { bytes: BASE_KEYS.Tab, preview };
  }
  if (CSI_LETTERS[key]) {
    const code = modifierCode(mods);
    const bytes = code > 1
      ? `\x1b[1;${code}${CSI_LETTERS[key]}`
      : `\x1b[${CSI_LETTERS[key]}`;
    return { bytes, preview };
  }
  if (CSI_TILDE[key]) {
    const code = modifierCode(mods);
    const bytes = code > 1
      ? `\x1b[${CSI_TILDE[key]};${code}~`
      : `\x1b[${CSI_TILDE[key]}~`;
    return { bytes, preview };
  }
  if (!(key in BASE_KEYS)) throw new Error(`Unknown key: ${key}`);
  let bytes = BASE_KEYS[key];
  if (mods.alt) bytes = `\x1b${bytes}`;
  return { bytes, preview };
}

/** Ordered pending key combinations plus latched modifiers. */
export function createTerminalKeyComposer() {
  const mods = { ctrl: false, alt: false, shift: false };
  let chunks = [];

  function clearModifiers() {
    for (const name of Object.keys(mods)) mods[name] = false;
  }

  return {
    toggleModifier(name) {
      if (!(name in mods)) throw new Error(`Unknown modifier: ${name}`);
      mods[name] = !mods[name];
      return { ...mods };
    },
    modifiers() {
      return { ...mods };
    },
    hasModifiers() {
      return Object.values(mods).some(Boolean);
    },
    hasPending() {
      return chunks.length > 0;
    },
    queueKey(key, kind = 'char') {
      const encoded = encodeTerminalKey(key, mods, kind);
      chunks.push(encoded);
      return encoded;
    },
    preview() {
      return chunks.map(chunk => chunk.preview).join(' ');
    },
    draft(text = '', enter = false) {
      return String(text ?? '') + chunks.map(chunk => chunk.bytes).join('') + (enter ? '\r' : '');
    },
    flush(text = '', enter = false) {
      const bytes = String(text ?? '') + chunks.map(chunk => chunk.bytes).join('') + (enter ? '\r' : '');
      chunks = [];
      clearModifiers();
      return bytes;
    },
    clear() {
      chunks = [];
      clearModifiers();
    },
  };
}
