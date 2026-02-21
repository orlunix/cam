/**
 * CAM API Client — supports direct HTTP and relay (REST-over-WS) modes.
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

    // Try direct first — require both serverUrl and token
    if (this.serverUrl && this.token) {
      try {
        // Use an authenticated endpoint to verify token works
        const r = await fetch(`${this.serverUrl}/api/contexts`, {
          headers: { 'Authorization': `Bearer ${this.token}` },
          signal: AbortSignal.timeout(10000),
        });
        if (r.ok || r.status === 200) {
          this.mode = 'direct';
          this._connectEventStream();
          return 'direct';
        }
        console.warn('Direct connect: server returned', r.status);
      } catch (e) {
        console.warn('Direct connect failed:', e.message);
      }
    }

    // Try relay
    if (this.relayUrl && this.relayToken) {
      try {
        await this._connectRelay();
        this.mode = 'relay';
        return 'relay';
      } catch (e) {
        console.warn('Relay connect failed:', e.message);
      }
    }

    this.mode = 'disconnected';
    return 'disconnected';
  }

  disconnect() {
    this.mode = 'disconnected';
    clearTimeout(this._reconnectTimer);
    if (this._eventWs) { try { this._eventWs.close(); } catch {} this._eventWs = null; }
    if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
    this._requestMap.forEach(({ reject }) => reject(new Error('disconnected')));
    this._requestMap.clear();
  }

  // --- Request dispatch (with retry + cache) ---

  // Only cache lightweight list endpoints, not file content
  _isCacheable(path) {
    return !path.includes('/files/read') && !path.includes('/upload') && !path.includes('/output') && !path.includes('/fulloutput') && !path.includes('/logs');
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

    // Retry up to 2 times for GET requests, no retry for mutations
    const maxRetries = isGet ? 2 : 0;
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
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('Relay not connected'));
        return;
      }
      const id = `req-${++this._reqCounter}`;
      const timeout = path.includes('/upload') ? 60000 : 15000;
      const timer = setTimeout(() => {
        this._requestMap.delete(id);
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

  _connectRelay() {
    return new Promise((resolve, reject) => {
      const url = `${this.relayUrl.replace(/^http/, 'ws')}/client?token=${encodeURIComponent(this.relayToken)}`;
      const ws = new WebSocket(url);
      let opened = false;

      ws.onopen = () => {
        opened = true;
        this.ws = ws;
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
    }, 5000);
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
  renameAgent(id, name) { return this.request('PATCH', `/api/agents/${id}`, { name }); }
  deleteAgentHistory(id) { return this.request('DELETE', `/api/agents/${id}/history`); }
  agentLogs(id, tail = 100) { return this.request('GET', `/api/agents/${id}/logs?tail=${tail}`); }
  agentOutput(id, lines = 80) { return this.request('GET', `/api/agents/${id}/output?lines=${lines}`); }
  agentFullOutput(id, offset = 0) { return this.request('GET', `/api/agents/${id}/fulloutput?offset=${offset}`); }
  sendInput(id, text, sendEnter = true) { return this.request('POST', `/api/agents/${id}/input`, { text, send_enter: sendEnter }); }
  sendKey(id, key) { return this.request('POST', `/api/agents/${id}/key`, { key }); }
  uploadFile(id, filename, base64data) { return this.request('POST', `/api/agents/${id}/upload`, { filename, data: base64data }); }
  listContexts() { return this.request('GET', '/api/contexts'); }
  getContext(nameOrId) { return this.request('GET', `/api/contexts/${nameOrId}`); }
  createContext(body) { return this.request('POST', '/api/contexts', body); }
  updateContext(nameOrId, body) { return this.request('PUT', `/api/contexts/${nameOrId}`, body); }
  deleteContext(nameOrId) { return this.request('DELETE', `/api/contexts/${nameOrId}`); }
  copyContext(nameOrId, newName) { return this.request('POST', `/api/contexts/${nameOrId}/copy`, { name: newName }); }
  listFiles(contextId, path = '') { return this.request('GET', `/api/contexts/${contextId}/files?path=${encodeURIComponent(path)}`); }
  readFile(contextId, path) { return this.request('GET', `/api/contexts/${contextId}/files/read?path=${encodeURIComponent(path)}`); }
}

export const api = new CamApi();
