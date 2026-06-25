/*
 * System health watcher: shows a sticky red banner + one-shot modal when
 * GET /api/status reports zone faults (relay failed to confirm a zone ON).
 *
 * Self-contained IIFE, runs on every page. Independent of app.js polling.
 * Reuses global escapeHtml() from app.js for XSS-safe rendering (with a
 * local fallback in case it isn't loaded yet).
 */
(function () {
  'use strict';

  var POLL_MS = 15000;
  var SEEN_KEY = 'sysHealthSeenFaults'; // localStorage: JSON array of zone_id already shown
  var lastFaults = [];                  // most recent faults from /api/status (for banner click)

  // ---------- DOM helpers ----------
  function $(id) { return document.getElementById(id); }
  // Mirror history.js: prefer the global escapeHtml, fall back to a tiny escaper.
  function safeText(v) {
    if (typeof escapeHtml === 'function') return escapeHtml(v);
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  // ---------- localStorage: set of seen fault zone_ids ----------
  function loadSeen() {
    try {
      var raw = localStorage.getItem(SEEN_KEY);
      if (!raw) return [];
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.map(Number) : [];
    } catch (e) { return []; }
  }
  function saveSeen(ids) {
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(ids.map(Number))); } catch (e) {}
  }
  function clearSeen() {
    try { localStorage.removeItem(SEEN_KEY); } catch (e) {}
  }

  // ---------- Banner ----------
  function updateBanner(health) {
    var banner = $('sysFaultBanner');
    if (!banner) return;
    var faults = (health && health.faults) || [];
    if (health && health.ok === false && faults.length) {
      var n = faults.length;
      // Russian plural for "зона".
      var word = pluralZones(n);
      banner.querySelector('.sysfault-banner__text').textContent =
        '⚠️ Полив не работает: ' + n + ' ' + word + ' в сбое — нажмите для деталей';
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  function pluralZones(n) {
    var mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return 'зона';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return 'зоны';
    return 'зон';
  }

  // ---------- Modal ----------
  function renderModalBody(faults) {
    var box = $('sysFaultList');
    if (!box) return;
    var html = '';
    faults.forEach(function (f) {
      var name = safeText(f.zone_name || ('Зона ' + f.zone_id));
      var zid = safeText(f.zone_id);
      var reason = safeText(f.reason || 'Сбой');
      var since = f.since ? (', с ' + safeText(f.since)) : '';
      html += '<li class="sysfault-item">'
        + '<span class="sysfault-item__icon">⚠️</span>'
        + '<span class="sysfault-item__text">'
        + 'Зона «' + name + '» (#' + zid + ') — ' + reason + since
        + '</span>'
        + '</li>';
    });
    box.innerHTML = html;
  }

  function openModal() {
    var ov = $('sysFaultOverlay');
    if (!ov) return;
    ov.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    var ov = $('sysFaultOverlay');
    if (ov) ov.hidden = true;
    document.body.style.overflow = '';
  }

  // ---------- One-shot auto-open logic ----------
  // Show the modal automatically only when a *new* faulty zone appears that
  // we haven't shown before. The seen set is the baseline of currently-known
  // faults; it's reset to empty once all faults clear, so a zone that breaks
  // again later will surface again.
  function maybeAutoOpen(faults) {
    var currentIds = faults.map(function (f) { return Number(f.zone_id); });

    if (!currentIds.length) {
      // No faults right now — forget history so a future fault re-notifies.
      clearSeen();
      return;
    }

    var seen = loadSeen();
    var hasNew = currentIds.some(function (id) { return seen.indexOf(id) === -1; });

    // Always rebase the seen set to the current active faults. This both
    // records the just-shown set and drops zones that have recovered.
    saveSeen(currentIds);

    if (hasNew) {
      renderModalBody(faults);
      openModal();
    }
  }

  // ---------- Poll ----------
  function poll() {
    fetch('/api/status?ts=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var health = data && data.system_health;
        if (!health) { lastFaults = []; updateBanner(null); return; }
        lastFaults = (health.faults) || [];
        updateBanner(health);
        maybeAutoOpen(lastFaults);
      })
      .catch(function (err) { /* network hiccup — keep last banner state */ void err; })
      // Reschedule only after this poll settles — avoids overlapping requests
      // piling up on the slow WB controller if a fetch hangs past POLL_MS.
      .finally(function () { setTimeout(poll, POLL_MS); });
  }

  // ---------- Wire up ----------
  function init() {
    var closeX = $('sysFaultClose');
    var okBtn = $('sysFaultOk');
    var banner = $('sysFaultBanner');
    if (closeX) closeX.addEventListener('click', closeModal);
    if (okBtn) okBtn.addEventListener('click', closeModal);
    if (banner) banner.addEventListener('click', function () {
      renderModalBody(lastFaults);
      openModal();
    });

    poll(); // self-reschedules via setTimeout after each completion
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
