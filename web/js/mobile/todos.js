import { state } from './app.js';
import { contextLabelFromContexts, storePathForContexts } from '../shared/worklog-core.js';
import { mountTodosWorkspace } from '../shared/todos-workspace.js';

/** Mobile adapter: shared Todos behavior, single-column responsive shell. */
export function renderTodos(container) {
  return mountTodosWorkspace(container, {
    platform: 'mobile',
    contextLabel: () => contextLabelFromContexts(state.get('contexts') || []),
    storePath: () => storePathForContexts(state.get('contexts') || []),
  });
}
