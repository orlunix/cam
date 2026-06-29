/** CamFlow workflow.yaml parser/serializer (shared Desktop + Mobile). */

export function yamlScalar(raw) {
  let v = String(raw == null ? '' : raw).trim();
  if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
    v = v.slice(1, -1);
  }
  return v;
}

export function parseWorkflowYamlV0(text) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
  const parsed = { workflow: '', version: '', goal: '', nodes: [] };
  let inNodes = false;
  let node = null;
  let section = '';
  function pushNode() {
    if (!node) return;
    if (!Array.isArray(node.needs)) node.needs = [];
    if (!Array.isArray(node.steps)) node.steps = [];
    if (!node.run) node.run = {};
    if (!node.output_schema) node.output_schema = {};
    if (!node.verify) node.verify = {};
    parsed.nodes.push(node);
  }
  for (const line of lines) {
    let m;
    if (!inNodes) {
      if ((m = line.match(/^workflow:\s*(.*)$/))) parsed.workflow = yamlScalar(m[1]);
      else if ((m = line.match(/^version:\s*(.*)$/))) parsed.version = yamlScalar(m[1]);
      else if ((m = line.match(/^goal:\s*(.*)$/))) parsed.goal = yamlScalar(m[1]);
      else if (line.match(/^nodes:\s*$/)) inNodes = true;
      continue;
    }
    if ((m = line.match(/^  - id:\s*(.*)$/))) {
      pushNode();
      node = { id: yamlScalar(m[1]), goal: '', needs: [], run: {}, steps: [], output_schema: {}, verify: {}, retry: null };
      section = '';
      continue;
    }
    if (!node) continue;
    if ((m = line.match(/^    goal:\s*(.*)$/))) { node.goal = yamlScalar(m[1]); section = ''; continue; }
    if ((m = line.match(/^    retry:\s*(.*)$/))) { node.retry = yamlScalar(m[1]); section = ''; continue; }
    if ((m = line.match(/^    ([A-Za-z0-9_-]+):\s*$/))) { section = m[1]; continue; }
    if (section === 'needs' && (m = line.match(/^      -\s*(.*)$/))) { node.needs.push(yamlScalar(m[1])); continue; }
    if (section === 'steps' && (m = line.match(/^      -\s*(.*)$/))) { node.steps.push(yamlScalar(m[1])); continue; }
    if (section === 'run' && (m = line.match(/^      ([A-Za-z0-9_-]+):\s*(.*)$/))) { node.run[m[1]] = yamlScalar(m[2]); continue; }
    if (section === 'output_schema' && (m = line.match(/^      ([A-Za-z0-9_.-]+):\s*(.*)$/))) { node.output_schema[m[1]] = yamlScalar(m[2]); continue; }
    if (section === 'verify' && (m = line.match(/^      ([A-Za-z0-9_.-]+):\s*(.*)$/))) { node.verify[m[1]] = yamlScalar(m[2]); continue; }
  }
  pushNode();
  parsed.edges = parsed.nodes.flatMap(n => (n.needs || []).map(dep => ({ from: dep, to: n.id })));
  return parsed;
}

function workflowYamlValue(v) {
  const s = String(v == null ? '' : v);
  if (!s) return '""';
  if (/^[A-Za-z0-9_.@/:+\-]+$/.test(s)) return s;
  return JSON.stringify(s);
}

function workflowObjectYaml(obj, indent = '      ') {
  const keys = Object.keys(obj || {}).filter(k => String(k || '').trim());
  if (!keys.length) return [];
  return keys.map(k => `${indent}${k}: ${workflowYamlValue(obj[k])}`);
}

