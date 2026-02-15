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
          signal: AbortSignal.timeout(3000),
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

  // --- Request dispatch ---

  async request(method, path, body = null) {
    if (this.mode === 'direct') return this._directRequest(method, path, body);
    if (this.mode === 'relay') return this._relayRequest(method, path, body);
    throw new Error('Not connected');
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
      const timer = setTimeout(() => {
        this._requestMap.delete(id);
        reject(new Error('Relay request timeout'));
      }, 15000);

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

    const wsUrl = `${this.serverUrl.replace(/^http/, 'ws')}/api/ws?token=${encodeURIComponent(this.token)}`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        this._dispatchEvent(data);
      } catch {}
    };

    ws.onclose = () => {
      this._eventWs = null;
      if (this.mode === 'direct' && !document.hidden) {
        setTimeout(() => this._connectEventStream(), 10000);
      }
    };

    this._eventWs = ws;

    // Reconnect on visibility change (mobile foreground/background)
    if (!this._visHandler) {
      this._visHandler = () => {
        if (!document.hidden && this.mode === 'direct' && !this._eventWs) {
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
  deleteAgentHistory(id) { return this.request('DELETE', `/api/agents/${id}/history`); }
  agentLogs(id, tail = 100) { return this.request('GET', `/api/agents/${id}/logs?tail=${tail}`); }
  agentOutput(id, lines = 80) { return this.request('GET', `/api/agents/${id}/output?lines=${lines}`); }
  agentFullOutput(id, offset = 0) { return this.request('GET', `/api/agents/${id}/fulloutput?offset=${offset}`); }
  sendInput(id, text, sendEnter = true) { return this.request('POST', `/api/agents/${id}/input`, { text, send_enter: sendEnter }); }
  listContexts() { return this.request('GET', '/api/contexts'); }
  getContext(nameOrId) { return this.request('GET', `/api/contexts/${nameOrId}`); }
  createContext(body) { return this.request('POST', '/api/contexts', body); }
  updateContext(nameOrId, body) { return this.request('PUT', `/api/contexts/${nameOrId}`, body); }
  deleteContext(nameOrId) { return this.request('DELETE', `/api/contexts/${nameOrId}`); }
}

export const api = new CamApi();
