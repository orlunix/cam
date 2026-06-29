/** Loop prompt templates — built-in defaults + user-managed custom (localStorage). */

import { parsePromptSections } from './prompt-sections.js';
import { loadCustomTemplates, saveCustomTemplates, slugTemplateId } from './template-store.js';

export const LOOP_PROMPT_MAX = 4000;
export const LOOP_CUSTOM_STORE = 'loop';

export const BUILTIN_LOOP_TEMPLATES = [
  {
    id: 'custom',
    label: 'Custom prompt',
    hint: 'Write your own loop prompt.',
    builtin: true,
    kind: 'custom',
  },
  {
    id: 'progress-review',
    label: 'Progress review (Goal / Checklist / Verify)',
    hint: 'Every 10m when idle: self-review plan and continue the next step.',
    builtin: true,
    kind: 'progress-review',
    defaults: {
      name: 'progress-review',
      schedule_type: 'every',
      schedule_value: '10m',
      no_expire: true,
    },
  },
  {
    id: 'continue-work',
    label: 'Continue work (light nudge)',
    hint: 'Every 10m when idle: short reminder to resume without a full review.',
    builtin: true,
    kind: 'continue-work',
    defaults: {
      name: 'continue-work',
      schedule_type: 'every',
      schedule_value: '10m',
      no_expire: true,
    },
  },
];

/** @deprecated use listLoopTemplates */
export const LOOP_TEMPLATES = BUILTIN_LOOP_TEMPLATES;

export function listCustomLoopTemplates() {
  return loadCustomTemplates(LOOP_CUSTOM_STORE);
}

export function listLoopTemplates() {
  return [...BUILTIN_LOOP_TEMPLATES, ...listCustomLoopTemplates()];
}

export function loopTemplateById(id) {
  return listLoopTemplates().find(t => t.id === id) || BUILTIN_LOOP_TEMPLATES[0];
}

export function isBuiltinLoopTemplate(id) {
  return BUILTIN_LOOP_TEMPLATES.some(t => t.id === id);
}

export function saveCustomLoopTemplate(entry) {
  const label = String(entry?.label || '').trim();
  if (!label) throw new Error('Template label is required.');
  const kind = String(entry?.kind || 'static').trim();
  const promptBody = String(entry?.promptBody || '').trim();
  if (kind === 'static' && !promptBody) throw new Error('Prompt text is required for a static template.');
  const custom = listCustomLoopTemplates();
  const id = String(entry?.id || '').trim() || slugTemplateId(label, 'loop');
  if (custom.some(t => t.id === id && t.id !== entry?.id)) {
    throw new Error('A template with this id already exists.');
  }
  const next = {
    id,
    label,
    hint: String(entry?.hint || '').trim() || 'Custom loop template.',
    builtin: false,
    kind: kind === 'progress-review' || kind === 'continue-work' ? kind : 'static',
    promptBody: kind === 'static' ? promptBody : '',
    defaults: {
      name: String(entry?.defaults?.name || id).slice(0, 48),
      schedule_type: entry?.defaults?.schedule_type || 'every',
      schedule_value: entry?.defaults?.schedule_value || '10m',
      no_expire: entry?.defaults?.no_expire !== false,
    },
  };
  const idx = custom.findIndex(t => t.id === id);
  if (idx >= 0) custom[idx] = next;
  else custom.push(next);
  saveCustomTemplates(LOOP_CUSTOM_STORE, custom);
  return next;
}

export function deleteCustomLoopTemplate(id) {
  if (isBuiltinLoopTemplate(id)) throw new Error('Built-in templates cannot be deleted.');
  const custom = listCustomLoopTemplates().filter(t => t.id !== id);
  saveCustomTemplates(LOOP_CUSTOM_STORE, custom);
}

export function fillLoopTemplateSelect(selectEl, selectedId) {
  if (!selectEl) return;
  const cur = selectedId || selectEl.value || 'custom';
  selectEl.innerHTML = listLoopTemplates().map(t => {
    const tag = t.builtin ? '' : ' ★';
    return `<option value="${escapeAttr(t.id)}">${escapeHtml(t.label)}${tag}</option>`;
  }).join('');
  selectEl.value = listLoopTemplates().some(t => t.id === cur) ? cur : 'custom';
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, '&quot;');
}

function truncateLoopPrompt(text, max = LOOP_PROMPT_MAX) {
  const s = String(text || '').trim();
  if (s.length <= max) return s;
  const cut = s.slice(0, max - 40);
  return `${cut}\n\n…(truncated to ${max} chars)`;
}

