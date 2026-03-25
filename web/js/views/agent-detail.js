import { api, state, navigate } from '../app.js';

export function renderAgentDetail(container, agentId) {
  let outputTimer = null;
  let elapsedTimer = null;
  let outputOffset = 0;
  let useFullOutput = false;
  let isFullscreen = false;
  let autoScroll = true;
  let _scrollByCode = false;
  let _touching = false;
  let _deferredUpdate = null;
  let _directInput = false;
  let _errorCount = 0;
  let _inflightStart = 0;       // timestamp when current output request started
  let _inflightTimer = null;     // interval updating the in-flight toast
  let _inflightAbort = null;     // AbortController for cancelling slow requests
  let _lastResponseMs = 0;       // last successful response time in ms
  // Restore last-seen output so the view renders instantly on re-entry
  const _prevOutput = state.getOutput(agentId);
  let cachedOutput = _prevOutput?.text || '';
  let _outputHash = _prevOutput?.hash || null;
  let agent = (state.get('agents') || []).find(a => a.id === agentId);
  // Auto-switch to full output for completed/terminal agents (live capture returns empty)
  if (agent && ['completed', 'failed', 'killed', 'timeout'].includes(agent.status)) {
    useFullOutput = true;
  }
  let fsOverlay = null;

  // Output font size — pinch-to-zoom on output pane adjusts font size
  const FONT_MIN = 8, FONT_MAX = 24;
  let _fontSize = parseFloat(localStorage.getItem('cam_output_font_size')) || 12;

  function _applyFontSize() {
    const sz = _fontSize.toFixed(1) + 'px';
    document.querySelectorAll('#output-pane, #fs-output-pane').forEach(el => { el.style.fontSize = sz; });
    localStorage.setItem('cam_output_font_size', _fontSize.toFixed(1));
  }

  function _wirePinchZoom(pane) {
    let startDist = 0, startSize = 0;
    pane.addEventListener('touchstart', e => {
      if (e.touches.length === 2) {
        startDist = Math.hypot(e.touches[1].pageX - e.touches[0].pageX, e.touches[1].pageY - e.touches[0].pageY);
        startSize = _fontSize;
      }
    }, { passive: true });
    pane.addEventListener('touchmove', e => {
      if (e.touches.length === 2) {
        e.preventDefault();
        const dist = Math.hypot(e.touches[1].pageX - e.touches[0].pageX, e.touches[1].pageY - e.touches[0].pageY);
        const scale = dist / startDist;
        _fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(startSize * scale * 2) / 2));
        _applyFontSize();
      }
    }, { passive: false });
  }

  // Floating "Copy" button — appears when text is selected inside an output pane.
  // Android WebView's native copy popup doesn't work with immersive fullscreen.
  let _copyCleanups = null;
  function _wireCopyButton(pane) {
    let copyBtn = null;
    const show = (x, y) => {
      if (!copyBtn) {
        copyBtn = document.createElement('button');
        copyBtn.textContent = 'Copy';
        copyBtn.className = 'copy-float-btn';
        document.body.appendChild(copyBtn);
        copyBtn.addEventListener('click', () => {
          const sel = window.getSelection();
          const text = sel ? sel.toString() : '';
          if (text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(text).then(
                () => state.toast('Copied', 'success'),
                () => _fallbackCopy(text),
              );
            } else {
              _fallbackCopy(text);
            }
          }
          sel && sel.removeAllRanges();
          hide();
        });
      }
      copyBtn.style.top = Math.max(8, y - 44) + 'px';
      copyBtn.style.left = Math.min(window.innerWidth - 70, Math.max(8, x - 28)) + 'px';
      copyBtn.classList.remove('hidden');
    };
    const hide = () => { if (copyBtn) copyBtn.classList.add('hidden'); };
    const _fallbackCopy = (text) => {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); state.toast('Copied', 'success'); }
      catch { state.toast('Copy failed', 'error'); }
      document.body.removeChild(ta);
    };
    const check = () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString()) { hide(); return; }
      try {
        if (!pane.contains(sel.anchorNode)) { hide(); return; }
      } catch { hide(); return; }
      const r = sel.getRangeAt(0).getBoundingClientRect();
      show(r.left + r.width / 2, r.top);
    };
    document.addEventListener('selectionchange', check);
    // Flush deferred output update once user clears their selection
    const flushDeferred = () => {
      const sel = window.getSelection();
      if ((!sel || sel.isCollapsed) && _deferredUpdate) {
        const { pane: p, text } = _deferredUpdate;
        _deferredUpdate = null;
        _applyPaneUpdate(p, text);
      }
    };
    document.addEventListener('selectionchange', flushDeferred);
    // Clean up on view teardown
    const origCleanup = _copyCleanups;
    _copyCleanups = () => {
      if (origCleanup) origCleanup();
      document.removeEventListener('selectionchange', check);
      document.removeEventListener('selectionchange', flushDeferred);
      if (copyBtn) { copyBtn.remove(); copyBtn = null; }
    };
  }

  // #content is the parent — we toggle flex layout on it
  const contentEl = document.getElementById('content');
  const appEl = document.getElementById('app');

  // ======= visualViewport: keyboard-aware height =======
  // On Android WebView with immersive fullscreen, adjustResize doesn't work.
  // Use visualViewport API to shrink #app when keyboard opens.
  let _vvCleanup = null;
  function setupVisualViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    const onResize = () => {
      const h = vv.height;
      appEl.style.height = h + 'px';
      if (fsOverlay) fsOverlay.style.height = h + 'px';
      const pane = container.querySelector('#output-pane');
      if (pane && autoScroll) requestAnimationFrame(() => { pane.scrollTop = pane.scrollHeight; });
      const fsPre = fsOverlay ? fsOverlay.querySelector('#fs-output-pane') : null;
      if (fsPre && autoScroll) requestAnimationFrame(() => { fsPre.scrollTop = fsPre.scrollHeight; });
    };
    vv.addEventListener('resize', onResize);
    onResize();
    _vvCleanup = () => {
      vv.removeEventListener('resize', onResize);
      appEl.style.height = '';
    };
  }
  setupVisualViewport();

  function timeSince(dateStr) {
    if (!dateStr) return '';
    const s = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d`;
  }

  function renderMeta() {
    const parts = [agent.tool];
    if (agent.context_name) parts.push(agent.context_name);
    parts.push(agent.status);
    if (agent.started_at) parts.push(timeSince(agent.started_at));
    if (agent.auto_confirm === false) parts.push('manual');
    if (agent.exit_reason) parts.push(agent.exit_reason);
    return parts.join(' \u00b7 ');
  }

  let _fetchActive = false;

  function inputHTML() {
    return `
      <div class="quick-actions">
        <button class="btn-quick" data-input="y">y</button>
        <button class="btn-quick" data-input="n">n</button>
        <button class="btn-quick" data-input="1">1</button>
        <button class="btn-quick" data-key="Enter">\u21b5</button>
        <button class="btn-quick" data-key="Escape">Esc</button>
        <button class="btn-quick" data-key="C-c">^C</button>
        <button class="btn-quick" data-key="BSpace">\u232b</button>
        <button class="btn-quick btn-quick-expand" id="expand-keys">\u00b7\u00b7\u00b7</button>
      </div>
      <div class="quick-actions-extra hidden" id="extra-keys">
        <div class="quick-row">
          <button class="btn-quick" data-input="2">2</button>
          <button class="btn-quick" data-input="3">3</button>
          <button class="btn-quick" data-key="Tab">Tab</button>
          <button class="btn-quick" data-key="BTab">S-Tab</button>
          <button class="btn-quick" data-key="DC">Del</button>
        </div>
        <div class="quick-row">
          <button class="btn-quick" data-key="Left">\u2190</button>
          <button class="btn-quick" data-key="Up">\u2191</button>
          <button class="btn-quick" data-key="Down">\u2193</button>
          <button class="btn-quick" data-key="Right">\u2192</button>
          <button class="btn-quick" data-key="Home">Home</button>
          <button class="btn-quick" data-key="End">End</button>
        </div>
        <div class="quick-row">
          <button class="btn-quick" data-key="PPage">PgUp</button>
          <button class="btn-quick" data-key="NPage">PgDn</button>
          <button class="btn-quick" data-input="/">/</button>
          <button class="btn-quick" data-input="~">~</button>
          <button class="btn-quick" data-input="@">@</button>
          <button class="btn-quick" data-input="*">*</button>
        </div>
        <div class="quick-row">
          <button class="btn-quick" data-input="$">$</button>
          <button class="btn-quick" data-input="{">{</button>
          <button class="btn-quick" data-input="}">}</button>
          <button class="btn-quick" data-input="[">[</button>
          <button class="btn-quick" data-input="]">]</button>
          <button class="btn-quick" data-input="|">|</button>
        </div>
      </div>
      <div class="upload-progress hidden" id="upload-progress">
        <div class="upload-progress-bar"></div>
        <span class="upload-progress-text" id="upload-text">Uploading...</span>
      </div>
      <div class="input-bar-sticky">
        <input type="text" id="input-text" class="input-field" placeholder="Send input..." autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" enterkeyhint="send">
        <button class="btn-direct btn-sm${_directInput ? ' active' : ''}" id="direct-btn" title="Direct input mode">Aa</button>
        <button class="btn-upload btn-sm" id="upload-btn" title="Send image">\u{1F4CE}</button>
        <button class="btn-primary btn-sm" id="send-btn">Send</button>
        <input type="file" id="file-input" accept="image/*" style="display:none">
      </div>`;
  }

  const render = async (freshAgent) => {
    try {
      agent = freshAgent || await api.getAgent(agentId);
    } catch (e) {
      container.innerHTML = '<div class="error-state">Agent not found</div>';
      return;
    }

    const existingPane = container.querySelector('#output-pane');
    if (existingPane && existingPane.textContent !== 'Loading...') {
      cachedOutput = existingPane.textContent;
    }

    const isActive = ['running', 'starting', 'pending'].includes(agent.status);
    const prompt = agent.prompt || '';

    // Active: chat-style layout — output fills space, input anchored at bottom
    // Completed: normal scrolling layout with prompt/logs/delete
    if (isActive) {
      container.innerHTML = `
        <div class="detail-header">
          <button class="back-btn" id="back-btn">&larr;</button>
          <div class="detail-title">
            <h2>${escapeHtml(agent.task_name || agent.id.slice(0, 8))}</h2>
            <span class="badge badge-${agent.status}">${agent.status}</span>
          </div>
          <div class="detail-actions-inline" style="position:relative;">
            <button class="overflow-menu-btn" id="menu-btn">\u22ee</button>
            <div class="overflow-menu hidden" id="overflow-menu">
              <button class="overflow-menu-item ${useFullOutput ? 'active' : ''}" id="toggle-full">Full output</button>
              <button class="overflow-menu-item ${!useFullOutput ? 'active' : ''}" id="toggle-live">Live output</button>
              <button class="overflow-menu-item" id="refresh-output">Refresh</button>
              <button class="overflow-menu-item" id="toggle-fullscreen">Fullscreen</button>
              <button class="overflow-menu-item" id="toggle-wrap">Scroll mode</button>
              <button class="overflow-menu-item" id="reset-zoom">Reset zoom</button>
              <hr>
              <button class="overflow-menu-item danger" id="stop-btn">Stop agent</button>
            </div>
          </div>
        </div>

        <div class="detail-meta-compact" id="meta-line">${renderMeta()}</div>

        <div class="output-section" id="output-section">
          <div class="output-wrap">
            <pre class="output-pane" id="output-pane"></pre>
            <button class="jump-bottom-btn hidden" id="jump-bottom">\u2193 Bottom</button>
          </div>
          <div class="input-section" id="input-section">
            ${inputHTML()}
          </div>
        </div>
      `;
      contentEl.classList.add('agent-detail-active');
    } else {
      container.innerHTML = `
        <div class="detail-header">
          <button class="back-btn" id="back-btn">&larr;</button>
          <div class="detail-title">
            <h2>${escapeHtml(agent.task_name || agent.id.slice(0, 8))}</h2>
            <span class="badge badge-${agent.status}">${agent.status}</span>
          </div>
          <div class="detail-actions-inline" style="position:relative;">
            <button class="overflow-menu-btn" id="menu-btn">\u22ee</button>
            <div class="overflow-menu hidden" id="overflow-menu">
              <button class="overflow-menu-item ${useFullOutput ? 'active' : ''}" id="toggle-full">Full output</button>
              <button class="overflow-menu-item ${!useFullOutput ? 'active' : ''}" id="toggle-live">Live output</button>
              <button class="overflow-menu-item" id="refresh-output">Refresh</button>
              <button class="overflow-menu-item" id="toggle-fullscreen">Fullscreen</button>
              <button class="overflow-menu-item" id="toggle-wrap">Scroll mode</button>
              <button class="overflow-menu-item" id="reset-zoom">Reset zoom</button>
              <hr>
              <button class="overflow-menu-item" id="restart-btn">Restart</button>
              <button class="overflow-menu-item danger" id="delete-btn">Delete agent</button>
            </div>
          </div>
        </div>

        <div class="detail-meta-compact" id="meta-line">${renderMeta()}</div>

        <div class="output-section" id="output-section">
          <div class="output-wrap">
            <pre class="output-pane" id="output-pane"></pre>
            <button class="jump-bottom-btn hidden" id="jump-bottom">\u2193 Bottom</button>
          </div>
        </div>

        ${prompt ? `
        <details class="detail-collapse">
          <summary class="collapse-summary">Prompt</summary>
          <div class="prompt-text">${escapeHtml(prompt)}</div>
        </details>` : ''}

        <details class="detail-collapse" id="logs-section">
          <summary class="collapse-summary" id="logs-summary">Logs</summary>
          <div class="log-entries" id="log-entries">Loading...</div>
        </details>
      `;
      contentEl.classList.remove('agent-detail-active');
    }

    const pane = container.querySelector('#output-pane');
    if (pane && cachedOutput) {
      const shortened = _shortenBoxLines(cachedOutput, pane);
      pane.textContent = shortened;
      cachedOutput = shortened;
      if (autoScroll) pane.scrollTop = pane.scrollHeight;
    }

    wireEvents(isActive);
    _applyFontSize();
    loadOutput();
    if (!isActive) loadLogs();
    startElapsedTimer(isActive);

    // Sync fullscreen overlay if active
    if (isFullscreen) {
      closeFullscreen();
      openFullscreen(isActive);
    }
  };

  // ======= Fullscreen overlay (IM-style) =======
  function openFullscreen(isActive) {
    if (fsOverlay) return;

    const name = escapeHtml(agent.task_name || agent.id.slice(0, 8));
    const overlay = document.createElement('div');
    overlay.id = 'fs-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;z-index:9999;background:#111;display:flex;flex-direction:column;font-family:inherit;color:#e0e0e0;';

    overlay.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid #333;flex-shrink:0;">
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-weight:600;font-size:14px;">${name}</span>
          <span class="badge badge-${agent.status}" style="font-size:10px;">${agent.status}</span>
        </div>
        <div style="display:flex;gap:4px;">
          <button class="btn-sm" id="fs-refresh">\u21bb</button>
          <button class="btn-sm" id="fs-close">\u2715</button>
        </div>
      </div>
      <div style="flex:1;min-height:0;display:flex;flex-direction:column;position:relative;padding:0 4px;">
        <pre id="fs-output-pane" style="flex:1;overflow-y:auto;overflow-x:auto;margin:0;padding:8px;background:#0d1117;color:#c9d1d9;font-family:SFMono-Regular,Consolas,Liberation Mono,Menlo,monospace;font-size:12px;line-height:1.45;white-space:pre-wrap;word-break:break-word;border-radius:4px;-webkit-overflow-scrolling:touch;-webkit-user-select:text;user-select:text;"></pre>
        <button class="jump-bottom-btn hidden" id="fs-jump-bottom" style="position:absolute;bottom:12px;right:16px;">\u2193 Bottom</button>
      </div>
      ${isActive ? `<div style="flex-shrink:0;padding:6px 8px;border-top:1px solid #333;">${inputHTML()}</div>` : ''}
    `;

    document.body.appendChild(overlay);
    fsOverlay = overlay;

    const pre = overlay.querySelector('#fs-output-pane');
    pre.textContent = cachedOutput || 'Loading...';
    _applyFontSize();
    _applyWrap();
    _wirePinchZoom(pre);

    overlay.querySelector('#fs-close').addEventListener('click', closeFullscreen);
    overlay.querySelector('#fs-refresh').addEventListener('click', () => {
      outputOffset = 0; cachedOutput = ''; pre.textContent = ''; loadFsOutput();
    });

    const jumpBtn = overlay.querySelector('#fs-jump-bottom');
    pre.addEventListener('scroll', () => {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
      autoScroll = atBottom;
      jumpBtn.classList.toggle('hidden', atBottom);
    });
    pre.addEventListener('touchstart', () => { _touching = true; }, { passive: true });
    pre.addEventListener('touchend', _onTouchEnd, { passive: true });
    pre.addEventListener('touchcancel', _onTouchEnd, { passive: true });
    jumpBtn.addEventListener('click', () => {
      autoScroll = true; jumpBtn.classList.add('hidden'); pre.scrollTop = pre.scrollHeight;
    });

    if (isActive) wireInputButtons(overlay);
    loadFsOutput();
    if (autoScroll) pre.scrollTop = pre.scrollHeight;
  }

  function closeFullscreen() {
    isFullscreen = false;
    if (fsOverlay) {
      fsOverlay.remove();
      fsOverlay = null;
    }
  }

  async function loadFsOutput() {
    const pre = fsOverlay ? fsOverlay.querySelector('#fs-output-pane') : null;
    if (!pre) return;

    try {
      if (useFullOutput) {
        const data = await api.agentFullOutput(agentId, outputOffset);
        if (data.output) {
          updatePane(pre, data.output);
          outputOffset = data.next_offset || outputOffset;
        }
      } else {
        const data = await api.agentOutput(agentId, 200, _outputHash);
        if (data.hash) _outputHash = data.hash;
        if (!data.unchanged && data.output) {
          updatePane(pre, data.output);
        }
      }
      _errorCount = 0;
    } catch {
      _errorCount++;
      if (_errorCount >= 3) _outputHash = null;
    }
  }

  function wireInputButtons(root) {
    const inputText = root.querySelector('#input-text');
    const sendBtn = root.querySelector('#send-btn');
    if (sendBtn && inputText) {
      let composing = false;
      let sending = false;
      inputText.addEventListener('compositionstart', () => { composing = true; });
      inputText.addEventListener('compositionend', () => { composing = false; });
      const doSend = async () => {
        const text = inputText.value;
        if (!text || sending) return;
        // Optimistic: clear input and disable button immediately
        inputText.value = '';
        sending = true;
        sendBtn.disabled = true;
        sendBtn.textContent = '...';
        const preview = text.length > 20 ? text.slice(0, 20) + '\u2026' : text;
        try { await _tracked(`Sending "${preview}"`, () => api.sendInput(agentId, text), true); }
        catch (e) {
          // Restore text on failure so user can retry
          inputText.value = text;
          state.toast(e.message, 'error');
        }
        sending = false;
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send';
      };
      sendBtn.addEventListener('click', () => setTimeout(doSend, 50));
      inputText.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !composing) { e.preventDefault(); setTimeout(doSend, 50); }
      });
      // Direct input mode: send each new character immediately without Enter
      let _prevDirectVal = '';
      inputText.addEventListener('input', (e) => {
        if (!_directInput || composing) return;
        const cur = inputText.value;
        // Find the newly typed portion
        const added = cur.startsWith(_prevDirectVal) ? cur.slice(_prevDirectVal.length) : cur;
        _prevDirectVal = cur;
        if (!added) return;
        api.sendInput(agentId, added, false).catch(err => state.toast(err.message, 'error'));
      });
    }

    // Direct input toggle
    const directBtn = root.querySelector('#direct-btn');
    if (directBtn && inputText) {
      directBtn.addEventListener('click', () => {
        _directInput = !_directInput;
        directBtn.classList.toggle('active', _directInput);
        if (_directInput) {
          _prevDirectVal = inputText.value;
          inputText.placeholder = 'Direct mode: keys sent instantly';
        } else {
          inputText.value = '';
          _prevDirectVal = '';
          inputText.placeholder = 'Send input...';
        }
        inputText.focus();
      });
    }

    const expandBtn = root.querySelector('#expand-keys');
    const extraKeys = root.querySelector('#extra-keys');
    if (expandBtn && extraKeys) {
      expandBtn.addEventListener('click', () => {
        const show = extraKeys.classList.toggle('hidden');
        expandBtn.textContent = show ? '\u00b7\u00b7\u00b7' : '\u00d7';
      });
    }

    // Quick-action buttons: fire-and-forget with brief disable to prevent double-tap
    const _keyLabels = { 'C-c': '^C', 'Escape': 'Esc', 'Enter': 'Enter', 'BSpace': 'Backspace', 'Tab': 'Tab', 'BTab': 'Shift-Tab', 'DC': 'Del', 'Left': '\u2190', 'Right': '\u2192', 'Up': '\u2191', 'Down': '\u2193', 'PPage': 'PgUp', 'NPage': 'PgDn', 'Home': 'Home', 'End': 'End' };
    function quickSend(btn, label, fn) {
      btn.addEventListener('click', () => {
        if (btn.disabled) return;
        btn.disabled = true;
        btn.style.opacity = '0.4';
        _tracked(label, fn, true).catch(e => state.toast(e.message, 'error'))
          .finally(() => { btn.disabled = false; btn.style.opacity = ''; });
      });
    }
    root.querySelectorAll('.btn-quick[data-input]').forEach(btn => {
      const ch = btn.dataset.input;
      quickSend(btn, `Sending "${ch}"`, () => api.sendInput(agentId, ch, false));
    });
    root.querySelectorAll('.btn-quick[data-key]').forEach(btn => {
      const key = btn.dataset.key;
      const label = `Sending ${_keyLabels[key] || key}`;
      quickSend(btn, label, () => api.sendKey(agentId, key));
    });

    // File upload
    const uploadBtn = root.querySelector('#upload-btn');
    const fileInput = root.querySelector('#file-input');
    const progressEl = root.querySelector('#upload-progress');
    const progressText = root.querySelector('#upload-text');
    if (uploadBtn && fileInput) {
      uploadBtn.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', async () => {
        const file = fileInput.files[0];
        if (!file) return;
        fileInput.value = '';

        // Show progress
        if (progressEl) { progressEl.classList.remove('hidden'); }
        if (progressText) { progressText.textContent = `Uploading ${file.name}...`; }

        try {
          // Read as base64
          const b64 = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.onerror = () => reject(new Error('Failed to read file'));
            reader.readAsDataURL(file);
          });

          // Upload
          const resp = await api.uploadFile(agentId, file.name, b64);

          // Send path to agent
          if (resp.path) {
            await api.sendInput(agentId, resp.path, false);
          }

          if (progressText) { progressText.textContent = `Sent \u2713`; }
          setTimeout(() => { if (progressEl) progressEl.classList.add('hidden'); }, 1500);
        } catch (e) {
          const msg = e?.message || (typeof e === 'string' ? e : JSON.stringify(e));
          if (progressText) { progressText.textContent = `Failed: ${msg}`; }
          setTimeout(() => { if (progressEl) progressEl.classList.add('hidden'); }, 3000);
        }
      });
    }
  }

  // ======= Normal mode event wiring =======
  function wireEvents(isActive) {
    container.querySelector('#back-btn').addEventListener('click', () => navigate('/'));

    // Overflow menu toggle
    const menuBtn = container.querySelector('#menu-btn');
    const menu = container.querySelector('#overflow-menu');
    if (menuBtn && menu) {
      menuBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        menu.classList.toggle('hidden');
      });
      document.addEventListener('click', (e) => {
        if (!menu.contains(e.target) && e.target !== menuBtn) {
          menu.classList.add('hidden');
        }
      });
    }

    const closeMenu = () => { if (menu) menu.classList.add('hidden'); };

    container.querySelector('#refresh-output').addEventListener('click', () => {
      closeMenu();
      outputOffset = 0;
      cachedOutput = '';
      const pane = container.querySelector('#output-pane');
      if (pane) pane.textContent = '';
      loadOutput();
    });

    container.querySelector('#toggle-full').addEventListener('click', () => {
      closeMenu();
      if (useFullOutput) return;
      useFullOutput = true;
      outputOffset = 0;
      cachedOutput = '';
      autoScroll = true;
      // Stop auto-refresh — full mode is manual-refresh only (allows text selection)
      clearInterval(outputTimer);
      outputTimer = null;
      render();
    });

    container.querySelector('#toggle-live').addEventListener('click', () => {
      closeMenu();
      if (!useFullOutput) return;
      useFullOutput = false;
      cachedOutput = '';
      autoScroll = true;
      // Restart auto-refresh for live mode
      clearInterval(outputTimer);
      if (['running', 'starting', 'pending'].includes(agent?.status)) {
        outputTimer = setInterval(loadOutput, 2000);
      }
      render();
    });

    container.querySelector('#toggle-fullscreen').addEventListener('click', () => {
      closeMenu();
      isFullscreen = true;
      openFullscreen(isActive);
    });

    // Scroll mode: toggle between pre-wrap (wraps lines) and pre (horizontal scroll)
    let _wrapMode = localStorage.getItem('cam_output_wrap') !== 'scroll';
    const _applyWrap = () => {
      document.querySelectorAll('#output-pane, #fs-output-pane').forEach(el => {
        el.style.whiteSpace = _wrapMode ? 'pre-wrap' : 'pre';
        el.style.wordBreak = _wrapMode ? 'break-word' : 'normal';
      });
      const btn = container.querySelector('#toggle-wrap');
      if (btn) btn.textContent = _wrapMode ? 'Scroll mode' : 'Wrap mode';
    };
    container.querySelector('#toggle-wrap')?.addEventListener('click', () => {
      closeMenu();
      _wrapMode = !_wrapMode;
      localStorage.setItem('cam_output_wrap', _wrapMode ? 'wrap' : 'scroll');
      _applyWrap();
    });

    // Reset zoom to default 12px
    container.querySelector('#reset-zoom')?.addEventListener('click', () => {
      closeMenu();
      _fontSize = 12;
      _applyFontSize();
    });

    const pane = container.querySelector('#output-pane');
    if (pane) {
      _applyWrap(); // Apply saved wrap/scroll mode
      _wirePinchZoom(pane);
      _wireCopyButton(pane);
      pane.addEventListener('scroll', () => {
        if (_scrollByCode) return;
        // User manually scrolled — disable auto-scroll, show jump button
        const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
        if (!atBottom) autoScroll = false;
        const jumpBtn = container.querySelector('#jump-bottom');
        if (jumpBtn) jumpBtn.classList.toggle('hidden', atBottom);
      });
      // Track touch state — defer DOM updates while user is dragging
      pane.addEventListener('touchstart', () => { _touching = true; }, { passive: true });
      pane.addEventListener('touchend', _onTouchEnd, { passive: true });
      pane.addEventListener('touchcancel', _onTouchEnd, { passive: true });

    }

    const jumpBtn = container.querySelector('#jump-bottom');
    if (jumpBtn) {
      jumpBtn.addEventListener('click', () => {
        autoScroll = true;
        jumpBtn.classList.add('hidden');
        if (pane) {
          _scrollByCode = true;
          pane.scrollTop = pane.scrollHeight;
          requestAnimationFrame(() => { _scrollByCode = false; });
        }
      });
    }

    const stopBtn = container.querySelector('#stop-btn');
    if (stopBtn) stopBtn.addEventListener('click', async () => {
      closeMenu();
      try { await api.stopAgent(agentId); state.toast('Agent stopped', 'success'); }
      catch (e) { state.toast(e.message, 'error'); }
    });

    const restartBtn = container.querySelector('#restart-btn');
    if (restartBtn) restartBtn.addEventListener('click', async () => {
      closeMenu();
      try {
        const newAgent = await api.restartAgent(agentId);
        state.toast('Agent restarted', 'success');
        const resp = await api.listAgents({ limit: 50 });
        state.set('agents', resp.agents || []);
        navigate(`/agent/${newAgent.id}`);
      } catch (e) { state.toast(e.message, 'error'); }
    });

    const deleteBtn = container.querySelector('#delete-btn');
    if (deleteBtn) deleteBtn.addEventListener('click', async () => {
      closeMenu();
      if (!confirm('Delete this agent from history?')) return;
      try {
        await api.deleteAgentHistory(agentId);
        state.toast('Agent deleted', 'success');
        const resp = await api.listAgents({ limit: 50 });
        state.set('agents', resp.agents || []);
        navigate('/');
      } catch (e) { state.toast(e.message, 'error'); }
    });

    if (isActive) wireInputButtons(container);
  }

  function startElapsedTimer(isActive) {
    clearInterval(elapsedTimer);
    if (!isActive || !agent.started_at) return;
    elapsedTimer = setInterval(() => {
      const el = container.querySelector('#meta-line');
      if (el) el.textContent = renderMeta();
    }, 1000);
  }

  // Shorten box-drawing horizontal lines to fit pane width
  const _boxCharRe = /[─━═╌╍┄┅┈┉╶╴╸╺]+/g;
  let _cachedCharW = 0;
  let _cachedPaneW = 0;
  function _measureCharW(pane) {
    if (!pane) return 7.2;
    const pw = pane.clientWidth;
    if (_cachedCharW && pw === _cachedPaneW) return _cachedCharW;
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font:inherit;';
    probe.textContent = '──────────'; // 10 box chars
    pane.appendChild(probe);
    _cachedCharW = probe.offsetWidth / 10 || 7.2;
    pane.removeChild(probe);
    _cachedPaneW = pw;
    return _cachedCharW;
  }
  // Invalidate cached charW on window resize
  const _onResize = () => { _cachedPaneW = 0; };
  window.addEventListener('resize', _onResize);

  function _shortenBoxLines(text, pane) {
    const charW = _measureCharW(pane);
    const contentW = pane ? (pane.clientWidth - 24) : 300; // subtract left+right padding (12+12)
    const maxCols = Math.floor(contentW / charW);
    if (maxCols >= 100) return text;
    return text.split('\n').map(line => {
      if (line.length <= maxCols) return line;
      let overflow = line.length - maxCols;
      if (overflow <= 0) return line;
      // Shrink box-drawing runs from longest to shortest
      return line.replace(_boxCharRe, (m) => {
        if (overflow <= 0 || m.length < 6) return m;
        const cut = Math.min(m.length - 3, overflow);
        overflow -= cut;
        return m.slice(0, m.length - cut);
      });
    }).join('\n');
  }

  function updatePane(pane, newText) {
    // Only touch DOM if content actually changed — avoids scroll glitch
    const cleaned = newText.replace(/\n([ \t]*\n)*[ \t]*$/, '\n');
    // Cache raw (unshortenened) output so re-entry can re-shorten for current pane width
    state.setOutput(agentId, cleaned, _outputHash);
    const trimmed = _shortenBoxLines(cleaned, pane);
    if (cachedOutput === trimmed) return;
    // Defer DOM update while user is actively touching/scrolling
    if (_touching) {
      _deferredUpdate = { pane, text: trimmed };
      return;
    }
    _applyPaneUpdate(pane, trimmed);
  }

  function _onTouchEnd() {
    _touching = false;
    // Don't flush deferred update if user has an active selection (let them copy first)
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;
    if (_deferredUpdate) {
      const { pane, text } = _deferredUpdate;
      _deferredUpdate = null;
      _applyPaneUpdate(pane, text);
    }
  }

  function _applyPaneUpdate(pane, trimmed) {
    // Don't nuke DOM while user has text selected — defer until selection clears
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) {
      try {
        if (pane.contains(sel.anchorNode)) {
          _deferredUpdate = { pane, text: trimmed };
          return;
        }
      } catch { /* anchorNode may be detached */ }
    }
    _scrollByCode = true;
    pane.textContent = trimmed;
    cachedOutput = trimmed;
    if (autoScroll) {
      pane.scrollTop = pane.scrollHeight;
    }
    requestAnimationFrame(() => { _scrollByCode = false; });
  }

  // --- In-flight status toast ---
  // Tracks any in-flight request (output fetch, key send, text input)
  // and shows a floating toast with live context when it takes > 1s.
  let _inflightLabel = '';  // e.g. "Fetching output", "Sending Esc", "Sending ^C"

  function _viaPath() {
    const mode = state.get('connectionMode') || 'direct';
    const ctx = agent?.context_name || '';
    let via = mode === 'relay' ? 'relay' : 'direct';
    if (ctx) via += ' \u2192 ' + ctx;
    return via;
  }

  function _showInflightToast() {
    let el = document.getElementById('inflight-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'inflight-toast';
      el.className = 'inflight-toast';
      document.body.appendChild(el);
    }
    return el;
  }
  function _removeInflightToast() {
    clearInterval(_inflightTimer);
    _inflightTimer = null;
    const el = document.getElementById('inflight-toast');
    if (el) {
      el.classList.add('inflight-toast-out');
      setTimeout(() => el.remove(), 200);
    }
  }
  let _inflightImmediate = false;  // true = user action in progress (higher priority)
  let _inflightId = 0;             // tracks which action owns the toast
  function _startInflightTracking(label, immediate) {
    // Don't let background fetch overwrite an active user action
    if (!immediate && _inflightImmediate && _inflightStart) return null;
    // If a send preempts, the old tracking is orphaned (its finish will be ignored)
    const id = ++_inflightId;
    _inflightLabel = label || 'Request';
    _inflightStart = Date.now();
    _inflightImmediate = !!immediate;
    clearInterval(_inflightTimer);
    if (immediate) {
      _updateInflightToast();
      _inflightTimer = setInterval(_updateInflightToast, 200);
    } else {
      _inflightTimer = setTimeout(() => {
        _inflightTimer = setInterval(_updateInflightToast, 200);
        _updateInflightToast();
      }, 1000);
    }
    return id;
  }
  function _updateInflightToast() {
    if (!_inflightStart) return;
    const elapsed = ((Date.now() - _inflightStart) / 1000).toFixed(1);
    const via = _viaPath();

    const el = _showInflightToast();
    el.classList.remove('inflight-toast-out', 'inflight-toast-ok', 'inflight-toast-err');
    const slow = parseFloat(elapsed) >= 5.0;
    el.innerHTML = slow
      ? `<span>\u26a0 ${_inflightLabel} via ${via} \u2026 ${elapsed}s</span><button class="inflight-cancel" id="inflight-cancel-btn">Cancel</button>`
      : `<span>\u23f3 ${_inflightLabel} via ${via} \u2026 ${elapsed}s</span>`;
    const cancelBtn = el.querySelector('#inflight-cancel-btn');
    if (cancelBtn) {
      cancelBtn.onclick = () => {
        if (_inflightAbort) _inflightAbort.abort();
        _removeInflightToast();
      };
    }
  }
  function _finishInflightTracking(id, success, doneLabel) {
    // Ignore if a newer action has taken over the toast
    if (id !== _inflightId) return;
    if (!_inflightStart) return;
    const elapsed = Date.now() - _inflightStart;
    _lastResponseMs = elapsed;
    _inflightStart = 0;
    _inflightImmediate = false;
    clearInterval(_inflightTimer);
    _inflightTimer = null;

    // Always show result briefly
    const el = _showInflightToast();
    const via = _viaPath();
    const secs = (elapsed / 1000).toFixed(1);
    const label = doneLabel || _inflightLabel;
    el.innerHTML = success
      ? `<span>\u2713 ${label} \u00b7 ${secs}s via ${via}</span>`
      : `<span>\u2717 ${label} failed \u00b7 ${secs}s</span>`;
    el.classList.remove('inflight-toast-out', 'inflight-toast-ok', 'inflight-toast-err');
    el.classList.toggle('inflight-toast-ok', success);
    el.classList.toggle('inflight-toast-err', !success);
    setTimeout(() => _removeInflightToast(), 1200);
  }

  // Convenience: wrap an async action with inflight tracking
  // immediate=true shows toast instantly (for user actions like send/key)
  async function _tracked(label, fn, immediate = false) {
    const id = _startInflightTracking(label, immediate);
    try {
      const result = await fn();
      if (id != null) _finishInflightTracking(id, true);
      return result;
    } catch (e) {
      if (id != null) _finishInflightTracking(id, false);
      throw e;
    }
  }

  async function loadOutput() {
    if (fsOverlay) {
      loadFsOutput();
      return;
    }
    const pane = container.querySelector('#output-pane');
    if (!pane) return;

    // Skip if a fetch is already in flight
    if (_fetchActive) return;
    _fetchActive = true;

    try {
      await _tracked('Fetching output', async () => {
        if (useFullOutput) {
          const data = await api.agentFullOutput(agentId, outputOffset);
          if (data.output) {
            updatePane(pane, data.output);
            outputOffset = data.next_offset || outputOffset;
          }
        } else {
          const data = await api.agentOutput(agentId, 200, _outputHash);
          if (data.hash) _outputHash = data.hash;
          if (!data.unchanged && data.output) {
            updatePane(pane, data.output);
          }
        }
      });
      _errorCount = 0;
    } catch (e) {
      if (e?.name === 'AbortError') { _fetchActive = false; return; }
      _errorCount++;
      if (_errorCount >= 3) _outputHash = null;
    }
    _fetchActive = false;
  }

  async function loadLogs() {
    const el = container.querySelector('#log-entries');
    const summary = container.querySelector('#logs-summary');
    if (!el) return;
    try {
      const data = await api.agentLogs(agentId, 50);
      const entries = data.entries || [];
      if (summary) summary.textContent = `Logs (${entries.length})`;
      if (entries.length === 0) {
        el.innerHTML = '<div class="empty-state">No logs yet</div>';
        return;
      }
      el.innerHTML = entries.map(e => {
        const ts = e.ts ? new Date(e.ts).toLocaleTimeString() : '';
        const type = e.type || '';
        return `<div class="log-entry log-${type}"><span class="log-ts">${ts}</span> <span class="log-type">[${type}]</span> ${escapeHtml(e.output || e.state || JSON.stringify(e.data || ''))}</div>`;
      }).join('');
    } catch {
      el.innerHTML = '<div class="empty-state">Logs unavailable</div>';
    }
  }

  // Initial render + auto-refresh
  // Pass cached agent from state to avoid blocking on a network fetch —
  // the page layout appears instantly, output loads in background.
  render(agent).then(() => {
    if (['running', 'starting', 'pending'].includes(agent?.status) && !useFullOutput) {
      outputTimer = setInterval(loadOutput, 2000);
    }
  });

  const unsub = state.subscribe((data) => {
    const updated = (data.agents || []).find(a => a.id === agentId);
    if (updated && updated.status !== agent?.status) {
      render(updated);
    }
  });

  return () => {
    clearInterval(outputTimer);
    clearInterval(elapsedTimer);
    _removeInflightToast();
    if (_inflightAbort) _inflightAbort.abort();
    if (_copyCleanups) _copyCleanups();
    contentEl.classList.remove('agent-detail-active');
    closeFullscreen();
    window.removeEventListener('resize', _onResize);
    if (_vvCleanup) _vvCleanup();
    unsub();
  };
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
