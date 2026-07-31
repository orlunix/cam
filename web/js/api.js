/**
 * CAM API Client — supports direct HTTP, relay HTTP proxy, and legacy relay REST-over-WS modes.
 */

// In-flight context-sync idle watchers. Context sync is the one long
// request WITH a live progress channel (the Nodes UI polls
// GET /api/contexts/:id/sync-status every 500ms), so its frontend
// timeout is IDLE-based, not absolute: any observed backend step
// change (including uploading-camc percentage advances) resets the
// timer via syncHeartbeat(); only a sync with NO progress for the
// whole window is given up on. heal/upload/skillm-sync have no
// progress channel, so they keep the absolute 120s slowOp budget —
// an idle timer would kill their healthy-but-silent long ops.
const _syncIdleWatchers = new Set();

export class CamApi {
  // Called by the Nodes sync-status poller on every observed backend
  // progress change; resets the idle timer of every in-flight context
  // sync (syncs are rare and user-driven, so a shared reset is fine).
  syncHeartbeat() {
    for (const w of _syncIdleWatchers) w.reset();
  }

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

  async _probeDirectConnection() {
    const hub = typeof window !== 'undefined' ? window.__camDirectHub : null;
    const loopback = (() => {
      try {
        const h = new URL(this.serverUrl).hostname;
        return h === '127.0.0.1' || h === 'localhost' || h === '::1';
      } catch { return false; }
    })();
    if (loopback && hub && typeof hub.request === 'function') {
      await hub.request('GET', '/api/contexts', null, this.token);
      return 'direct';
    }
    const r = await fetch(`${this.serverUrl}/api/contexts`, {
      headers: { 'Authorization': `Bearer ${this.token}` },
      signal: AbortSignal.timeout(4000),
    });
    if (!r.ok) throw new Error(`direct HTTP ${r.status}`);
    return 'direct';
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
        this._probeDirectConnection().then(mode => {
          this.lastConnectDiagnostics?.attempts.push({ kind: 'direct', url: `${this.serverUrl}/api/contexts`, status: 200 });
          return mode;
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
    return !path.includes('refresh=1') && !path.includes('/skillm') && !path.includes('/files/read') && !path.includes('/workspace/files') && !path.includes('/upload') && !path.includes('/output') && !path.includes('/fulloutput') && !path.includes('/logs');
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
    const loopback = (() => {
      try {
        const h = new URL(this.serverUrl).hostname;
        return h === '127.0.0.1' || h === 'localhost' || h === '::1';
      } catch { return false; }
    })();
    const hub = typeof window !== 'undefined' ? window.__camDirectHub : null;
    if (loopback && hub && typeof hub.request === 'function') {
      return hub.request(method, path, body, this.token);
    }

    const url = `${this.serverUrl}${path}`;
    const headers = { 'Content-Type': 'application/json' };
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

    // Slow ops (heal/uploads) can legitimately outlast a read request —
    // backend budgets there are 30s..120s, so the frontend must not
    // abort them at the read-side 15s. Mirror the relay path's split
    // (120s slow / 15s normal).
    // Context sync is different: it has a live progress channel, so it
    // uses a 45s IDLE timeout (see _syncIdleWatchers above) instead of
    // an absolute one. 45s comfortably covers the backend's own 30s
    // per-op budget — any single stuck op errors out before the idle
    // timer fires, while a progressing sync never gets aborted.
    // `/sync-status` itself stays a 15s read (after "sync" comes "-").
    const isSync = /\/contexts\/[^/]+\/sync(\/|$|\?)/.test(path);
    const slowOp = isSync || /\/(sync|heal|upload)(\/|$|\?)/.test(path);
    const SYNC_IDLE_MS = 45000;
    const reqTimeoutMs = slowOp ? 120000 : 15000;

    let syncController = null;
    let syncWatcher = null;
    let signal;
    if (isSync) {
      syncController = new AbortController();
      syncWatcher = {
        tm: null,
        reset() {
          clearTimeout(this.tm);
          this.tm = setTimeout(() => syncController.abort(), SYNC_IDLE_MS);
        },
      };
      syncWatcher.reset();
      _syncIdleWatchers.add(syncWatcher);
      signal = syncController.signal;
    } else {
      // Never let a request hang forever: an accepted-but-unanswered
      // connection would otherwise spin the loading state indefinitely.
      signal = AbortSignal.timeout(reqTimeoutMs);
    }

    let resp;
    try {
      resp = await fetch(url, {
        method,
        headers,
        body: body != null ? JSON.stringify(body) : undefined,
        signal,
      });
    } catch (e) {
      const aborted = e && (e.name === 'AbortError' || e.name === 'TimeoutError');
      const raw = aborted
        ? (isSync
            ? `sync stalled — no backend progress for ${Math.round(SYNC_IDLE_MS / 1000)}s (${this.serverUrl}). The hub may still finish in the background; check Diagnostics.`
            : `request timed out after ${Math.round(reqTimeoutMs / 1000)}s (${this.serverUrl})`)
        : (e?.message || String(e));
      // Surface frontend aborts in the diag log: the backend op may still
      // complete after the renderer gave up, and without this line the
      // mismatch (UI error vs hub success) is invisible.
      if (aborted) {
        try {
          if (window.CamBridge && typeof window.CamBridge.diagLog === 'function') {
            window.CamBridge.diagLog(`[api] ${method} ${path} ${isSync ? 'idle-aborted (no progress)' : 'aborted'} by frontend after ${Math.round((isSync ? SYNC_IDLE_MS : reqTimeoutMs) / 1000)}s`);
          }
        } catch (_) {}
      }
      if (/failed to fetch|networkerror|network error|load failed/i.test(raw)) {
        const err = new Error(
          `Local Hub unreachable at ${this.serverUrl}. ` +
          'Settings → Direct → Disable, then Enable again. ' +
          `(This is not your SSH host — save only talks to the phone Hub.)`
        );
        err.cause = e;
        throw err;
      }
      const err = new Error(raw);
      err.cause = e;
      throw err;
    } finally {
      if (syncWatcher) {
        clearTimeout(syncWatcher.tm);
        _syncIdleWatchers.delete(syncWatcher);
      }
    }

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
    if (params.refresh) qs.set('refresh', '1');
    const q = qs.toString();
    return this.request('GET', `/api/agents${q ? '?' + q : ''}`);
  }
  _agentEndpointQs(hints, prefix = '?') {
    if (!hints || !hints.machine_host) return '';
    const qs = new URLSearchParams();
    qs.set('machine_host', hints.machine_host);
    if (hints.machine_user) qs.set('machine_user', hints.machine_user);
    if (hints.machine_port != null && hints.machine_port !== '') {
      qs.set('machine_port', String(hints.machine_port));
    }
    const s = qs.toString();
    return s ? `${prefix}${s}` : '';
  }
  getAgent(id, hints = null) {
    return this.request('GET', `/api/agents/${encodeURIComponent(id)}${this._agentEndpointQs(hints)}`);
  }
  startAgent(body) { return this.request('POST', '/api/agents', body); }
  getApiModels(hints = {}) {
    const qs = new URLSearchParams();
    if (hints.context) qs.set('context', hints.context);
    else if (hints.node) qs.set('node', hints.node);
    const q = qs.toString();
    return this.request('GET', `/api/api-models${q ? '?' + q : ''}`);
  }
  stopAgent(id, force = false) { return this.request('DELETE', `/api/agents/${id}?force=${force}`); }
  updateAgent(id, body) { return this.request('PATCH', `/api/agents/${id}`, body); }
  agentCronJobs(id) { return this.request('GET', `/api/agents/${id}/cron`); }
  createAgentCronJob(id, body) { return this.request('POST', `/api/agents/${id}/cron`, body); }
  deleteAgentCronJob(id, jobKey) { return this.request('DELETE', `/api/agents/${id}/cron/${encodeURIComponent(jobKey)}`); }
  restartAgent(id) { return this.request('POST', `/api/agents/${id}/restart`); }
  deleteAgentHistory(id) { return this.request('DELETE', `/api/agents/${id}/history`); }
  agentLogs(id, tail = 100) { return this.request('GET', `/api/agents/${id}/logs?tail=${tail}`); }
  agentOutput(id, lines = 80, hash = null, format = null, hints = null) {
    const qs = new URLSearchParams();
    qs.set('lines', String(lines));
    if (hash) qs.set('hash', hash);
    if (format) qs.set('format', format);
    if (hints && hints.machine_host) {
      qs.set('machine_host', hints.machine_host);
      if (hints.machine_user) qs.set('machine_user', hints.machine_user);
      if (hints.machine_port != null && hints.machine_port !== '') {
        qs.set('machine_port', String(hints.machine_port));
      }
    }
    return this.request('GET', `/api/agents/${encodeURIComponent(id)}/output?${qs.toString()}`);
  }
  agentFullOutput(id, offset = 0, format = null) {
    const fmt = format ? `&format=${encodeURIComponent(format)}` : '';
    return this.request('GET', `/api/agents/${id}/fulloutput?offset=${offset}${fmt}`);
  }
  sendInput(id, text, sendEnter = true, hints = null) {
    const body = { text, send_enter: sendEnter };
    if (hints && hints.machine_host) {
      body.machine_host = hints.machine_host;
      if (hints.machine_user) body.machine_user = hints.machine_user;
      if (hints.machine_port != null && hints.machine_port !== '') body.machine_port = hints.machine_port;
    }
    return this.request('POST', `/api/agents/${encodeURIComponent(id)}/input`, body);
  }
  sendKey(id, key, hints = null) {
    const body = { key };
    if (hints && hints.machine_host) {
      body.machine_host = hints.machine_host;
      if (hints.machine_user) body.machine_user = hints.machine_user;
      if (hints.machine_port != null && hints.machine_port !== '') body.machine_port = hints.machine_port;
    }
    return this.request('POST', `/api/agents/${encodeURIComponent(id)}/key`, body);
  }
  async uploadFile(id, filename, base64data) {
    const path = `/api/agents/${encodeURIComponent(id)}/upload`;
    // Current Hub contract is JSON filename + base64 data. Aliases are ignored
    // by current servers and let compatible older JSON controllers accept it.
    try {
      return await this.request('POST', path, {
        filename,
        data: base64data,
        upload: base64data,
        file: base64data,
      });
    } catch (firstError) {
      // Some older direct controllers only accept a multipart field. Relay
      // transports carry JSON frames, so do not bypass the relay protocol.
      const needsMultipart = /upload field not found|missing (?:upload|file) field/i.test(firstError?.message || '');
      const canUseHttp = !!this.serverUrl && !(this.mode === 'relay' && !this._relayHttp);
      if (!needsMultipart || !canUseHttp || typeof FormData === 'undefined') throw firstError;
      const raw = atob(String(base64data || ''));
      const bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      const postMultipart = async (field) => {
        const form = new FormData();
        form.append(field, new Blob([bytes]), filename);
        form.append('filename', filename);
        const headers = this.token ? { Authorization: `Bearer ${this.token}` } : {};
        const resp = await fetch(`${this.serverUrl}${path}`, { method: 'POST', headers, body: form });
        const text = await resp.text();
        let data;
        try { data = JSON.parse(text); } catch { data = text; }
        if (!resp.ok) {
          const err = new Error(data?.detail || data?.error || `HTTP ${resp.status}`);
          err.status = resp.status;
          throw err;
        }
        return data;
      };
      try { return await postMultipart('upload'); }
      catch (uploadError) {
        if (!/upload field not found|missing (?:upload|file) field/i.test(uploadError?.message || '')) throw uploadError;
        return postMultipart('file');
      }
    }
  }
  listContexts() { return this.request('GET', '/api/contexts'); }
  getContext(nameOrId) { return this.request('GET', `/api/contexts/${nameOrId}`); }
  createContext(body) { return this.request('POST', '/api/contexts', body); }
  updateContext(nameOrId, body) { return this.request('PUT', `/api/contexts/${nameOrId}`, body); }
  deleteContext(nameOrId) { return this.request('DELETE', `/api/contexts/${nameOrId}`); }
  copyContext(nameOrId, newName) { return this.request('POST', `/api/contexts/${nameOrId}/copy`, { name: newName }); }
  syncContext(nameOrId, opts = {}) {
    const m = (opts && opts.machine) || {};
    const body = {};
    if (opts.id) body.id = opts.id;
    if (m.host) body.host = m.host;
    if (m.user) body.user = m.user;
    if (m.port != null && m.port !== '') body.port = m.port;
    return this.request('POST', `/api/contexts/${encodeURIComponent(nameOrId)}/sync`, body);
  }
  // Host-scoped heal operations (Nodes → Heal…): sequential camc
  // commands over the context's SSH connection; one result per op.
  healContext(nameOrId, ops) {
    return this.request('POST', `/api/contexts/${encodeURIComponent(nameOrId)}/heal`, { ops });
  }
  // Live sync step progress (poll while a sync is in flight).
  syncStatus(nameOrId) {
    return this.request('GET', `/api/contexts/${encodeURIComponent(nameOrId)}/sync-status`);
  }
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
