/**
 * CAM API Client — supports direct HTTP, relay HTTP proxy, and legacy relay REST-over-WS modes.
 */

export class CamApi {
  constructor() {
    this.mode = 'disconnected'; // 'direct' | 'relay' | 'disconnected'
    this.serverUrl = '';
    this.token = '';
    this.relayUrl = '';
    this.relayToken = '';
    this.ws = null;
    this._requestMap = new Map();
    this._reqCounter = 0;
    this._eventHandlers = [];
    this._reconnectTimer = null;
    this._eventWs = null;
    this._pingTimer = null;
    this._consecutiveTimeouts = 0;
    this._relayHttp = false;
    this.lastConnectError = '';
    this.lastConnectDiagnostics = null;
  }

  configure({ serverUrl, token, relayUrl, relayToken }) {
    if (serverUrl !== undefined) this.serverUrl = serverUrl.replace(/\/$/, '');
    if (token !== undefined) this.token = token;
    if (relayUrl !== undefined) this.relayUrl = relayUrl.replace(/\/$/, '');
    if (relayToken !== undefined) this.relayToken = relayToken;
  }

  // --- Connection ---

  async connect() {
    this.disconnect();

    const canDirect = !!(this.serverUrl && this.token);
    const canRelay = !!(this.relayUrl && this.relayToken);
    const relayHttpUrl = canRelay && /^https?:\/\//i.test(this.relayUrl);
    this._relayHttp = false;
    this.lastConnectError = '';
    this.lastConnectDiagnostics = {
      serverUrl: this.serverUrl || '',
      relayUrl: this.relayUrl || '',
      canDirect,
      canRelay,
      relayHttpUrl: !!relayHttpUrl,
      attempts: [],
    };

    // If both available, skip direct when serverUrl points at relay origin
    // (phone scenario: serverUrl auto-detected to relay host)
    const directOrigin = canDirect && new URL(this.serverUrl).origin;
    const relayOrigin = canRelay && new URL(this.relayUrl).origin;
    const skipDirect = canDirect && canRelay && directOrigin === relayOrigin;

    // Race: try available modes in parallel, prefer direct if both succeed
    const attempts = [];

    if (canDirect && !skipDirect) {
      attempts.push(
        fetch(`${this.serverUrl}/api/contexts`, {
          headers: { 'Authorization': `Bearer ${this.token}` },
          signal: AbortSignal.timeout(4000),
        }).then(r => {
          this.lastConnectDiagnostics?.attempts.push({ kind: 'direct', url: `${this.serverUrl}/api/contexts`, status: r.status });
          if (!r.ok) throw new Error(`direct HTTP ${r.status}`);
          return 'direct';
        }).catch(e => {
          this.lastConnectDiagnostics?.attempts.push({ kind: 'direct', url: `${this.serverUrl}/api/contexts`, error: e?.message || String(e) });
          throw e;
        })
      );
    }

    if (canRelay) {
      if (relayHttpUrl) {
        attempts.push(
          fetch(`${this.relayUrl}/api/system/health`, {
            signal: AbortSignal.timeout(8000),
          }).then(r => {
            this.lastConnectDiagnostics?.attempts.push({ kind: 'relay-http', url: `${this.relayUrl}/api/system/health`, status: r.status });
            if (!r.ok) throw new Error(`relay HTTP ${r.status}`);
            this.serverUrl = this.relayUrl;
            this._relayHttp = true;
            return 'relay';
          }).catch(e => {
            this.lastConnectDiagnostics?.attempts.push({ kind: 'relay-http', url: `${this.relayUrl}/api/system/health`, error: e?.message || String(e) });
            this._relayHttp = false;
            return this._connectRelay().then(() => {
              this.lastConnectDiagnostics?.attempts.push({ kind: 'relay-ws-fallback', url: this._relayWsUrlForDiagnostics(), status: 'open' });
              return 'relay';
            }).catch(wsErr => {
              this.lastConnectDiagnostics?.attempts.push({ kind: 'relay-ws-fallback', url: this._relayWsUrlForDiagnostics(), error: wsErr?.message || String(wsErr) });
              throw wsErr;
            });
          })
        );
      } else {
        attempts.push(
          this._connectRelay().then(() => {
            this.lastConnectDiagnostics?.attempts.push({ kind: 'relay-ws', url: this._relayWsUrlForDiagnostics(), status: 'open' });
            return 'relay';
          }).catch(e => {
            this.lastConnectDiagnostics?.attempts.push({ kind: 'relay-ws', url: this._relayWsUrlForDiagnostics(), error: e?.message || String(e) });
            throw e;
          })
        );
      }
    }

    if (attempts.length === 0) {
      this.mode = 'disconnected';
      return 'disconnected';
    }

    // Use Promise.any — first success wins
    try {
      const mode = await Promise.any(attempts);
      this.mode = mode;
      if (mode === 'direct') {
        // Close relay WS if it also connected
        if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
        this._connectEventStream();
      }
      return mode;
    } catch (e) {
      const failures = (this.lastConnectDiagnostics?.attempts || []).filter(a => a.error || (a.status && Number(a.status) >= 400));
      this.lastConnectError = failures.map(a => `${a.kind}: ${a.error || `HTTP ${a.status}`}`).join('; ') || e?.message || String(e);
      console.warn('All connect attempts failed:', e, this.lastConnectDiagnostics);
      this.mode = 'disconnected';
      return 'disconnected';
    }
  }

