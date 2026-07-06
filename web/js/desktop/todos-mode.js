import { contextLabelFromContexts, storePathForContexts } from '../shared/worklog-core.js';
import { mountTodosWorkspace } from '../shared/todos-workspace.js';

/** Desktop adapter: shared Todos behavior, responsive wide-screen shell. */
export function mountTodosMode({ state }) {
  const root = document.getElementById('mode-todos');
  if (!root) return;
  return mountTodosWorkspace(root, {
    platform: 'desktop',
    contextLabel: () => contextLabelFromContexts(state?.get?.('contexts') || []),
    storePath: () => storePathForContexts(state?.get?.('contexts') || []),
  });
}
