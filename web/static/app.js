// Agentanbud client-side enhancements
// Loaded on every page via base.html

(function () {
  'use strict';

  // === Theme toggle (light/dark/auto) ===
  const THEME_KEY = 'ot-theme';
  const root = document.documentElement;

  function applyTheme(theme) {
    if (theme === 'auto') {
      root.removeAttribute('data-theme');
    } else {
      root.setAttribute('data-theme', theme);
    }
  }

  function currentTheme() {
    return localStorage.getItem(THEME_KEY) || 'auto';
  }

  function setTheme(theme) {
    localStorage.setItem(THEME_KEY, theme);
    applyTheme(theme);
    updateToggleIcon();
  }

  function nextTheme() {
    const cycle = ['auto', 'light', 'dark'];
    const cur = currentTheme();
    setTheme(cycle[(cycle.indexOf(cur) + 1) % cycle.length]);
  }

  function updateToggleIcon() {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const t = currentTheme();
    btn.textContent = t === 'dark' ? '☀' : t === 'light' ? '◐' : '◑';
    btn.title = 'Tema: ' + t + ' (klicka för att växla)';
  }

  applyTheme(currentTheme());
  updateToggleIcon();

  document.getElementById('theme-toggle')?.addEventListener('click', nextTheme);

  // === Keyboard shortcuts ===
  // g d = dashboard, g b = browse, g p = providers, g r = research, g a = api
  // / = focus search, ? = show help, Esc = close

  let gMode = false;
  const routes = {
    d: '/',
    b: '/browse',
    p: '/providers',
    r: '/research',
    a: '/api-docs',
  };

  function showHelp() {
    const existing = document.getElementById('shortcuts-modal');
    if (existing) { existing.remove(); return; }

    const overlay = document.createElement('div');
    overlay.id = 'shortcuts-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;display:flex;align-items:center;justify-content:center;';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    overlay.innerHTML = `
      <div style="background:var(--surface);border-radius:8px;padding:1.5rem 2rem;max-width:480px;width:90%;box-shadow:var(--shadow-lg);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
          <h2 style="margin:0;font-size:1.1rem;">Tangentbordsgenvägar</h2>
          <button class="btn ghost" onclick="document.getElementById('shortcuts-modal').remove()" aria-label="Stäng">✕</button>
        </div>
        <table style="font-size:0.9rem;">
          <tr><td><kbd>/</kbd></td><td>Fokusera sökfältet</td></tr>
          <tr><td><kbd>g</kbd> <kbd>d</kbd></td><td>Gå till Dashboard</td></tr>
          <tr><td><kbd>g</kbd> <kbd>b</kbd></td><td>Gå till Browse</td></tr>
          <tr><td><kbd>g</kbd> <kbd>p</kbd></td><td>Gå till Providers</td></tr>
          <tr><td><kbd>g</kbd> <kbd>r</kbd></td><td>Gå till Research</td></tr>
          <tr><td><kbd>g</kbd> <kbd>a</kbd></td><td>Gå till API-docs</td></tr>
          <tr><td><kbd>t</kbd></td><td>Växla tema (auto/ljust/mörkt)</td></tr>
          <tr><td><kbd>?</kbd></td><td>Visa/dölj denna hjälp</td></tr>
          <tr><td><kbd>Esc</kbd></td><td>Stäng dialogruta</td></tr>
        </table>
        <p class="muted mt-2" style="font-size:0.85rem;">Tips: g trycks först, sedan bokstaven — som i vim eller GitHub.</p>
      </div>
    `;
    document.body.appendChild(overlay);
  }

  document.addEventListener('keydown', (e) => {
    // Don't intercept when typing in input/textarea
    const tag = e.target.tagName.toLowerCase();
    const isInput = tag === 'input' || tag === 'textarea' || e.target.isContentEditable;

    if (e.key === 'Escape') {
      document.getElementById('shortcuts-modal')?.remove();
      return;
    }

    if (e.key === '?' && !isInput) { showHelp(); return; }

    if (e.key === 't' && !isInput) { nextTheme(); return; }

    if (e.key === '/' && !isInput) {
      e.preventDefault();
      const s = document.querySelector('input[type=search], input[name=q]');
      if (s) { s.focus(); s.select(); }
      return;
    }

    // g-prefix shortcuts
    if (e.key === 'g' && !isInput && !gMode) {
      gMode = true;
      setTimeout(() => { gMode = false; }, 1500);
      return;
    }
    if (gMode && !isInput) {
      const route = routes[e.key];
      if (route) { window.location.href = route; }
      gMode = false;
    }
  });

  // === Toast helper ===
  window.otToast = function (msg, type = 'info', ms = 3000) {
    const existing = document.getElementById('ot-toast');
    if (existing) existing.remove();
    const t = document.createElement('div');
    t.id = 'ot-toast';
    t.className = 'toast';
    t.innerHTML = `<span class="badge badge-${type === 'error' ? 'err' : type === 'ok' ? 'ok' : 'accent'}">${type}</span> <span style="margin-left:0.5rem;">${msg}</span>`;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity 200ms'; setTimeout(() => t.remove(), 200); }, ms);
  };

  // === Admin fetch helper ===
  // When ADMIN_API_KEY is set on the server, mutating endpoints require the
  // X-Admin-Key header. Ask once, keep in localStorage, retry on 401.
  const ADMIN_KEY = 'ot-admin-key';
  window.adminFetch = async function (url, opts = {}) {
    const doFetch = (key) => fetch(url, {
      ...opts,
      headers: { ...(opts.headers || {}), ...(key ? { 'X-Admin-Key': key } : {}) },
    });
    let r = await doFetch(localStorage.getItem(ADMIN_KEY) || '');
    if (r.status === 401) {
      const key = prompt('Admin-nyckel krävs (X-Admin-Key):');
      if (key) {
        localStorage.setItem(ADMIN_KEY, key.trim());
        r = await doFetch(key.trim());
      }
    }
    return r;
  };

  // === Sync button (dashboard) ===
  const syncBtn = document.getElementById('sync-btn');
  if (syncBtn) {
    syncBtn.addEventListener('click', async () => {
      syncBtn.disabled = true;
      syncBtn.innerHTML = '<span class="spinner"></span> Kör sync...';
      try {
        const r = await window.adminFetch('/api/sync', { method: 'POST' });
        if (r.status === 409) {
          window.otToast('Sync körs redan', 'warn', 4000);
          return;
        }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        window.otToast('Sync startad — laddar om om 60s', 'ok');
        setTimeout(() => location.reload(), 60000);
      } catch (e) {
        window.otToast('Fel: ' + e.message, 'error', 5000);
        syncBtn.disabled = false;
        syncBtn.textContent = 'Kör sync nu';
      }
    });
  }

  // === Live dashboard refresh (every 30s) ===
  if (document.querySelector('[data-auto-refresh]')) {
    setTimeout(() => location.reload(), 30000);
  }

  // === Highlight search query in result titles ===
  const query = new URLSearchParams(location.search).get('q');
  if (query && query.length >= 2) {
    const re = new RegExp('(' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    document.querySelectorAll('a.tender-link, .card-tender h3').forEach((el) => {
      const html = el.innerHTML;
      el.innerHTML = html.replace(re, '<mark>$1</mark>');
    });
  }
})();