export function serializeWorkflowYaml(parsed) {
  const p = parsed || { nodes: [] };
  const out = [];
  out.push(`workflow: ${workflowYamlValue(p.workflow || 'workflow')}`);
  if (p.version) out.push(`version: ${workflowYamlValue(p.version)}`);
  if (p.goal) out.push(`goal: ${workflowYamlValue(p.goal)}`);
  out.push('nodes:');
  for (const node of (p.nodes || [])) {
    out.push(`  - id: ${workflowYamlValue(node.id || 'node')}`);
    if (node.goal) out.push(`    goal: ${workflowYamlValue(node.goal)}`);
    const needs = Array.isArray(node.needs) ? node.needs.filter(Boolean) : [];
    if (needs.length) {
      out.push('    needs:');
      needs.forEach(n => out.push(`      - ${workflowYamlValue(n)}`));
    }
    const run = node.run || {};
    if (Object.keys(run).length) {
      out.push('    run:');
      out.push(...workflowObjectYaml(run));
    }
    const steps = Array.isArray(node.steps) ? node.steps.filter(Boolean) : [];
    if (steps.length) {
      out.push('    steps:');
      steps.forEach(step => out.push(`      - ${workflowYamlValue(step)}`));
    }
    const output = node.output_schema || {};
    if (Object.keys(output).length) {
      out.push('    output_schema:');
      out.push(...workflowObjectYaml(output));
    }
    const verify = node.verify || {};
    if (Object.keys(verify).length) {
      out.push('    verify:');
      out.push(...workflowObjectYaml(verify));
    }
    if (node.retry != null && String(node.retry).trim() !== '') out.push(`    retry: ${workflowYamlValue(node.retry)}`);
  }
  return out.join('\n') + '\n';
}

export function workflowLinesFromTextarea(value) {
  return String(value || '').replace(/\r\n?/g, '\n').split('\n').map(s => s.trim()).filter(Boolean);
}

export function workflowObjectFromLines(value) {
  const obj = {};
  for (const line of workflowLinesFromTextarea(value)) {
    const idx = line.indexOf(':');
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim();
    const val = line.slice(idx + 1).trim();
    if (key) obj[key] = val;
  }
  return obj;
}

export function validateWorkflow(parsed) {
  const errors = [];
  const ids = new Set();
  for (const [idx, node] of (parsed?.nodes || []).entries()) {
    const where = node.id || `node ${idx + 1}`;
    if (!node.id) errors.push(`${where}: id is required`);
    if (node.id && ids.has(node.id)) errors.push(`${where}: duplicate id`);
    if (node.id) ids.add(node.id);
    if (!node.goal) errors.push(`${where}: goal is recommended`);
    if (!node.run || (!node.run.skill && !node.run.command)) errors.push(`${where}: run.skill or run.command is required`);
    const v = node.verify || {};
    const modes = ['criterion', 'command', 'human'].filter(k => v[k]);
    if (modes.length > 1) errors.push(`${where}: verify can use only one of criterion, command, human`);
  }
  for (const node of (parsed?.nodes || [])) {
    for (const dep of (node.needs || [])) {
      if (!ids.has(dep)) errors.push(`${node.id}: missing dependency ${dep}`);
    }
  }
  return errors;
}

export function workflowVerifyLabel(node) {
  const v = node?.verify || {};
  if (v.command) return 'command';
  if (v.human) return 'human';
  if (v.criterion) return 'evaluator';
  return 'auto';
}

export function workflowRunLabel(node) {
  const run = node?.run || {};
  if (run.skill) return `skill: ${run.skill}`;
  if (run.command) return `cmd: ${run.command}`;
  return 'run: unset';
}

export function nextWorkflowNodeId(nodes, prefix = 'node') {
  let n = (nodes || []).length + 1;
  let id = `${prefix}-${n}`;
  while ((nodes || []).some(x => x.id === id)) id = `${prefix}-${++n}`;
  return id;
}

export function applyWorkflowField(node, field, val) {
  if (field === 'id') node.id = val;
  else if (field === 'goal') node.goal = val;
  else if (field === 'needs') node.needs = val.split(',').map(s => s.trim()).filter(Boolean);
  else if (field === 'retry') node.retry = val || null;
  else if (field === 'steps') node.steps = workflowLinesFromTextarea(val);
  else if (field === 'run.skill') { node.run = node.run || {}; node.run.skill = val; }
  else if (field === 'run.command') { node.run = node.run || {}; node.run.command = val; }
  else if (field === 'output_schema') node.output_schema = workflowObjectFromLines(val);
  else if (field === 'verify') node.verify = workflowObjectFromLines(val);
}
