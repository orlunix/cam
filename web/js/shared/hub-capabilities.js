/**
 * Hub capability flags — single source for Desktop vs Mobile Direct parity.
 * Full Desktop embedded Hub omits nothing; mobile embedded Hub advertises gaps.
 * Relay/workstation Hubs without a capabilities block are treated as full.
 */

/** @typedef {{ context_crud: boolean, context_sync: boolean, agent_list: boolean, agent_ops: boolean, agent_terminal: boolean, skillm: boolean, ssh_config_import: boolean, runtime: string }} HubCapabilities */

export const HUB_CAP_FULL = Object.freeze({
  context_crud: true,
  context_sync: true,
  agent_list: true,
  agent_ops: true,
  agent_terminal: true,
  skillm: true,
  ssh_config_import: true,
  runtime: 'embedded-full',
});

export const HUB_CAP_MOBILE_EMBEDDED = Object.freeze({
  context_crud: true,
  context_sync: true,
  agent_list: true,
  agent_ops: true,
  agent_terminal: true,
  skillm: false,
  ssh_config_import: false,
  runtime: 'mobile-embedded',
});

let _cached = null;

export function parseHubCapabilities(healthOrConfig) {
  const raw = healthOrConfig && healthOrConfig.capabilities;
  if (!raw || typeof raw !== 'object') {
    const version = String(healthOrConfig && healthOrConfig.version || '');
    if (/cam-mobile-embedded/i.test(version)) return { ...HUB_CAP_MOBILE_EMBEDDED };
    return { ...HUB_CAP_FULL };
  }
  return {
    context_crud: raw.context_crud !== false,
    context_sync: !!raw.context_sync,
    agent_list: raw.agent_list !== false,
    agent_ops: !!raw.agent_ops,
    agent_terminal: !!raw.agent_terminal,
    skillm: !!raw.skillm,
    ssh_config_import: !!raw.ssh_config_import,
    runtime: String(raw.runtime || 'unknown'),
  };
}

export function setHubCapabilities(caps) {
  _cached = caps ? { ...caps } : null;
}

export function getHubCapabilities() {
  return _cached ? { ..._cached } : null;
}

export function syncHostSupported(caps = _cached) {
  if (!caps) return true;
  return !!caps.context_sync;
}

export function agentOpsSupported(caps = _cached) {
  if (!caps) return true;
  return !!caps.agent_ops;
}

export function terminalSupported(caps = _cached) {
  if (!caps) return true;
  return !!caps.agent_terminal || !!caps.agent_ops;
}

export function isMobileEmbeddedHub(caps = _cached) {
  if (!caps) return false;
  return caps.runtime === 'mobile-embedded';
}

/** Probe GET /api/system/health (auth required on mobile Hub). */
export async function refreshHubCapabilities(api, token) {
  if (!api) return getHubCapabilities();
  const hub = typeof window !== 'undefined' ? window.__camDirectHub : null;
  let health = null;
  try {
    if (hub && typeof hub.request === 'function' && token) {
      health = await hub.request('GET', '/api/system/health', null, token);
    } else if (typeof api.request === 'function') {
      health = await api.request('GET', '/api/system/health');
    }
  } catch {
    health = null;
  }
  const caps = parseHubCapabilities(health);
  setHubCapabilities(caps);
  return caps;
}

export function directHubLimitationHint(caps = _cached) {
  if (!caps || caps.runtime !== 'mobile-embedded') return '';
  const parts = [];
  if (!caps.agent_ops && !caps.agent_terminal) {
    parts.push('Live input/Terminal on phone Direct is not ready yet — use Relay for interactive agents');
  } else if (caps.runtime === 'mobile-embedded' && !caps.agent_ops) {
    parts.push('Phone Direct: use Terminal for live attach; Live/Full output need Relay or Desktop Hub');
  }
  return parts.join('. ');
}
