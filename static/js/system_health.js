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

  // ---------- Severity ----------
  // Faults without an explicit severity default to 'critical' (back-compat).
  function isCritical(f) { return (f && f.severity) ? f.severity === 'critical' : true; }

  // ---------- Banner ----------
  function updateBanner(health) {
    var banner = $('sysFaultBanner');
    if (!banner) return;
    var textEl = banner.querySelector('.sysfault-banner__text');
    var faults = (health && health.faults) || [];
    var critical = faults.filter(isCritical);
    var warnings = faults.filter(function (f) { return f && f.severity === 'warning'; });

    if (critical.length) {
      // Red alarm: watering is actually broken.
      banner.classList.remove('sysfault-banner--warning');
      var n = critical.length;
      var word = pluralZones(n);
      textEl.textContent =
        '⚠️ Полив не работает: ' + n + ' ' + word + ' в сбое — нажмите для деталей';
      banner.hidden = false;
    } else if (warnings.length) {
      // Amber warning: watering continues (e.g. sensor_mismatch → API fallback).
      banner.classList.add('sysfault-banner--warning');
      textEl.textContent = (warnings.length === 1)
        ? '🟡 ' + (warnings[0].reason || 'Предупреждение') + ' — нажмите для деталей'
        : '🟡 Предупреждений: ' + warnings.length + ' — нажмите для деталей';
      banner.hidden = false;
    } else {
      banner.classList.remove('sysfault-banner--warning');
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
    var hasCrit = faults.some(isCritical);
    var titleEl = $('sysFaultTitle');
    if (titleEl) {
      titleEl.textContent = hasCrit ? '⚠️ Сбой системы полива' : '🟡 Предупреждения';
    }
    var leadEl = $('sysFaultLead');
    if (leadEl) {
      // Warning-only must NOT claim zones aren't watering — watering continues.
      leadEl.textContent = hasCrit
        ? 'Обнаружены сбои системы полива:'
        : 'Полив продолжается, но требуется внимание:';
    }
    var html = '';
    faults.forEach(function (f) {
      var reason = safeText(f.reason || 'Сбой');
      var since = f.since ? (', с ' + safeText(f.since)) : '';
      var icon = (f.severity === 'warning') ? '🟡' : '⚠️';
      var text;
      if (f.zone_id != null) {
        // Zone-scoped fault (relay failed to confirm).
        var name = safeText(f.zone_name || ('Зона ' + f.zone_id));
        text = 'Зона «' + name + '» (#' + safeText(f.zone_id) + ') — ' + reason + since;
      } else {
        // System-wide fault/warning (mqtt_disconnect, sensor_mismatch).
        text = reason + since;
      }
      html += '<li class="sysfault-item">'
        + '<span class="sysfault-item__icon">' + icon + '</span>'
        + '<span class="sysfault-item__text">' + text + '</span>'
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
        // Auto-open the modal only for critical (relay) faults — warnings are
        // surfaced by the amber banner without an intrusive popup.
        maybeAutoOpen(lastFaults.filter(isCritical));
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
