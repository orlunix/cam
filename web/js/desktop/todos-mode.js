import { contextLabelFromContexts, storePathForContexts, readWorklogMarkdown } from '../shared/worklog-core.js';
import { mountTodosWorkspace } from '../shared/todos-workspace.js';

/** Desktop adapter: shared Todos behavior, responsive wide-screen shell. */
export function mountTodosMode({ state, api }) {
  const root = document.getElementById('mode-todos');
  if (!root) return;
  return mountTodosWorkspace(root, {
    platform: 'desktop',
    contextLabel: () => contextLabelFromContexts(state?.get?.('contexts') || []),
    storePath: () => storePathForContexts(state?.get?.('contexts') || []),
    reloadMarkdown: () => { const context = (state?.get?.('contexts') || []).find(ctx => ctx && (ctx.id || ctx.name)); return readWorklogMarkdown(api, context && (context.id || context.name)); },
  });
}
