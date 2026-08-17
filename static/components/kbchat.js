/* kbchat.js — reusable knowledge-base chat module.
 *
 * Mirrors the /knowledge page chat: conversation history (sessions), live
 * "thinking" streaming via /api/chat/progress, prompt chips, optional client
 * scope. Pi/OpenRouter backend only (no Ollama, per project policy).
 *
 *   initKbChat({ mount: 'homeChat', scopeClients: [{name}], height: '520px' })
 */
(function () {
  const TOKEN_KEY = 'wk_token';
  const authHeaders = () => {
    const t = localStorage.getItem(TOKEN_KEY);
    return t ? { 'Authorization': 'Bearer ' + t } : {};
  };
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const md = a => (typeof marked !== 'undefined') ? marked.parse(a || '') : esc(a || '');

  function injectStyles() {
    if (document.getElementById('kbc-styles')) return;
    const css = `
    .kbc { display: flex; border: 1px solid var(--c-line, #262626); border-radius: 10px; overflow: hidden; background: var(--c-bg2, #1a1a1a); }
    .kbc-side { width: 210px; flex-shrink: 0; border-right: 1px solid #242424; display: flex; flex-direction: column; background: var(--c-bg3, #181818); }
    .kbc-new { margin: 0.6rem; background: var(--c-accenttint, #1a3a2e); border: 1px solid var(--c-accentline, #2d5a48); color: var(--c-accent, #4ec9b0); border-radius: 6px; padding: 0.45rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; }
    .kbc-new:hover { background: var(--c-accenttint2, #1f4a38); }
    .kbc-sessions { flex: 1; overflow-y: auto; }
    .kbc-srow { display: flex; align-items: center; justify-content: space-between; gap: 0.4rem; padding: 0.45rem 0.7rem; border-left: 2px solid transparent; cursor: pointer; font-size: 0.78rem; color: var(--c-text3, #bbb); }
    .kbc-srow:hover { background: var(--c-bg, #1e1e1e); }
    .kbc-srow.active { border-left-color: var(--c-accent, #4ec9b0); background: var(--c-postint, #0d2a1a); color: var(--c-accent, #4ec9b0); }
    .kbc-stitle { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
    .kbc-sdate { font-size: 0.6rem; color: var(--c-faint2, #444); flex-shrink: 0; }
    .kbc-empty { font-size: 0.74rem; color: var(--c-faint2, #444); font-style: italic; padding: 0.5rem 0.7rem; }
    .kbc-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
    .kbc-scoperow { display: flex; align-items: center; gap: 0.5rem; padding: 0.5rem 0.8rem; border-bottom: 1px solid #242424; }
    .kbc-scoperow select { background: var(--c-ink, #111); border: 1px solid var(--c-line2, #333); color: var(--c-text, #d4d4d4); border-radius: 5px; font-size: 0.78rem; padding: 0.3rem 0.45rem; max-width: 240px; }
    .kbc-scoperow select.kbc-scope { flex: 1; min-width: 0; max-width: none; }
    .kbc-scoperow select.kbc-filter { flex-shrink: 0; }
    .kbc-scopehint { color: var(--c-muted2, #666); font-size: 0.74rem; }
    .kbc-thread { flex: 1; overflow-y: auto; padding: 0.9rem 1rem; display: flex; flex-direction: column; gap: 0.7rem; }
    .kbc-ph { color: var(--c-faint, #555); font-size: 0.85rem; font-style: italic; margin: auto; text-align: center; }
    .kbc-wrap { display: flex; flex-direction: column; gap: 0.3rem; animation: kbcFade 0.2s ease; }
    .kbc-wrap.user { align-items: flex-end; } .kbc-wrap.ai { align-items: flex-start; }
    @keyframes kbcFade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; } }
    .kbc-bubble { max-width: 80%; border-radius: 10px; padding: 0.55rem 0.8rem; font-size: 0.85rem; line-height: 1.55; }
    .kbc-u { background: #243447; color: #dbeafe; } .kbc-a { background: #202020; color: var(--c-text, #d4d4d4); }
    .kbc-think { color: var(--c-accent, #4ec9b0); font-size: 0.8rem; font-style: italic; padding: 0.3rem 0; }
    .kbc-a h1,.kbc-a h2,.kbc-a h3 { color: var(--c-bluehi, #9cdcfe); font-size: 0.9rem; margin: 0.5rem 0 0.25rem; }
    .kbc-a strong { color: var(--c-neutraly, #dcdcaa); } .kbc-a em { color: var(--c-orange, #ce9178); }
    .kbc-a ul,.kbc-a ol { padding-left: 1.4rem; margin: 0.3rem 0; } .kbc-a li { margin-bottom: 0.15rem; }
    .kbc-a p { margin: 0 0 0.4rem; } .kbc-a p:last-child { margin: 0; }
    .kbc-a a { color: var(--c-blue, #569cd6); } .kbc-a code { background: var(--c-ink, #111); padding: 0.1rem 0.3rem; border-radius: 3px; color: var(--c-orange, #ce9178); }
    .kbc-srcs { display: flex; flex-wrap: wrap; gap: 0.3rem; }
    .kbc-pill { font-size: 0.64rem; color: var(--c-blue, #569cd6); border: 1px solid var(--c-blueline, #2a3a4a); border-radius: 9px; padding: 0.05rem 0.45rem; text-decoration: none; }
    .kbc-pill:hover { color: var(--c-text2, #ccc); border-color: var(--c-faint2, #444); }
    .kbc-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; padding: 0.5rem 0.8rem 0; }
    .kbc-chip { background: var(--c-linefaint, #222); border: 1px solid var(--c-line2, #333); color: var(--c-bluehi, #9cdcfe); border-radius: 14px; padding: 0.2rem 0.65rem; font-size: 0.72rem; cursor: pointer; }
    .kbc-chip:hover { border-color: var(--c-accent, #4ec9b0); }
    .kbc-inputrow { display: flex; gap: 0.5rem; padding: 0.6rem 0.8rem 0.8rem; }
    .kbc-inputrow textarea { flex: 1; background: var(--c-ink, #111); border: 1px solid var(--c-line2, #333); border-radius: 6px; color: var(--c-text, #d4d4d4); font-family: inherit; font-size: 0.85rem; padding: 0.5rem 0.7rem; resize: none; max-height: 110px; }
    .kbc-inputrow textarea:focus { outline: none; border-color: var(--c-accent, #4ec9b0); }
    .kbc-send { background: var(--c-accent, #4ec9b0); color: var(--c-ink, #111); font-weight: 600; border: none; border-radius: 6px; padding: 0 1.1rem; font-size: 0.85rem; cursor: pointer; }
    .kbc-send:disabled { opacity: 0.5; cursor: default; }
    @media (max-width: 720px) { .kbc-side { display: none; } }
    `;
    const st = document.createElement('style'); st.id = 'kbc-styles'; st.textContent = css;
    document.head.appendChild(st);
  }

  window.initKbChat = function (opts) {
    opts = opts || {};
    injectStyles();
    const root = typeof opts.mount === 'string' ? document.getElementById(opts.mount) : opts.mount;
    if (!root) return;
    const me = opts.me || null;
    const allClients = (opts.scopeClients || []).filter(c => c && c.name);
    const metaOf = c => { let m = c.metadata; if (typeof m === 'string') { try { m = JSON.parse(m || '{}'); } catch (_) { m = {}; } } return m || {}; };
    const isMine = c => { if (!me) return false; if (c.created_by === me.id) return true; const o = metaOf(c).owner_ids || []; return Array.isArray(o) && o.map(Number).includes(me.id); };
    const isFocus = c => { if (!me) return false; const f = metaOf(c).focus_user_ids || []; return Array.isArray(f) && f.map(Number).includes(me.id); };

    root.innerHTML = `
      <div class="kbc" style="height:${opts.height || '520px'}">
        <aside class="kbc-side">
          <button class="kbc-new">+ New chat</button>
          <div class="kbc-sessions"><div class="kbc-empty">Loading…</div></div>
        </aside>
        <div class="kbc-main">
          <div class="kbc-scoperow">
            <select class="kbc-filter" title="Filter which clients you can scope to">
              <option value="all">All clients</option>
              <option value="mine">My clients</option>
              <option value="focus">★ My focus</option>
            </select>
            <select class="kbc-scope"><option value="">All clients (whole knowledge base)</option></select>
          </div>
          <div class="kbc-thread"><div class="kbc-ph">Ask anything about your clients, contacts, meetings &amp; research.</div></div>
          <div class="kbc-chips"></div>
          <div class="kbc-inputrow">
            <textarea class="kbc-in" rows="1" placeholder="e.g. What did we last discuss with Bosch? Who should I follow up with?"></textarea>
            <button class="kbc-send">Send</button>
          </div>
        </div>
      </div>`;

    const q = sel => root.querySelector(sel);
    const sessionsEl = q('.kbc-sessions'), threadEl = q('.kbc-thread'), chipsEl = q('.kbc-chips');
    const inputEl = q('.kbc-in'), sendEl = q('.kbc-send'), scopeEl = q('.kbc-scope'), filterEl = q('.kbc-filter');
    let sessionId = null, scope = '';

    // Build the client scope dropdown: apply the filter (all | mine | focus)
    // and always list the caller's focus clients first, then alphabetical.
    function renderScopeOptions() {
      const f = filterEl.value;
      let list = allClients.slice();
      if (f === 'mine') list = list.filter(isMine);
      else if (f === 'focus') list = list.filter(isFocus);
      list.sort((a, b) => { const fa = isFocus(a) ? 0 : 1, fb = isFocus(b) ? 0 : 1; return fa - fb || String(a.name).localeCompare(String(b.name)); });
      const cur = scopeEl.value;
      scopeEl.innerHTML = '<option value="">All clients (whole knowledge base)</option>' +
        list.map(c => `<option value="${esc(c.name)}">${isFocus(c) ? '★ ' : ''}${esc(c.name)}</option>`).join('');
      if ([...scopeEl.options].some(o => o.value === cur)) scopeEl.value = cur;
      else { scope = ''; }
    }

    const scrollBottom = () => { threadEl.scrollTop = threadEl.scrollHeight; };
    const clearThread = () => { threadEl.innerHTML = '<div class="kbc-ph">Ask anything about your clients, contacts, meetings &amp; research.</div>'; };

    function chipSet() {
      const who = scope || 'this client';
      return [
        ['📋 Meeting prep', `Prepare a concise meeting briefing for ${who}: recent activity, open topics, and what I should raise.`],
        ['🏢 Business model', `Summarize ${who}'s core business model and main revenue drivers.`],
        ['🎯 Pain points', `What are the likely pain points and priorities at ${who}, and how could we help?`],
        ['✍ Talking points', `Give me 3 talking points for my next conversation with ${who}.`],
        ['❓ Quiz me', `Ask me 3 questions to check how well I know ${who}.`],
      ];
    }
    function renderChips() {
      const set = chipSet();
      chipsEl.innerHTML = set.map((c, i) => `<button class="kbc-chip" data-i="${i}">${esc(c[0])}</button>`).join('');
      chipsEl.querySelectorAll('.kbc-chip').forEach(b => b.addEventListener('click', () => { inputEl.value = set[+b.dataset.i][1]; send(); }));
    }

    function relDate(iso) {
      const diff = (Date.now() - new Date(iso)) / 1000;
      if (diff < 60) return 'now'; if (diff < 3600) return Math.floor(diff / 60) + 'm';
      if (diff < 86400) return Math.floor(diff / 3600) + 'h';
      return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }
    async function loadSessions() {
      try {
        const r = await fetch('/api/chat/sessions', { headers: authHeaders() });
        if (!r.ok) return;
        renderSessions((await r.json()).sessions || []);
      } catch (_) {}
    }
    function renderSessions(sessions) {
      if (!sessions.length) { sessionsEl.innerHTML = '<div class="kbc-empty">No conversations yet</div>'; return; }
      sessionsEl.innerHTML = sessions.map(s =>
        `<div class="kbc-srow${s.id === sessionId ? ' active' : ''}" data-id="${s.id}">
           <span class="kbc-stitle">${esc(s.title)}</span><span class="kbc-sdate">${relDate(s.updated_at)}</span></div>`).join('');
      sessionsEl.querySelectorAll('.kbc-srow').forEach(row => row.addEventListener('click', () => openSession(+row.dataset.id)));
    }
    async function openSession(id) {
      if (id === sessionId) return;
      try {
        const r = await fetch('/api/chat/sessions/' + id, { headers: authHeaders() });
        if (!r.ok) return;
        const s = await r.json();
        sessionId = id; clearThread();
        for (const m of (s.messages || [])) {
          if (m.role === 'user') appendUser(m.content, false);
          else if (m.role === 'ai') appendAI(m.content, m.sources || []);
        }
        scope = s.client_name || '';
        if (scope) { filterEl.value = 'all'; renderScopeOptions(); }
        scopeEl.value = scope; renderChips();
        loadSessions();
      } catch (_) {}
    }

    function appendUser(text, scroll = true) {
      threadEl.querySelector('.kbc-ph')?.remove();
      const w = document.createElement('div'); w.className = 'kbc-wrap user';
      w.innerHTML = `<div class="kbc-bubble kbc-u">${esc(text)}</div>`;
      threadEl.appendChild(w); if (scroll) scrollBottom(); return w;
    }
    function appendThinking() {
      threadEl.querySelector('.kbc-ph')?.remove();
      const w = document.createElement('div'); w.className = 'kbc-wrap ai';
      w.innerHTML = `<div class="kbc-think">Thinking…</div>`;
      threadEl.appendChild(w); scrollBottom(); return w;
    }
    function srcClass(t) {
      if (t === 'meeting') return ''; if (['research', 'osint', 'finding', 'summary'].includes(t)) return '';
      return '';
    }
    function pills(sources) {
      if (!sources || !sources.length) return '';
      const seen = new Set();
      const items = sources.filter(s => { const k = (s.url || s.title || '').trim(); if (!k || seen.has(k)) return false; seen.add(k); return true; })
        .map(s => s.url
          ? `<a class="kbc-pill" href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(s.snippet || s.url)}">↗ ${esc(s.title || s.type || 'source')}</a>`
          : `<span class="kbc-pill" title="${esc(s.snippet || '')}">${esc(s.title || s.type || 'source')}</span>`);
      return `<div class="kbc-srcs">${items.join('')}</div>`;
    }
    function replaceAI(w, answer, sources) { w.innerHTML = `<div class="kbc-bubble kbc-a">${md(answer)}</div>${pills(sources)}`; scrollBottom(); }
    function appendAI(answer, sources) {
      threadEl.querySelector('.kbc-ph')?.remove();
      const w = document.createElement('div'); w.className = 'kbc-wrap ai';
      replaceAI(w, answer, sources); threadEl.appendChild(w); scrollBottom();
    }
    function renderEvents(w, events) {
      const recent = (events || []).slice(-5);
      if (!recent.length) return;
      w.innerHTML = '<div class="kbc-think" style="text-align:left">' + recent.map((e, i) =>
        `<div style="color:${i === recent.length - 1 ? '#4ec9b0' : '#555'}">${i === recent.length - 1 ? '▸' : '✓'} ${esc(e)}</div>`).join('') + '</div>';
      scrollBottom();
    }
    async function poll(chatId, w) {
      for (let i = 0; i < 150; i++) {
        await new Promise(r => setTimeout(r, 1000));
        let d;
        try { const r = await fetch('/api/chat/progress/' + chatId, { headers: authHeaders() }); if (!r.ok) { replaceAI(w, '_(chat run lost — retry)_', []); return; } d = await r.json(); }
        catch (_) { continue; }
        if (d.status === 'done' || d.status === 'failed') { replaceAI(w, d.answer || '_(no answer)_', d.sources || []); return; }
        renderEvents(w, d.events);
      }
      replaceAI(w, '_(chat timed out — retry)_', []);
    }

    async function send() {
      const text = inputEl.value.trim(); if (!text) return;
      inputEl.value = ''; inputEl.style.height = ''; sendEl.disabled = true;
      appendUser(text); const w = appendThinking();
      if (!sessionId) {
        try { const r = await fetch('/api/chat/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ client_name: scope || null }) }); if (r.ok) sessionId = (await r.json()).id; } catch (_) {}
      }
      try {
        const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ message: text, client_name: scope || null, session_id: sessionId || null, backend: 'pi', stream: true }) });
        const d = r.ok ? await r.json() : { answer: `_(Error ${r.status})_`, sources: [] };
        if (d.chat_id) await poll(d.chat_id, w); else replaceAI(w, d.answer || '_(no answer)_', d.sources || []);
      } catch (_) { replaceAI(w, '_(Could not reach server)_', []); }
      finally { sendEl.disabled = false; inputEl.focus(); loadSessions(); }
    }

    sendEl.addEventListener('click', send);
    inputEl.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
    inputEl.addEventListener('input', () => { inputEl.style.height = ''; inputEl.style.height = Math.min(inputEl.scrollHeight, 110) + 'px'; });
    scopeEl.addEventListener('change', () => { scope = scopeEl.value; renderChips(); });
    filterEl.addEventListener('change', () => { renderScopeOptions(); scope = scopeEl.value; renderChips(); });
    q('.kbc-new').addEventListener('click', () => { sessionId = null; scope = ''; scopeEl.value = ''; clearThread(); renderChips(); loadSessions(); inputEl.focus(); });

    renderScopeOptions();
    renderChips();
    loadSessions();
  };
})();
