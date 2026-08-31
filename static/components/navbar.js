/**
 * Shared navbar component for Buzzowl.
 * Usage: include this script, then call initNavbar({ active, actions, subtitle }).
 * Exposes window.navbarRefreshUser() so pages can refresh the user chip after login.
 */
(function () {
  if (!document.getElementById('wk-navbar-style')) {
    const s = document.createElement('style');
    s.id = 'wk-navbar-style';
    s.textContent = `
      #main-nav {
        display: flex;
        align-items: center;
        gap: 1rem;
        padding: 0.5rem 1.2rem;
        border-bottom: 1px solid var(--c-linefaint, #222);
        background: var(--c-bg3, #181818);
        flex-shrink: 0;
        font-family:var(--c-font-sans, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif);
        font-size: 0.82rem;
        min-height: 38px;
      }
      #main-nav .nav-brand {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        color: var(--c-text, #d4d4d4);
        font-weight: 700;
        font-size: 0.92rem;
        white-space: nowrap;
        text-decoration: none;
      }
      #main-nav .nav-subtitle {
        color: var(--c-faint, #555);
        font-size: 0.8rem;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 200px;
        /* Without this it is the one item the row can squeeze, so a crowded
           bar shrank the record's name to "N..". The bar (a scrolling rail,
           or a horizontally-scrolling row under 900px) overflows instead of
           reflowing; max-width still caps very long names. */
        flex-shrink: 0;
      }
      #main-nav a.nav-link {
        color: var(--c-muted, #888);
        text-decoration: none;
        font-size: 0.82rem;
        white-space: nowrap;
        padding-bottom: 2px;
        border-bottom: 2px solid transparent;
        transition: color 0.15s;
      }
      #main-nav a.nav-link:hover { color: var(--c-text2, #ccc); }
      #main-nav a.nav-link.nav-active {
        color: var(--c-text, #d4d4d4);
        font-weight: 600;
        border-bottom-color: var(--c-accent, #4ec9b0);
      }
      #main-nav .nav-spacer { flex: 1; }
      /* Links live in their own box, separate from the tools cluster, so each
         group lays out independently. Neither must shrink: every link and
         button already has white-space:nowrap, so a shrinkable box would
         just let its content get clipped instead of the bar's own scroll
         (vertical on the rail, horizontal under 900px) taking the overflow. */
      #main-nav .nav-links {
        display: flex;
        align-items: center;
        gap: 1rem;
        flex-shrink: 0;
      }
      #main-nav .nav-tools {
        display: flex;
        align-items: center;
        gap: 1rem;
        flex-shrink: 0;
      }
      #main-nav .nav-action-btn {
        background: var(--c-surface, #252526);
        border: 1px solid var(--c-line3, #3a3a3a);
        color: var(--c-text2, #ccc);
        border-radius: 4px;
        padding: 0.28rem 0.75rem;
        font-size: 0.78rem;
        cursor: pointer;
        transition: background 0.15s;
        font-family: inherit;
        white-space: nowrap;
      }
      #main-nav .nav-action-btn:hover { background: var(--c-surface2, #2d2d2d); }
      #main-nav .nav-action-btn.nav-btn-teal {
        background: var(--c-accenttint, #1a3a2e);
        border-color: var(--c-accent, #4ec9b0);
        color: var(--c-accent, #4ec9b0);
      }
      #main-nav .nav-action-btn.nav-btn-teal:hover { background: var(--c-accenttint2, #1f4a38); }
      #main-nav .nav-action-btn.nav-btn-ghost {
        background: transparent;
        border: none;
        color: var(--c-faint, #555);
        font-size: 1rem;
        padding: 0.1rem 0.35rem;
      }
      #main-nav .nav-action-btn.nav-btn-ghost:hover { color: var(--c-text3, #aaa); }
      #main-nav .nav-user {
        font-size: 0.75rem;
        color: var(--c-faint, #555);
        white-space: nowrap;
      }
      #main-nav .nav-logout {
        background: none;
        border: none;
        color: var(--c-faint2, #444);
        font-size: 0.75rem;
        cursor: pointer;
        font-family: inherit;
        padding: 0;
        white-space: nowrap;
      }
      #main-nav .nav-logout:hover { color: var(--c-text2, #ccc); }
      #wk-fb-btn { background:none;border:none;color:var(--c-faint2, #444);font-size:0.88rem;cursor:pointer;padding:0.1rem 0.4rem;font-family:inherit;transition:color 0.15s;line-height:1;white-space:nowrap; }
      #wk-fb-btn:hover { color:var(--c-orange, #ce9178); }
      #wk-fb-overlay { position:fixed;inset:0;background:rgba(0,0,0,0.75);display:flex;align-items:center;justify-content:center;z-index:500; }
      #wk-fb-box { background:var(--c-surface, #252526);border:1px solid var(--c-line2, #333);border-radius:8px;padding:1.5rem;width:380px;display:flex;flex-direction:column;gap:0.75rem; }
      #wk-fb-box h3 { font-size:0.92rem;font-weight:600;color:var(--c-text, #d4d4d4);margin:0; }
      #wk-fb-box input,#wk-fb-box textarea { background:var(--c-bg, #1e1e1e);border:1px solid var(--c-line2, #333);color:var(--c-text, #d4d4d4);border-radius:4px;padding:0.45rem 0.65rem;font-size:0.84rem;width:100%;box-sizing:border-box;font-family:inherit; }
      #wk-fb-box textarea { resize:vertical;min-height:100px; }
      #wk-fb-box input:focus,#wk-fb-box textarea:focus { outline:none;border-color:var(--c-orange, #ce9178); }
      .wk-fb-row { display:flex;gap:0.5rem;justify-content:flex-end; }
      .wk-fb-btn-cancel { padding:0.3rem 0.8rem;border-radius:4px;font-size:0.8rem;cursor:pointer;font-family:inherit;border:1px solid var(--c-line2, #333);background:var(--c-surface, #252526);color:var(--c-text, #d4d4d4); }
      .wk-fb-btn-send { padding:0.3rem 0.8rem;border-radius:4px;font-size:0.8rem;cursor:pointer;font-family:inherit;border:1px solid var(--c-orange, #ce9178);background:var(--c-accenttint, #1a3a2e);color:var(--c-orange, #ce9178); }
      #wk-fb-status { font-size:0.76rem;min-height:1rem;color:var(--c-muted, #888); }
      /* No-LLM-configured banner (WP9) — DOM-injected as #main-nav's sibling,
         never into nav.innerHTML, so it survives every initNavbar() re-render. */
      #wk-llm-banner {
        display:flex; align-items:center; gap:0.6rem; padding:0.5rem 1.2rem;
        font-family:var(--c-font-sans, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif);
        font-size:0.8rem; background:var(--c-orangetint, #2c1a0d); color:var(--c-orange, #ce9178);
        border-bottom:1px solid var(--c-orange, #ce9178);
      }
      #wk-llm-banner a { color:inherit; font-weight:600; text-decoration:underline; }
      #wk-llm-banner .wk-llm-banner-text { flex:1; }
      #wk-llm-banner button { background:none;border:none;color:inherit;font-size:0.85rem;cursor:pointer;padding:0 0.2rem;opacity:0.8;font-family:inherit; }
      #wk-llm-banner button:hover { opacity:1; }
    `;
    document.head.appendChild(s);
  }

  // Owner-approved owl mark ("night owl", variant B) — small-size stroke
  // variant (heavier stroke, larger eyes, longer beak) sized for the nav rail.
  const OWL_MARK_SVG = `<svg viewBox="0 0 32 32" width="20" height="20" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Buzzowl" style="flex-shrink:0">
        <path d="M5.5 10.5 L8.5 4.5 L12 8.2 Q16 6.8 20 8.2 L23.5 4.5 L26.5 10.5 Q28 14 27.4 18.5 Q26.6 24.6 21.5 27 L16 28.6 L10.5 27 Q5.4 24.6 4.6 18.5 Q4 14 5.5 10.5 Z" fill="none" stroke="#E8A33D" stroke-width="2.4" stroke-linejoin="round"/>
        <circle cx="11.2" cy="15.6" r="4.6" fill="#E8A33D"/>
        <circle cx="20.8" cy="15.6" r="4.6" fill="#E8A33D"/>
        <circle cx="12.3" cy="14.9" r="1.6" fill="#131313"/>
        <circle cx="21.9" cy="14.9" r="1.6" fill="#131313"/>
        <path d="M16 20.4 L14.2 23.4 L17.8 23.4 Z" fill="#E8A33D"/>
      </svg>`;

  const LINKS = [
    { href: '/',          label: 'Home',      key: 'home' },
    { href: '/today',     label: 'Today',     key: 'today' },
    { href: '/ranking',   label: 'Ranking',   key: 'ranking' },
    { href: '/record',    label: 'Transcripts', key: 'record' },
    { href: '/knowledge', label: 'Knowledge', key: 'knowledge' },
    { href: '/clients',   label: 'Clients',   key: 'clients' },
    { href: '/products',  label: 'Products',  key: 'products' },
    { href: '/match',     label: 'Match',     key: 'match' },
    { href: '/opportunities', label: 'Opportunities', key: 'opportunities' },
    { href: '/outreach',  label: 'Outreach',  key: 'outreach' },
    { href: '/pipeline',  label: 'Pipeline',  key: 'pipeline' },
    { href: '/news',      label: 'News',      key: 'news' },
    { href: '/research',  label: 'Research',  key: 'research', adminOnly: true },
    { href: '/agents',    label: 'Agents',    key: 'agents',   adminOnly: true },
    { href: '/insights',  label: 'Insights',  key: 'insights', adminOnly: true },
  ];

  window.initNavbar = function ({ active = '', actions = [], subtitle = false } = {}) {
    const nav = document.getElementById('main-nav');
    if (!nav) return;

    // Admin-only links (Agents, Research) start hidden and are revealed in
    // loadNavbarUser() once /api/auth/me confirms the user is an admin.
    const linksHtml = LINKS.map(l => {
      const cls = active === l.key ? ' nav-active' : '';
      const adminAttrs = l.adminOnly ? ' data-admin-only="1" style="display:none"' : '';
      return `<a href="${l.href}" class="nav-link${cls}"${adminAttrs}>${l.label}</a>`;
    }).join('');

    const subtitleHtml = subtitle
      ? `<span class="nav-subtitle" id="navSubtitle"></span>`
      : '';

    const actionsHtml = actions.map(a => {
      const extraCls = a.teal ? ' nav-btn-teal' : a.ghost ? ' nav-btn-ghost' : '';
      return `<button id="${a.id}" class="nav-action-btn${extraCls}">${a.label}</button>`;
    }).join('');

    nav.innerHTML = `
      <span class="nav-brand">${OWL_MARK_SVG}<span>Buzzowl</span></span>
      ${subtitleHtml}
      <div class="nav-links" id="navLinks">${linksHtml}</div>
      <div class="nav-spacer"></div>
      <div class="nav-tools" id="navTools">
        ${actionsHtml}
        <button id="wk-fb-btn" title="Send feedback">✉</button>
        <span class="nav-user" id="navUserDisplay"></span>
        <button class="nav-logout" id="navLogoutBtn" style="display:none">logout</button>
      </div>
    `;

    // Feedback modal — inject once into document.body
    if (!document.getElementById('wk-fb-overlay')) {
      const overlay = document.createElement('div');
      overlay.id = 'wk-fb-overlay';
      overlay.style.display = 'none';
      overlay.innerHTML = `<div id="wk-fb-box">
        <h3>Send Feedback</h3>
        <input id="wk-fb-subject" type="text" placeholder="Subject" maxlength="200">
        <textarea id="wk-fb-message" placeholder="Your message…" maxlength="4000"></textarea>
        <div id="wk-fb-status"></div>
        <div class="wk-fb-row">
          <button class="wk-fb-btn-cancel" id="wk-fb-cancel">Cancel</button>
          <button class="wk-fb-btn-send" id="wk-fb-submit">Send</button>
        </div>
      </div>`;
      document.body.appendChild(overlay);

      document.getElementById('wk-fb-cancel').addEventListener('click', () => {
        document.getElementById('wk-fb-overlay').style.display = 'none';
      });
      document.getElementById('wk-fb-submit').addEventListener('click', async () => {
        const subject = document.getElementById('wk-fb-subject').value.trim();
        const message = document.getElementById('wk-fb-message').value.trim();
        const status  = document.getElementById('wk-fb-status');
        if (!subject || !message) {
          status.textContent = 'Subject and message are required.';
          status.style.color = '#f14c4c';
          return;
        }
        document.getElementById('wk-fb-submit').disabled = true;
        status.textContent = 'Sending…'; status.style.color = '#888';
        const token = localStorage.getItem('wk_token');
        const hdrs = { 'Content-Type': 'application/json' };
        if (token) hdrs['Authorization'] = `Bearer ${token}`;
        try {
          const r = await fetch('/api/feedback', {
            method: 'POST', headers: hdrs,
            body: JSON.stringify({ subject, message }),
          });
          if (!r.ok) throw new Error(`${r.status}`);
          status.textContent = 'Thank you!'; status.style.color = '#4ec9b0';
          setTimeout(() => {
            document.getElementById('wk-fb-overlay').style.display = 'none';
          }, 1400);
        } catch(e) {
          status.textContent = 'Failed to send. Try again.'; status.style.color = '#f14c4c';
        } finally {
          document.getElementById('wk-fb-submit').disabled = false;
        }
      });
    }

    document.getElementById('wk-fb-btn').addEventListener('click', () => {
      document.getElementById('wk-fb-subject').value = '';
      document.getElementById('wk-fb-message').value = '';
      document.getElementById('wk-fb-status').textContent = '';
      document.getElementById('wk-fb-overlay').style.display = 'flex';
    });

    document.getElementById('navLogoutBtn').addEventListener('click', async () => {
      const token = localStorage.getItem('wk_token');
      if (token) {
        try {
          await fetch('/api/auth/logout', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
          });
        } catch (_) {}
      }
      localStorage.removeItem('wk_token');
      location.reload();
    });

    loadNavbarUser();

    // Page-view beacon for the evaluation — one prompt_log row per page load.
    try {
      const t = localStorage.getItem('wk_token');
      if (t) fetch('/api/eval/pageview', {
        method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${t}` },
        body: JSON.stringify({ path: location.pathname }),
      }).catch(() => {});
    } catch (_) {}
  };

  // ── No-LLM-configured banner (WP9) ────────────────────────────────────────
  const LLM_BANNER_ID = 'wk-llm-banner';
  const LLM_BANNER_HIDDEN_KEY = 'wk_llm_banner_hidden';

  function removeLlmBanner() {
    const el = document.getElementById(LLM_BANNER_ID);
    if (el) el.remove();
  }

  function showLlmBanner(role) {
    if (location.pathname === '/setup') return;         // the wizard IS the fix — don't nag on it
    if (localStorage.getItem(LLM_BANNER_HIDDEN_KEY) === '1') return;
    if (document.getElementById(LLM_BANNER_ID)) return;  // already shown

    const banner = document.createElement('div');
    banner.id = LLM_BANNER_ID;
    const text = document.createElement('span');
    text.className = 'wk-llm-banner-text';
    if (role === 'admin') {
      text.innerHTML = 'No AI model is configured — chat, research and summaries are off. <a href="/setup">Set it up</a>.';
    } else {
      text.textContent = 'No AI model is configured yet — ask your workspace admin.';
    }
    const dismiss = document.createElement('button');
    dismiss.textContent = '✕';
    dismiss.title = 'Dismiss';
    dismiss.addEventListener('click', () => {
      localStorage.setItem(LLM_BANNER_HIDDEN_KEY, '1');
      removeLlmBanner();
    });
    banner.appendChild(text);
    banner.appendChild(dismiss);

    // Injected as #main-nav's sibling, never into nav.innerHTML (that gets
    // wholesale-replaced on every initNavbar() call) — works whether the nav
    // is mounted under .page or directly under <body> (see theme.css's two
    // mutually-exclusive rail-offset rules), since either parent already
    // reserves the rail's width via padding-left.
    const nav = document.getElementById('main-nav');
    if (nav && nav.parentNode) {
      nav.parentNode.insertBefore(banner, nav.nextSibling);
    } else {
      document.body.insertBefore(banner, document.body.firstChild);
    }
  }

  async function loadNavbarUser() {
    const token = localStorage.getItem('wk_token');
    const userEl   = document.getElementById('navUserDisplay');
    const logoutEl = document.getElementById('navLogoutBtn');
    if (!userEl) return;
    if (!token) {
      userEl.textContent = '';
      if (logoutEl) logoutEl.style.display = 'none';
      return;
    }
    try {
      const res = await fetch('/api/auth/me', {
        headers: { 'Authorization': `Bearer ${token}` },
      });
      if (res.ok) {
        const d = await res.json();
        userEl.textContent = `${d.org.slug}/${d.user.username}`;
        if (logoutEl) logoutEl.style.display = '';
        window.userRole = d.user.role;
        // Reveal admin-only nav links (Agents, Research) for admins
        if (d.user.role === 'admin') {
          document.querySelectorAll('#main-nav [data-admin-only]').forEach(el => { el.style.display = ''; });
        }
        // Gear icon → /settings for admins
        const existingGear = document.getElementById('navSettingsGear');
        if (existingGear) existingGear.remove();
        if (d.user.role === 'admin') {
          const gear = document.createElement('a');
          gear.id = 'navSettingsGear';
          gear.href = '/settings';
          gear.textContent = '⚙';
          gear.title = 'Settings';
          gear.style.cssText = 'color:var(--c-faint2, #444);text-decoration:none;font-size:0.85rem;margin-left:0.5rem;transition:color 0.15s';
          gear.onmouseenter = () => { gear.style.color = '#9cdcfe'; };
          gear.onmouseleave = () => { gear.style.color = '#444'; };
          userEl.parentNode.insertBefore(gear, userEl.nextSibling);
        }
        // No-LLM-configured banner: show while it's false, auto-clear the
        // dismissal (and drop a stale banner) the moment it becomes true.
        if (d.org && d.org.llm_configured === false) {
          showLlmBanner(d.user.role);
        } else if (d.org && d.org.llm_configured === true) {
          localStorage.removeItem(LLM_BANNER_HIDDEN_KEY);
          removeLlmBanner();
        }
      } else {
        userEl.textContent = '';
        if (logoutEl) logoutEl.style.display = 'none';
      }
    } catch (_) {
      userEl.textContent = '';
    }
  }

  // Call this after a successful login so the user chip updates without a reload.
  window.navbarRefreshUser = loadNavbarUser;

})();
