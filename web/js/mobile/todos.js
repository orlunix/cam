import { api, state } from './app.js';
import { contextLabelFromContexts, storePathForContexts, readWorklogMarkdown, escapeHtml, escapeAttr } from '../shared/worklog-core.js';
import { mountTodosWorkspace } from '../shared/todos-workspace.js';
import { isWorkspaceNode, saveWorkspaceContext, selectedWorkspaceContext, workspaceContextId, workspaceNodeLabel } from './workspace-context.js';

/** Mobile adapter: shared Todos behavior, single-column responsive shell. */
export function renderTodos(container) {
  const workspace = () => selectedWorkspaceContext(state.get('contexts') || [], 'todos');
  const workspaceSettings = {
    label: 'Settings',
    render: () => {
      const contexts = state.get('contexts') || [];
      const nodes = contexts.filter(isWorkspaceNode);
      const selected = workspace();
      const options = nodes.length
        ? nodes.map(node => `<option value=\"${escapeAttr(workspaceContextId(node))}\" ${selected && workspaceContextId(node) === workspaceContextId(selected) ? 'selected' : ''}>${escapeHtml(workspaceNodeLabel(node))}</option>`).join('')
        : '<option value=\"\">No SSH nodes available</option>';
      return `<section class=\"tw-endpoint-settings\"><h3>Todos node</h3><p>Choose the node whose workspace provides this Todo list.</p><label for=\"tw-workspace-node\">Workspace node</label><select id=\"tw-workspace-node\" data-action=\"workspace-node\" ${nodes.length ? '' : 'disabled'}>${options}</select><small>${nodes.length ? 'This choice applies only to Todos.' : 'Add an SSH node in Global Settings → Direct first.'}</small></section>`;
    },
    onChange: (value) => {
      const node = (state.get('contexts') || []).find(item => workspaceContextId(item) === value);
      saveWorkspaceContext(node, 'todos');
      state.set('todosWorkspaceContextId', workspaceContextId(node));
    },
  };
  return mountTodosWorkspace(container, {
    platform: 'mobile',
    contextLabel: () => contextLabelFromContexts([workspace()].filter(Boolean)),
    storePath: () => storePathForContexts([workspace()].filter(Boolean)),
    reloadMarkdown: () => { const context = workspace(); if (!context) throw new Error('Select a Todos node in the Todos Settings tab'); return readWorklogMarkdown(api, context.id || context.name); },
    workspaceSettings,
  });
}