function sectionBlock(title, body) {
  const t = String(body || '').trim();
  if (!t) return '';
  return `## ${title}\n${t}`;
}

function applyPromptPlaceholders(text, sections) {
  const { goal, checklist, verify, body } = sections || {};
  return String(text || '')
    .replace(/\{\{goal\}\}/g, goal || '')
    .replace(/\{\{checklist\}\}/g, checklist || '')
    .replace(/\{\{verify\}\}/g, verify || '')
    .replace(/\{\{body\}\}/g, body || '');
}

function buildProgressReviewPrompt(sections) {
  const { goal, checklist, verify, body } = sections || {};
  const hasStructured = !!(goal || checklist || verify);
  const parts = [
    'Scheduled progress review — you are idle; the loop scheduler sent this prompt.',
    '',
    '## What to do',
    '1. Re-read your Goal, Checklist, and Verify criteria (below or in your system prompt file).',
    '2. Briefly report: done · in progress · blocked (3–6 lines max).',
    '3. Compare the checklist against the current workspace (files, tests, git).',
    '4. Pick the single highest-priority unfinished item.',
    '5. Continue working on it immediately — do not stop after the report unless truly blocked.',
    '',
    'If everything in Verify is satisfied, say so and wait for the next scheduled review.',
  ];
  if (hasStructured) {
    parts.push('');
    const g = sectionBlock('Goal (reference)', goal);
    const c = sectionBlock('Checklist (reference)', checklist);
    const v = sectionBlock('Verify (reference)', verify);
    if (g) parts.push(g);
    if (c) parts.push(c);
    if (v) parts.push(v);
  } else {
    parts.push(
      '',
      '## System prompt',
      'No structured Goal/Checklist/Verify was found when this loop was created.',
      'Read your camc block in CLAUDE.md / AGENTS.md, infer the plan, then continue.',
    );
    const extra = String(body || '').trim();
    if (extra) parts.push('', sectionBlock('Notes', extra));
  }
  return truncateLoopPrompt(parts.join('\n'));
}

function buildContinueWorkPrompt(sections) {
  const goal = String(sections?.goal || '').trim();
  const parts = [
    'Idle nudge — continue your assigned work.',
    '',
    goal
      ? `Goal reminder: ${goal.split('\n')[0]}`
      : 'Re-read your system prompt Goal, then resume the next unfinished step.',
    '',
    'Do not ask for permission to continue unless blocked. Start the next concrete action now.',
  ];
  return truncateLoopPrompt(parts.join('\n'));
}

export function buildLoopPrompt(templateId, sections) {
  const tpl = loopTemplateById(templateId);
  if (!tpl || tpl.id === 'custom' || tpl.kind === 'custom') return '';
  if (tpl.kind === 'static') {
    return truncateLoopPrompt(applyPromptPlaceholders(tpl.promptBody, sections));
  }
  switch (tpl.kind || templateId) {
    case 'progress-review':
      return buildProgressReviewPrompt(sections);
    case 'continue-work':
      return buildContinueWorkPrompt(sections);
    default:
      return '';
  }
}

export async function resolvePromptSectionsForAgent(api, agent, {
  extractSystemPromptBlock,
  systemPromptFileName,
} = {}) {
  let prompt = String(agent?.system_prompt || agent?.task?.system_prompt || '');
  const file = typeof systemPromptFileName === 'function'
    ? systemPromptFileName(agent)
    : '';
  if (file && api && typeof api.agentReadWorkspaceFile === 'function') {
    try {
      const resp = await api.agentReadWorkspaceFile(agent.id, file);
      if (resp && !resp.binary) {
        const raw = resp.content || '';
        const agentId = agent?.id || agent;
        const fromFile = typeof extractSystemPromptBlock === 'function'
          ? extractSystemPromptBlock(raw, agentId)
          : '';
        if (fromFile || !prompt) prompt = fromFile;
      }
    } catch (e) {
      const msg = String(e?.message || e || '');
      if (!/not_found|404/i.test(msg)) throw e;
    }
  }
  return parsePromptSections(prompt);
}

export function applyLoopTemplateDefaults(form, templateId) {
  const tpl = loopTemplateById(templateId);
  if (!tpl?.defaults || !form) return;
  const d = tpl.defaults;
  if (d.name && form.nameEl) form.nameEl.value = d.name;
  if (d.schedule_type && form.schedTypeEl) form.schedTypeEl.value = d.schedule_type;
  if (d.schedule_value && form.schedValEl) form.schedValEl.value = d.schedule_value;
  if (form.noExpireEl) form.noExpireEl.checked = !!d.no_expire;
  if (typeof form.onScheduleChange === 'function') form.onScheduleChange();
}
