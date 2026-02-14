/**
 * Simple reactive state store.
 */

class AppState {
  constructor() {
    this._data = {
      agents: [],
      contexts: [],
      selectedAgentId: null,
      connectionMode: 'disconnected', // 'direct' | 'relay' | 'disconnected'
      filters: { status: '', tool: '' },
      toast: null,
    };
    this._listeners = [];
  }

  get(key) {
    return this._data[key];
  }

  set(key, value) {
    this._data[key] = value;
    this._notify();
  }

  update(partial) {
    Object.assign(this._data, partial);
    this._notify();
  }

  subscribe(fn) {
    this._listeners.push(fn);
    return () => {
      this._listeners = this._listeners.filter(f => f !== fn);
    };
  }

  _notify() {
    for (const fn of this._listeners) {
      try { fn(this._data); } catch (e) { console.error('State listener error:', e); }
    }
  }

  // --- Agent helpers ---

  updateAgent(id, patch) {
    const agents = this._data.agents.map(a =>
      a.id === id ? { ...a, ...patch } : a
    );
    this.set('agents', agents);
  }

  removeAgent(id) {
    this.set('agents', this._data.agents.filter(a => a.id !== id));
  }

  toast(message, type = 'info', duration = 3000) {
    this.set('toast', { message, type });
    setTimeout(() => {
      if (this._data.toast?.message === message) this.set('toast', null);
    }, duration);
  }
}

export const state = new AppState();