  disconnect() {
    this.mode = 'disconnected';
    this._relayHttp = false;
    clearTimeout(this._reconnectTimer);
    clearInterval(this._pingTimer);
    this._pingTimer = null;
    if (this._eventWs) { try { this._eventWs.close(); } catch {} this._eventWs = null; }
    if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
    this._requestMap.forEach(({ reject }) => reject(new Error('disconnected')));
    this._requestMap.clear();
  }

  // --- Request dispatch (with retry + cache) ---

  // Only cache lightweight list endpoints, not file content
  _isCacheable(path) {
    return !path.includes('/skillm') && !path.includes('/files/read') && !path.includes('/workspace/files') && !path.includes('/upload') && !path.includes('/output') && !path.includes('/fulloutput') && !path.includes('/logs');
  }

  _pruneCache() {
    // Evict expired entries, keep max 50
    const PREFIX = 'cam_cache:';
    const keys = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(PREFIX)) keys.push(k);
    }
    // Remove expired (>5min)
    const now = Date.now();
    for (const k of keys) {
      try {
        const { ts } = JSON.parse(localStorage.getItem(k));
        if (now - ts > 300_000) localStorage.removeItem(k);
      } catch { localStorage.removeItem(k); }
    }
    // If still over limit, remove oldest
    const remaining = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(PREFIX)) {
        try {
          const { ts } = JSON.parse(localStorage.getItem(k));
          remaining.push({ k, ts });
        } catch { localStorage.removeItem(k); }
      }
    }
    if (remaining.length > 50) {
      remaining.sort((a, b) => a.ts - b.ts);
      for (const { k } of remaining.slice(0, remaining.length - 50)) {
        localStorage.removeItem(k);
      }
    }
  }

  async request(method, path, body = null) {
    const isGet = method === 'GET';
    const cacheKey = (isGet && this._isCacheable(path)) ? `cam_cache:${path}` : null;

    const isRealtimeGet = isGet && (
      path.includes('/output') ||
      path.includes('/fulloutput') ||
      path.includes('/logs') ||
      path.includes('/api/ws')
    );
    // Retry lightweight GETs, but never retry live output/log polling. In Relay
    // mode each retry can consume another socket timeout and make the selected
    // agent pane look frozen even though the next poll could succeed.
    const maxRetries = (isGet && !isRealtimeGet) ? 2 : 0;
    let lastError;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        let data;
        if (this.mode === 'direct') data = await this._directRequest(method, path, body);
        else if (this.mode === 'relay') data = await this._relayRequest(method, path, body);
        else throw new Error('Not connected');

        // Cache successful GET responses (lightweight endpoints only)
        if (cacheKey) {
          try {
            this._pruneCache();
            localStorage.setItem(cacheKey, JSON.stringify({ data, ts: Date.now() }));
          } catch {}
        }
        return data;
      } catch (e) {
        lastError = e;
        // Don't retry on 4xx (client errors) or mutations
        if (e.status && e.status >= 400 && e.status < 500) throw e;
        if (attempt < maxRetries) {
          await new Promise(r => setTimeout(r, 1000 * (attempt + 1)));
        }
      }
    }

    // All retries failed — try localStorage cache for GET requests
    if (cacheKey) {
      try {
        const cached = localStorage.getItem(cacheKey);
        if (cached) {
          const { data, ts } = JSON.parse(cached);
          if (Date.now() - ts < 300_000) {
            return Object.assign(Object.create(null), data, { _cached: true, _cachedAt: ts });
          }
        }
      } catch {}
    }

    throw lastError;
  }

  async _directRequest(method, path, body) {
    const url = `${this.serverUrl}${path}`;
    const headers = { 'Content-Type': 'application/json' };
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

    const resp = await fetch(url, {
      method,
      headers,
      body: body != null ? JSON.stringify(body) : undefined,
    });

    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch { data = text; }

    if (!resp.ok) {
      const err = new Error(data?.detail || `HTTP ${resp.status}`);
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  _relayRequest(method, path, body) {
    if (this._relayHttp && this.serverUrl) {
      return this._directRequest(method, path, body);
    }
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('Relay not connected'));
        return;
      }
      const id = `req-${++this._reqCounter}`;
      const isSlowMutation = path.includes('/upload') || path.endsWith('/input') || path.endsWith('/key');
      // Relay/source side allows input/key/upload to take longer because
      // SSH/tmux writes can be delayed by remote load while still succeeding.
      // Read-side output polling should not have a tiny hard deadline: public
      // relay links can legitimately take >8s. Keep it bounded, but disable
      // request-level retries for realtime GETs in request() so one slow poll
      // costs at most this timeout, not timeout * 3.
      const timeout = isSlowMutation ? 120000 : 30000;
      const timer = setTimeout(() => {
        this._requestMap.delete(id);
        this._consecutiveTimeouts++;
        if (!isSlowMutation || this._consecutiveTimeouts >= 2) {
          console.warn('Relay: request timeout, forcing reconnect');
          this._consecutiveTimeouts = 0;
          if (this.ws) { try { this.ws.close(); } catch {} }
        }
        reject(new Error('Relay request timeout'));
      }, timeout);

      this._requestMap.set(id, { resolve, reject, timer });

      const frame = {
        id,
        method,
        path,
        headers: {},
        body: body != null ? JSON.stringify(body) : '',
      };
      if (this.token) frame.headers['authorization'] = `Bearer ${this.token}`;
      this.ws.send(JSON.stringify(frame));
    });
  }

  // --- Relay WebSocket ---

  _relayWsUrlForDiagnostics() {
    return `${this.relayUrl.replace(/^http/, 'ws')}/client?token=<redacted>`;
  }

  _connectRelay() {
    return new Promise((resolve, reject) => {
      const url = `${this.relayUrl.replace(/^http/, 'ws')}/client?token=${encodeURIComponent(this.relayToken)}`;
      const ws = new WebSocket(url);
      let opened = false;

      // Timeout: reject if WS doesn't open within 8s
      const connectTimer = setTimeout(() => {
        if (!opened) {
          try { ws.close(); } catch {}
          reject(new Error('Relay connect timeout'));
        }
      }, 8000);

      ws.onopen = () => {
        opened = true;
        clearTimeout(connectTimer);
        this.ws = ws;
        this._consecutiveTimeouts = 0;
        // Heartbeat: ping every 25s to detect dead connections
        clearInterval(this._pingTimer);
        this._pingTimer = setInterval(() => {
          if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            try { this.ws.send(JSON.stringify({ ping: true })); } catch {}
          }
        }, 25000);
        resolve();
      };

      ws.onmessage = (evt) => {
        let data;
        try { data = JSON.parse(evt.data); } catch { return; }

        // Response to a pending request
        if (data.id && this._requestMap.has(data.id)) {
          const { resolve: res, reject: rej, timer } = this._requestMap.get(data.id);
          clearTimeout(timer);
          this._requestMap.delete(data.id);
          this._consecutiveTimeouts = 0; // Got a response — connection is alive

          if (data.status !== undefined) {
            // HTTP response from relay
            let body;
            try { body = JSON.parse(data.body); } catch { body = data.body; }
            if (data.status >= 400) {
              const err = new Error(body?.detail || `HTTP ${data.status}`);
              err.status = data.status;
              rej(err);
            } else {
              res(body);
            }
          } else if (data.event) {
            // WS event stream through relay
            this._dispatchEvent(data.event);
          }
          return;
        }

        // Unsolicited event (broadcast)
        if (data.event) {
          this._dispatchEvent(data.event);
        } else if (data.type) {
          this._dispatchEvent(data);
        }
      };

      ws.onerror = () => { if (!opened) reject(new Error('Relay connect failed')); };
      ws.onclose = () => {
        clearInterval(this._pingTimer);
        this._pingTimer = null;
        // Reject all pending requests — their responses will never arrive
        this._requestMap.forEach(({ reject: rej, timer }) => {
          clearTimeout(timer);
          rej(new Error('Relay connection lost'));
        });
        this._requestMap.clear();

        if (this.mode === 'relay') {
          this._scheduleReconnect();
        }
      };
    });
  }

  _scheduleReconnect() {
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(async () => {
      try {
        await this._connectRelay();
        this.mode = 'relay';
        this._requestRelayEventStream();
      } catch {
        this._scheduleReconnect();
      }
    }, 1000);
  }

  // --- Event stream ---

  onEvent(handler) {
    this._eventHandlers.push(handler);
    return () => {
      this._eventHandlers = this._eventHandlers.filter(h => h !== handler);
    };
  }

  _dispatchEvent(event) {
    for (const h of this._eventHandlers) {
      try { h(event); } catch {}
    }
  }

  _connectEventStream() {
    if (this.mode !== 'direct') return;
    if (this._eventWs) { try { this._eventWs.close(); } catch {} this._eventWs = null; }

    // Don't open WS if page is hidden (mobile background)
    if (document.hidden) return;

    // Back off after repeated failures (max 3 attempts, then stop)
    if (this._wsFailCount >= 3) return;

    const wsUrl = `${this.serverUrl.replace(/^http/, 'ws')}/api/ws?token=${encodeURIComponent(this.token)}`;
    const ws = new WebSocket(wsUrl);
    const openedAt = Date.now();

    ws.onopen = () => {
      // Reset fail count on successful long-lived connection (>30s)
      this._wsStableTimer = setTimeout(() => { this._wsFailCount = 0; }, 30000);
    };

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        this._dispatchEvent(data);
      } catch {}
    };

    ws.onclose = () => {
      this._eventWs = null;
      clearTimeout(this._wsStableTimer);

      // If connection died within 15s, count as failure
      if (Date.now() - openedAt < 15000) {
        this._wsFailCount = (this._wsFailCount || 0) + 1;
      }

      if (this._wsFailCount >= 3) {
        console.warn('WebSocket unstable, falling back to HTTP polling');
        return;
      }

      if (this.mode === 'direct' && !document.hidden) {
        const delay = Math.min(10000 * Math.pow(2, this._wsFailCount || 0), 60000);
        setTimeout(() => this._connectEventStream(), delay);
      }
    };

    this._eventWs = ws;

    // Reconnect on visibility change (mobile foreground/background)
    if (!this._visHandler) {
      this._visHandler = () => {
        if (document.hidden) return;
        if (this.mode === 'direct' && !this._eventWs) {
          this._wsFailCount = 0; // Reset on foreground — user is active
          this._connectEventStream();
        }
      };
      document.addEventListener('visibilitychange', this._visHandler);
    }
  }

  _requestRelayEventStream() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({
      id: 'ws-events',
      method: 'WS',
      path: '/api/ws',
    }));
  }

  // --- Convenience methods ---

  health() { return this.request('GET', '/api/system/health'); }
  relayStatus() { return this.request('GET', '/_relay/status'); }
  config() { return this.request('GET', '/api/system/config'); }
  listAgents(params = {}) {
    const qs = new URLSearchParams();
    if (params.status) qs.set('status', params.status);
    if (params.tool) qs.set('tool', params.tool);
    if (params.context) qs.set('context', params.context);
    if (params.limit) qs.set('limit', params.limit);
    const q = qs.toString();
    return this.request('GET', `/api/agents${q ? '?' + q : ''}`);
  }
  getAgent(id) { return this.request('GET', `/api/agents/${id}`); }
  startAgent(body) { return this.request('POST', '/api/agents', body); }
  stopAgent(id, force = false) { return this.request('DELETE', `/api/agents/${id}?force=${force}`); }
  updateAgent(id, body) { return this.request('PATCH', `/api/agents/${id}`, body); }
  agentCronJobs(id) { return this.request('GET', `/api/agents/${id}/cron`); }
  createAgentCronJob(id, body) { return this.request('POST', `/api/agents/${id}/cron`, body); }
  deleteAgentCronJob(id, jobKey) { return this.request('DELETE', `/api/agents/${id}/cron/${encodeURIComponent(jobKey)}`); }
  restartAgent(id) { return this.request('POST', `/api/agents/${id}/restart`); }
  deleteAgentHistory(id) { return this.request('DELETE', `/api/agents/${id}/history`); }
  agentLogs(id, tail = 100) { return this.request('GET', `/api/agents/${id}/logs?tail=${tail}`); }
  agentOutput(id, lines = 80, hash = null, format = null) {
    const fmt = format ? `&format=${encodeURIComponent(format)}` : '';
    return this.request('GET', `/api/agents/${id}/output?lines=${lines}${hash ? '&hash=' + hash : ''}${fmt}`);
  }
  agentFullOutput(id, offset = 0, format = null) {
    const fmt = format ? `&format=${encodeURIComponent(format)}` : '';
    return this.request('GET', `/api/agents/${id}/fulloutput?offset=${offset}${fmt}`);
  }
  sendInput(id, text, sendEnter = true) { return this.request('POST', `/api/agents/${id}/input`, { text, send_enter: sendEnter }); }
  sendKey(id, key) { return this.request('POST', `/api/agents/${id}/key`, { key }); }
  uploadFile(id, filename, base64data) { return this.request('POST', `/api/agents/${id}/upload`, { filename, data: base64data }); }
  listContexts() { return this.request('GET', '/api/contexts'); }
  getContext(nameOrId) { return this.request('GET', `/api/contexts/${nameOrId}`); }
  createContext(body) { return this.request('POST', '/api/contexts', body); }
  updateContext(nameOrId, body) { return this.request('PUT', `/api/contexts/${nameOrId}`, body); }
  deleteContext(nameOrId) { return this.request('DELETE', `/api/contexts/${nameOrId}`); }
  copyContext(nameOrId, newName) { return this.request('POST', `/api/contexts/${nameOrId}/copy`, { name: newName }); }
  syncContext(nameOrId) { return this.request('POST', `/api/contexts/${nameOrId}/sync`); }
  listFiles(contextId, path = '') { return this.request('GET', `/api/contexts/${contextId}/files?path=${encodeURIComponent(path)}`); }
  readFile(contextId, path) { return this.request('GET', `/api/contexts/${contextId}/files/read?path=${encodeURIComponent(path)}`); }

  /* Workspace Browser (CAM-DESK-FILE-010..017): agent-scoped reads so
   * Desktop's Browse mode resolves through the agent's recorded
   * working directory rather than a separate context lookup. The
   * mobile File Browser keeps using the `listFiles` / `readFile`
   * context-scoped path above; both routes share helpers server-side. */
  agentListWorkspaceFiles(agentId, path = '') {
    return this.request('GET', `/api/agents/${encodeURIComponent(agentId)}/workspace/files?path=${encodeURIComponent(path)}`);
  }
  agentReadWorkspaceFile(agentId, path) {
    return this.request('GET', `/api/agents/${encodeURIComponent(agentId)}/workspace/files/read?path=${encodeURIComponent(path)}`);
  }
  agentWriteWorkspaceFile(agentId, path, content) {
    return this.request('POST', `/api/agents/${encodeURIComponent(agentId)}/workspace/files/write`, { path, content });
  }

  // CAM-DESK-DIRECT-017: Desktop's embedded Hub exposes a read-only
  // suggestion list parsed from the user's ~/.ssh/config. Returns
  // `{ available, source, hosts:[{alias,host,user,port,identity_file}], note }`.
  // Key file values are paths only; key contents are never returned.
  // Hubs that do not implement this (e.g. external CAM server, relay)
  // will 404; the renderer treats that as "import unavailable".
  sshConfigHosts() { return this.request('GET', '/api/system/ssh-config'); }

  // Skillm library management (CAM-DESK-SKILLM-010..014).
  skillmStatus(contextName) {
    return this.request('GET', `/api/skillm/status?context=${encodeURIComponent(contextName || '')}`);
  }
  skillmRepos(contextName) {
    return this.request('GET', `/api/skillm/repos?context=${encodeURIComponent(contextName || '')}`);
  }
  skillmList(contextName, opts = {}) {
    const qs = new URLSearchParams({ context: contextName || '' });
    if (opts.repoName) qs.set('repo', opts.repoName);
    if (opts.sync) qs.set('sync', '1');
    return this.request('GET', `/api/skillm/list?${qs.toString()}`);
  }
  skillmRepoAdd(body) { return this.request('POST', '/api/skillm/repos', body); }
  skillmRepoUpdate(body) { return this.request('PATCH', '/api/skillm/repos', body); }
  skillmRepoRemove(body) { return this.request('DELETE', '/api/skillm/repos', body); }
  skillmRepoRefresh(body) { return this.request('POST', '/api/skillm/repos/refresh', body); }
  skillmRepoConnect(body) { return this.request('POST', '/api/skillm/repo-connect', body); }
  skillmSync(body) { return this.request('POST', '/api/skillm/sync', body); }
  skillmInstall(body) { return this.request('POST', '/api/skillm/install', body); }

}

export const api = new CamApi();
