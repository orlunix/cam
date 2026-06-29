/** Workflow.yaml templates — built-in defaults + user custom (localStorage). */

import { parseWorkflowYamlV0, serializeWorkflowYaml } from './workflow-yaml.js';
import { loadCustomTemplates, saveCustomTemplates, slugTemplateId } from './template-store.js';

export const WORKFLOW_CUSTOM_STORE = 'workflow';

export const BUILTIN_WORKFLOW_TEMPLATES = [
  {
    id: 'wf-single',
    label: 'Single task',
    hint: 'One node — simplest CamFlow workflow.',
    builtin: true,
    yaml: `workflow: single
version: "0"
goal: Complete one focused task end-to-end
nodes:
  - id: main
    goal: Do the assigned work
    steps:
      - Understand requirements
      - Implement or fix
      - Verify locally
    verify:
      criterion: Task outcome matches goal
`,
  },
  {
    id: 'wf-linear-pipeline',
    label: 'Linear pipeline (plan → build → verify)',
    hint: 'Three nodes in sequence — good default for feature work.',
    builtin: true,
    yaml: `workflow: linear-pipeline
version: "0"
goal: Plan, implement, and verify in order
nodes:
  - id: plan
    goal: Clarify scope and write a short plan
    steps:
      - Read relevant files
      - List concrete deliverables
  - id: build
    goal: Implement the plan
    needs:
      - plan
    steps:
      - Make focused code changes
      - Run targeted checks
  - id: verify
    goal: Verify and summarize
    needs:
      - build
    steps:
      - Run tests or lint
      - Summarize what changed
    verify:
      criterion: Tests pass or verify steps complete
`,
  },
  {
    id: 'wf-tdd',
    label: 'TDD cycle (test → implement → refactor)',
    hint: 'Test-first loop with explicit verify gate.',
    builtin: true,
    yaml: `workflow: tdd-cycle
version: "0"
goal: Iterate with tests as the back-pressure signal
nodes:
  - id: test
    goal: Write or update failing tests
    steps:
      - Identify behavior to cover
      - Add minimal failing test
  - id: implement
    goal: Make tests pass
    needs:
      - test
    steps:
      - Minimal implementation
      - Run test suite
  - id: refactor
    goal: Clean up while keeping tests green
    needs:
      - implement
    verify:
      criterion: All tests pass
`,
  },
];

export function listCustomWorkflowTemplates() {
  return loadCustomTemplates(WORKFLOW_CUSTOM_STORE);
}

export function listWorkflowTemplates() {
  return [...BUILTIN_WORKFLOW_TEMPLATES, ...listCustomWorkflowTemplates()];
}

export function workflowTemplateById(id) {
  return listWorkflowTemplates().find(t => t.id === id) || null;
}

export function isBuiltinWorkflowTemplate(id) {
  return BUILTIN_WORKFLOW_TEMPLATES.some(t => t.id === id);
}

export function saveCustomWorkflowTemplate({ label, hint, yaml, id }) {
  const name = String(label || '').trim();
  if (!name) throw new Error('Template label is required.');
  const text = String(yaml || '').trim();
  if (!text) throw new Error('Workflow YAML is required.');
  parseWorkflowYamlV0(text);
  const custom = listCustomWorkflowTemplates();
  const tid = String(id || '').trim() || slugTemplateId(name, 'wf');
  const next = {
    id: tid,
    label: name,
    hint: String(hint || '').trim() || 'Custom workflow template.',
    builtin: false,
    yaml: text,
  };
  const idx = custom.findIndex(t => t.id === tid);
  if (idx >= 0) custom[idx] = next;
  else custom.push(next);
  saveCustomTemplates(WORKFLOW_CUSTOM_STORE, custom);
  return next;
}

export function deleteCustomWorkflowTemplate(id) {
  if (isBuiltinWorkflowTemplate(id)) throw new Error('Built-in templates cannot be deleted.');
  const custom = listCustomWorkflowTemplates().filter(t => t.id !== id);
  saveCustomTemplates(WORKFLOW_CUSTOM_STORE, custom);
}

export function fillWorkflowTemplateSelect(selectEl, selectedId) {
  if (!selectEl) return;
  const cur = selectedId || selectEl.value || '';
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  selectEl.innerHTML = [
    '<option value="">— Apply template —</option>',
    ...listWorkflowTemplates().map(t => {
      const tag = t.builtin ? '' : ' ★';
      return `<option value="${esc(t.id)}">${esc(t.label)}${tag}</option>`;
    }),
  ].join('');
  if (cur && listWorkflowTemplates().some(t => t.id === cur)) selectEl.value = cur;
}

export function applyWorkflowTemplate(id, { goal } = {}) {
  const tpl = workflowTemplateById(id);
  if (!tpl?.yaml) return null;
  const parsed = parseWorkflowYamlV0(tpl.yaml);
  if (goal && String(goal).trim()) parsed.goal = String(goal).trim();
  return {
    parsed,
    text: serializeWorkflowYaml(parsed),
    template: tpl,
  };
}
