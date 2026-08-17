/**
 * Shared mail personalization + Outlook export for Buzzowl.
 *
 * Mail templates are generated with [Name] / [First Name] / [Role] placeholders.
 * This module fetches a client's contacts, renders a per-contact row with
 * Copy + "📧 Outlook" buttons, and personalizes the template per recipient —
 * the same flow the bulk-mail page on /products/{id} uses, reused everywhere
 * a single mail is generated (match page, etc.).
 *
 * Usage:
 *   WKMail.attach(containerEl, clientName, {
 *     subject:      'Acme — WidgetPro',          // Outlook subject line
 *     getTemplate:  () => textarea.value,         // current mail body
 *     getCasual:    () => false,                  // optional: first-name greeting
 *   });
 */
(function () {
  if (!document.getElementById('wkmail-style')) {
    const s = document.createElement('style');
    s.id = 'wkmail-style';
    s.textContent = `
      .wkmail-wrap { margin-top:0.55rem; background:var(--c-bg2, #1a1a1a); border:1px solid var(--c-line, #2a2a2a); border-radius:4px; padding:0.4rem 0.6rem; }
      .wkmail-head { font-size:0.62rem; color:var(--c-faint, #555); text-transform:uppercase; letter-spacing:0.06em; margin-bottom:0.3rem; }
      .wkmail-row { display:flex; align-items:center; gap:0.5rem; padding:0.25rem 0; border-bottom:1px solid var(--c-linefaint, #232323); }
      .wkmail-row:last-child { border-bottom:none; }
      .wkmail-name { font-size:0.75rem; color:var(--c-text2, #ccc); flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wkmail-name .wkmail-role { color:var(--c-muted2, #666); }
      .wkmail-email { color:var(--c-blue, #569cd6); font-size:0.7rem; white-space:nowrap; }
      .wkmail-noemail { color:var(--c-faint2, #444); font-size:0.7rem; font-style:italic; }
      .wkmail-btn { flex-shrink:0; background:none; border:1px solid var(--c-line, #2a2a2a); border-radius:3px; padding:0.1rem 0.45rem; font-size:0.68rem; cursor:pointer; font-family:inherit; }
      .wkmail-btn.wkmail-copy { color:var(--c-blue, #569cd6); }
      .wkmail-btn.wkmail-outlook { color:var(--c-accent, #4ec9b0); }
      .wkmail-btn.wkmail-exported { color:var(--c-accent, #4ec9b0); border-color:#2a5a3a; background:var(--c-accenttint, #13241c); }
      .wkmail-btn:hover { border-color:var(--c-faint, #555); }
      .wkmail-btn:disabled { opacity:0.4; cursor:not-allowed; }
      .wkmail-count { color:var(--c-accent, #4ec9b0); text-transform:none; letter-spacing:0; margin-left:0.4rem; font-weight:600; }
      .wkmail-count.wkmail-count-zero { color:var(--c-faint, #555); }
      .wkmail-empty { font-size:0.72rem; color:var(--c-faint, #555); margin-top:0.45rem; }
      .wkmail-empty a { color:var(--c-blue, #569cd6); text-decoration:none; }
    `;
    document.head.appendChild(s);
  }

  function authHeaders() {
    const t = localStorage.getItem('wk_token');
    return t ? { 'Authorization': 'Bearer ' + t } : {};
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function metaOf(contact) {
    const m = contact.metadata;
    if (typeof m === 'string') { try { return JSON.parse(m || '{}'); } catch (_) { return {}; } }
    return m || {};
  }

  function firstNameOf(contact) {
    const m = metaOf(contact);
    return m.first_name || (contact.name || '').split(' ')[0] || '';
  }

  // ── Outlook export tracking ────────────────────────────────────────────────
  // A personal, per-browser memory of which recipients you've already hit the
  // "📧 Outlook" (or bulk .eml) export for, so a campaign with dozens of people
  // shows at a glance who's done — and you don't email the same person twice or
  // forget someone. Stored in localStorage on purpose: it's a working aid, not
  // evaluation data, and must not depend on a server round-trip while you click
  // through a long list.
  const _EXPORT_LS_KEY = 'wk_outlook_exported';
  const _EXPORT_TTL_MS = 30 * 24 * 60 * 60 * 1000;  // 30 days — after this the mark clears so the client looks contactable again
  function _loadExported() {
    let m;
    try { m = JSON.parse(localStorage.getItem(_EXPORT_LS_KEY) || '{}') || {}; }
    catch (_) { return {}; }
    // Prune marks past the 30-day TTL (the durable record lives in the DB contact log).
    const cutoff = Date.now() - _EXPORT_TTL_MS;
    let changed = false;
    for (const k in m) { const t = new Date(m[k]).getTime(); if (isNaN(t) || t < cutoff) { delete m[k]; changed = true; } }
    if (changed) { try { localStorage.setItem(_EXPORT_LS_KEY, JSON.stringify(m)); } catch (_) {} }
    return m;
  }
  function _saveExported(map) {
    try { localStorage.setItem(_EXPORT_LS_KEY, JSON.stringify(map)); } catch (_) {}
  }
  // Stable per-recipient id: client + email (falls back to name when no email).
  function exportKey(clientName, email, name) {
    const who = String(email || name || '').trim().toLowerCase();
    return String(clientName || '').trim().toLowerCase() + '::' + who;
  }
  function isExported(key) { return !!(key && _loadExported()[key]); }
  function exportedAt(key) { return (key && _loadExported()[key]) || null; }
  function markExported(key) {
    if (!key) return;
    const m = _loadExported(); m[key] = new Date().toISOString(); _saveExported(m);
  }
  function markExportedMany(keys) {
    const m = _loadExported(); const now = new Date().toISOString();
    (keys || []).forEach(k => { if (k) m[k] = now; }); _saveExported(m);
  }
  function clearExported(keys) {
    if (!keys) { _saveExported({}); return; }
    const m = _loadExported(); (keys || []).forEach(k => { delete m[k]; }); _saveExported(m);
  }
  // Short relative-time label ("just now", "5m ago", "2h ago", "3d ago", "12 Jun").
  // Used so an export from a PREVIOUS batch reads as history, not "this draft was sent".
  function relTime(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 45) return 'just now';
    const m = Math.round(s / 60); if (m < 60) return m + 'm ago';
    const h = Math.round(m / 60); if (h < 24) return h + 'h ago';
    const d = Math.round(h / 24); if (d < 7) return d + 'd ago';
    return new Date(t).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  }
  function exportedTitle(iso) {
    return iso ? 'Exported to Outlook ' + new Date(iso).toLocaleString() + ' — click to export again' : '';
  }

  function personalize(template, opts) {
    opts = opts || {};
    const name = opts.name || '';
    const firstName = opts.firstName || name.split(' ')[0] || '';
    const greet = opts.casual ? (firstName || '[First Name]') : (name || '[Name]');
    return (template || '')
      .replace(/\[Name\]/g, greet)
      .replace(/\[First Name\]/g, firstName || '[First Name]')
      .replace(/\[Role\]/g, opts.role || '[Role]');
  }

  function toOutlook(email, subject, body) {
    // The subject lives in its own field — strip any leading Betreff:/Subject: line from the body.
    const cleanBody = (body || '')
      .replace(/^\s*Betreff:[^\n]*\n+/i, '')
      .replace(/^\s*Subject:[^\n]*\n+/i, '');
    window.location.href = `mailto:${encodeURIComponent(email)}`
      + `?subject=${encodeURIComponent(subject || '')}`
      + `&body=${encodeURIComponent(cleanBody)}`;
  }

  async function fetchContacts(clientName) {
    try {
      const r = await fetch('/api/people?client_name=' + encodeURIComponent(clientName), { headers: authHeaders() });
      if (!r.ok) return [];
      const d = await r.json();
      return d.contacts || [];
    } catch (_) { return []; }
  }

  function render(container, clientName, contacts, opts) {
    opts = opts || {};
    const getTemplate = opts.getTemplate || (() => '');
    const getCasual = opts.getCasual || (() => false);
    const subject = opts.subject || clientName || '';

    if (!contacts.length) {
      container.innerHTML = `<div class="wkmail-empty">No contacts on file — `
        + `<a href="/client/${encodeURIComponent(clientName)}" target="_blank" rel="noopener">add on the client page ↗</a> `
        + `to fill in name &amp; email automatically.</div>`;
      return;
    }

    const rows = contacts.map((c, i) => {
      const m = metaOf(c);
      const role = m.role || '';
      const email = m.email || '';
      const emailHtml = email
        ? `<span class="wkmail-email">${esc(email)}</span>`
        : `<span class="wkmail-noemail">no email</span>`;
      const key = exportKey(clientName, email, c.name);
      const at = email ? exportedAt(key) : null;
      const done = !!at;
      return `<div class="wkmail-row">
        <span class="wkmail-name" title="${esc(c.name || '')}">${esc(c.name || '')}${role ? ` <span class="wkmail-role">· ${esc(role)}</span>` : ''}</span>
        ${emailHtml}
        <button class="wkmail-btn wkmail-copy" data-i="${i}">Copy</button>
        <button class="wkmail-btn wkmail-outlook${done ? ' wkmail-exported' : ''}" data-i="${i}" data-key="${esc(key)}"${done ? ` title="${esc(exportedTitle(at))}"` : ''}${email ? '' : ' disabled'}>${done ? '✓ ' + esc(relTime(at)) : '📧 Outlook'}</button>
      </div>`;
    }).join('');
    container.innerHTML = `<div class="wkmail-wrap"><div class="wkmail-head">Contacts — fill in &amp; send<span class="wkmail-count" data-wkmail-count></span></div>${rows}</div>`;
    refreshCount(container, clientName, contacts);

    container.querySelectorAll('.wkmail-copy').forEach(btn => {
      btn.addEventListener('click', () => {
        const c = contacts[+btn.dataset.i];
        const m = metaOf(c);
        const text = personalize(getTemplate(), { name: c.name, role: m.role, firstName: firstNameOf(c), casual: getCasual() });
        navigator.clipboard.writeText(text).then(() => {
          const o = btn.textContent; btn.textContent = 'Copied!';
          setTimeout(() => { btn.textContent = o; }, 1500);
        });
      });
    });
    container.querySelectorAll('.wkmail-outlook').forEach(btn => {
      btn.addEventListener('click', () => {
        const c = contacts[+btn.dataset.i];
        const m = metaOf(c);
        if (!m.email) return;
        const body = personalize(getTemplate(), { name: c.name, role: m.role, firstName: firstNameOf(c), casual: getCasual() });
        toOutlook(m.email, subject, body);
        markExported(btn.dataset.key);
        btn.classList.add('wkmail-exported');
        btn.textContent = '✓ ' + relTime(exportedAt(btn.dataset.key));
        btn.title = exportedTitle(exportedAt(btn.dataset.key));
        refreshCount(container, clientName, contacts);
        // Durable record for the Home "Last contacted" panel (best-effort).
        try {
          fetch('/api/contact-log', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ client_name: clientName, contact_name: c.name || '', contact_email: m.email, subject, body }),
          }).catch(() => {});
        } catch (_) {}
      });
    });
  }

  // Update the "x/y exported" tally in a contact list's header.
  function refreshCount(container, clientName, contacts) {
    const el = container.querySelector('[data-wkmail-count]');
    if (!el) return;
    const withEmail = (contacts || []).filter(c => metaOf(c).email);
    const total = withEmail.length;
    const done = withEmail.filter(c => isExported(exportKey(clientName, metaOf(c).email, c.name))).length;
    el.textContent = total ? `${done}/${total} exported` : '';
    el.classList.toggle('wkmail-count-zero', done === 0);
  }

  async function attach(container, clientName, opts) {
    if (!container) return;
    container.innerHTML = '<div class="wkmail-empty">Loading contacts…</div>';
    const contacts = await fetchContacts(clientName);
    render(container, clientName, contacts, opts);
  }

  // Download all generated emails as a ZIP of .eml drafts (one per contact).
  // items: [{ client_name, subject, body }]. The server personalizes per contact
  // and tags each .eml X-Unsent so Outlook opens it as an editable draft.
  let _emlMacAcked = false;   // only warn Mac users once per session
  async function downloadEmlBundle(items, casual, btn) {
    items = (items || []).filter(it => it && it.client_name && it.body);
    if (!items.length) { alert('Nothing to download yet — generate emails first.'); return; }
    // The .eml "editable draft" trick (X-Unsent + drag into Drafts) is a Windows
    // Outlook feature. On macOS, Outlook/Apple Mail open .eml as read-only messages,
    // so steer Mac users to the per-contact 📧 Outlook (mailto) / Copy buttons.
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
    if (isMac && !_emlMacAcked) {
      const go = confirm(
        '“Save all to Outlook drafts (.eml)” is built for Windows Outlook — you drag the files into ' +
        'Outlook → Drafts and they open as editable drafts.\n\n' +
        'On a Mac, Outlook/Apple Mail usually open .eml files as read-only messages, so you won’t get ' +
        'editable drafts. Instead use the per-contact 📧 Outlook button (opens a compose window) or Copy.\n\n' +
        'Download the .eml files anyway?'
      );
      if (!go) return;
      _emlMacAcked = true;
    }
    const orig = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Building…'; }
    const restore = (txt, ms) => { if (btn) { btn.textContent = txt; setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, ms); } };
    try {
      const r = await fetch('/api/mail/eml-bundle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ items, casual: !!casual }),
      });
      if (!r.ok) {
        let msg = 'Failed';
        try { msg = (await r.json()).detail || msg; } catch (_) {}
        restore(msg, 3000);
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'outlook-drafts.zip';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      restore('Downloaded ✓', 2000);
    } catch (e) {
      restore('Error', 3000);
    }
  }

  // Example custom-instruction snippets, inserted via a dropdown above each
  // mail "custom instructions" textarea so reps have a starting point to fine-tune.
  const MAIL_EXAMPLES = [
    'Keep it under 80 words and very direct.',
    'Open by referencing their most recent news, then tie it to a concrete benefit.',
    'Warm, conversational tone — greet by first name.',
    'Lead with ROI and cost savings.',
    'Make the call-to-action a 15-minute intro call.',
    'Mention we already work with similar companies in their industry.',
    'Acknowledge a likely pain point and position us as the fix.',
  ];

  function attachExamples(textareaId) {
    const ta = document.getElementById(textareaId);
    if (!ta || ta.dataset.wkExamples) return;
    ta.dataset.wkExamples = '1';
    const sel = document.createElement('select');
    sel.style.cssText = 'display:block;background:var(--c-bg, #1e1e1e);border:1px solid var(--c-line, #2a2a2a);color:var(--c-bluehi, #9cdcfe);border-radius:4px;padding:0.22rem 0.45rem;font-size:0.72rem;margin-bottom:0.3rem;cursor:pointer;max-width:100%;font-family:inherit';
    sel.innerHTML = '<option value="">📋 Insert example instruction…</option>'
      + MAIL_EXAMPLES.map(e => `<option>${esc(e)}</option>`).join('');
    sel.addEventListener('change', () => {
      if (!sel.value) return;
      ta.value = ta.value.trim() ? (ta.value.trim() + ' ' + sel.value) : sel.value;
      sel.value = '';
      ta.focus();
    });
    ta.parentNode.insertBefore(sel, ta);
  }

  function _autoAttachExamples() {
    ['mailCustom', 'bmCustom', 'mmCustom'].forEach(attachExamples);
  }
  if (document.readyState !== 'loading') _autoAttachExamples();
  else document.addEventListener('DOMContentLoaded', _autoAttachExamples);

  window.WKMail = { personalize, toOutlook, fetchContacts, render, attach, firstNameOf, downloadEmlBundle, attachExamples,
                    exportKey, isExported, exportedAt, markExported, markExportedMany, clearExported, relTime, exportedTitle };
})();
