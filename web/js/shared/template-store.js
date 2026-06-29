/** localStorage-backed custom template lists (Loop / Workflow). */

const PREFIX = 'cam.templates.';

export function loadCustomTemplates(storeKey) {
  try {
    const raw = localStorage.getItem(`${PREFIX}${storeKey}`);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveCustomTemplates(storeKey, items) {
  localStorage.setItem(`${PREFIX}${storeKey}`, JSON.stringify(items || []));
}

export function slugTemplateId(label, prefix = 'user') {
  const base = String(label || 'template')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32) || 'template';
  return `${prefix}-${base}-${Date.now().toString(36).slice(-4)}`;
}
