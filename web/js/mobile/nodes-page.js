/**
 * Nodes route — Relay (frozen) vs Direct (embedded Hub).
 */
import { useDirectNodesUi } from './direct-session.js';
import { renderNodes as renderRelayNodes } from './nodes.js';
import { renderDirectNodes } from './nodes-direct.js';

export function renderNodes(container) {
  if (useDirectNodesUi()) return renderDirectNodes(container);
  return renderRelayNodes(container);
}
