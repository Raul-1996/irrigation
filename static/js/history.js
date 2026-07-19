/*
 * Issue #35: irrigation history modal.
 * Public API:
 *   window.historyModal.openGlobal()
 *   window.historyModal.openForZone(zoneId)
 *   window.historyModal.openForGroup(groupId)
 *   window.historyModal.close()
 * Depends on: Chart.js v4 (window.Chart), escapeHtml() from app.js.
 */
(function () {
  'use strict';

  var state = {
    days: 7,
    groupId: 'all',     // 'all' | number
    zoneId: 'all',      // 'all' | number
    compare: true,
    lastData: null,     // last per-zone or aggregated payload
    zonesCache: [],
    groupsCache: [],
    chart: null,
    refreshGeneration: 0,
  };

  // ---------- DOM helpers ----------
  function $(id) { return document.getElementById(id); }
  function safeText(v) { return (typeof escapeHtml === 'function') ? escapeHtml(v) : String(v == null ? '' : v); }

  // ---------- Open / close ----------
  function open() {
    var ov = $('historyOverlay');
    if (!ov) { console.warn('history modal not mounted'); return; }
    ov.hidden = false;
    document.body.style.overflow = 'hidden';
    loadZonesAndGroups().then(function () {
      bindControls();
      refresh();
    });
  }

  function close() {
    state.refreshGeneration += 1;
    var ov = $('historyOverlay');
    if (ov) ov.hidden = true;
    document.body.style.overflow = '';
    if (state.chart) { try { state.chart.destroy(); } catch (e) {} state.chart = null; }
  }

  function openGlobal() {
    state.groupId = 'all';
    state.zoneId = 'all';
    open();
  }

  function openForZone(zoneId) {
    state.zoneId = Number(zoneId);
    state.groupId = 'all';   // populated after zones load
    open();
  }

  function openForGroup(groupId) {
    state.groupId = Number(groupId);
    state.zoneId = 'all';
    open();
  }

  // ---------- Data loading ----------
  function loadZonesAndGroups() {
    // Reuse SSR data if present — saves a round-trip.
    if (state.zonesCache.length && state.groupsCache.length) {
      populateSelectors();
      return Promise.resolve();
    }
    var zonesP = (window._ssrZones && window._ssrZones.length)
      ? Promise.resolve({ zones: window._ssrZones })
      : fetch('/api/zones', { cache: 'no-store' }).then(function (r) { return r.json(); });
    var groupsP = (window._ssrGroups && window._ssrGroups.length)
      ? Promise.resolve({ groups: window._ssrGroups })
      : fetch('/api/groups', { cache: 'no-store' }).then(function (r) { return r.json(); });
    return Promise.all([zonesP, groupsP]).then(function (rs) {
      var zonesPayload = rs[0];
      var groupsPayload = rs[1];
      var zones = Array.isArray(zonesPayload) ? zonesPayload : (zonesPayload.zones || []);
      var groups = Array.isArray(groupsPayload) ? groupsPayload : (groupsPayload.groups || []);
      state.zonesCache = zones.filter(function (z) { return z.group_id !== 999; });
      state.groupsCache = groups.filter(function (g) { return g.id !== 999; });
      // If zone preselected, sync groupId from the zone.
      if (state.zoneId !== 'all') {
        var z = state.zonesCache.find(function (x) { return x.id === state.zoneId; });
        if (z) state.groupId = z.group_id;
      }
      populateSelectors();
    }).catch(function (err) {
      console.warn('history: failed to load zones/groups', err);
    });
  }

  function populateSelectors() {
    var gSel = $('historyGroupSelect');
    var zSel = $('historyZoneSelect');
    if (!gSel || !zSel) return;
    // Groups: "All" + each group
    var gHtml = '<option value="all">Все группы</option>';
    state.groupsCache.forEach(function (g) {
      gHtml += '<option value="' + g.id + '">' + safeText(g.name) + '</option>';
    });
    gSel.innerHTML = gHtml;
    gSel.value = String(state.groupId);
    refillZoneSelect();
  }

  function refillZoneSelect() {
    var zSel = $('historyZoneSelect');
    if (!zSel) return;
    var filtered = state.zonesCache;
    if (state.groupId !== 'all') {
      filtered = filtered.filter(function (z) { return z.group_id === Number(state.groupId); });
    }
    var zHtml = '<option value="all">Все зоны</option>';
    filtered.forEach(function (z) {
      zHtml += '<option value="' + z.id + '">#' + z.id + ' ' + safeText(z.name) + '</option>';
    });
    zSel.innerHTML = zHtml;
    // Keep zone selection if it still fits the current group filter.
    if (state.zoneId !== 'all' && filtered.some(function (z) { return z.id === Number(state.zoneId); })) {
      zSel.value = String(state.zoneId);
    } else {
      zSel.value = 'all';
      state.zoneId = 'all';
    }
  }

  // ---------- Controls binding (idempotent) ----------
  function bindControls() {
    var gSel = $('historyGroupSelect');
    var zSel = $('historyZoneSelect');
    var cmp = $('historyCompareToggle');
    var csv = $('historyCsvBtn');
    if (gSel && !gSel._bound) {
      gSel._bound = true;
      gSel.addEventListener('change', function () {
        state.groupId = (gSel.value === 'all') ? 'all' : Number(gSel.value);
        state.zoneId = 'all';
        refillZoneSelect();
        refresh();
      });
    }
    if (zSel && !zSel._bound) {
      zSel._bound = true;
      zSel.addEventListener('change', function () {
        state.zoneId = (zSel.value === 'all') ? 'all' : Number(zSel.value);
        refresh();
      });
    }
    if (cmp && !cmp._bound) {
      cmp._bound = true;
      cmp.checked = state.compare;
      cmp.addEventListener('change', function () {
        state.compare = cmp.checked;
        renderChart();
        renderBanner();
      });
    }
    if (csv && !csv._bound) {
      csv._bound = true;
      csv.addEventListener('click', onCsvClick);
    }
    document.querySelectorAll('.history-range__btn').forEach(function (btn) {
      if (btn._bound) return;
      btn._bound = true;
      btn.addEventListener('click', function () {
        var d = Number(btn.dataset.days);
        if (d !== 7 && d !== 30) return;
        state.days = d;
        document.querySelectorAll('.history-range__btn').forEach(function (b) {
          b.classList.toggle('is-active', Number(b.dataset.days) === d);
        });
        refresh();
      });
    });
  }

  // ---------- Refresh: fetch + render ----------
  function clearHistoryView(message) {
    state.lastData = null;
    if (state.chart) { try { state.chart.destroy(); } catch (e) {} state.chart = null; }
    var canvas = $('historyChart');
    if (canvas && typeof canvas.getContext === 'function') {
      try { canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height); } catch (e) {}
    }
    var minutes = $('historyTotalMinutes'); if (minutes) minutes.textContent = '—';
    var runs = $('historyTotalRuns'); if (runs) runs.textContent = '—';
    var liters = $('historyTotalLiters'); if (liters) liters.textContent = '—';
    var litersCard = $('historyLitersCard'); if (litersCard) litersCard.hidden = true;
    var litersSub = $('historyLitersSub'); if (litersSub) litersSub.hidden = true;
    var empty = $('historyChartEmpty');
    if (empty) { empty.hidden = false; empty.textContent = 'История временно недоступна'; }
    var banner = $('historySavingsBanner'); if (banner) banner.hidden = true;
    var noPlan = $('historyNoPlanNote'); if (noPlan) noPlan.hidden = true;
    var box = $('historyRunsList');
    if (box) box.innerHTML = '<div class="history-runs__empty">Ошибка загрузки истории: ' + safeText(message || 'сеть') + '</div>';
    var footer = $('historyFooterStats'); if (footer) footer.textContent = 'Данные недоступны';
    var title = $('historyTitle'); if (title) title.textContent = '💧 История — данные недоступны';
    var csv = $('historyCsvBtn'); if (csv) csv.hidden = true;
  }

  function refresh() {
    var generation = ++state.refreshGeneration;
    var url = buildJsonUrl();
    fetch(url, { cache: 'no-store' })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (data) {
          if (!r.ok || !data || data.success === false) {
            throw new Error((data && (data.message || data.error)) || ('HTTP ' + r.status));
          }
          return data;
        });
      })
      .then(function (data) {
        if (generation !== state.refreshGeneration) return;
        state.lastData = data;
        renderSummary();
        renderChart();
        renderBanner();
        renderRuns();
        renderFooter();
        renderTitle();
      })
      .catch(function (err) {
        if (generation !== state.refreshGeneration) return;
        console.warn('history fetch error', err);
        clearHistoryView(err.message || 'сеть');
      });
  }

  function buildJsonUrl() {
    if (state.zoneId !== 'all') {
      return '/api/zones/' + state.zoneId + '/history?days=' + state.days;
    }
    var qs = ['days=' + state.days];
    if (state.groupId !== 'all') qs.push('group_id=' + state.groupId);
    return '/api/zones/history?' + qs.join('&');
  }

  // ---------- Render: title + summary ----------
  function renderTitle() {
    var t = $('historyTitle');
    if (!t) return;
    if (state.zoneId !== 'all' && state.lastData && state.lastData.zone) {
      t.textContent = '💧 История — #' + state.lastData.zone.id + ' ' + (state.lastData.zone.name || '');
    } else if (state.groupId !== 'all') {
      var g = state.groupsCache.find(function (x) { return x.id === Number(state.groupId); });
      t.textContent = '💧 История — ' + (g ? g.name : 'группа');
    } else {
      t.textContent = '💧 История полива';
    }
  }

  function renderSummary() {
    var d = state.lastData;
    if (!d) return;
    var s = d.summary || {};
    var minutes = Math.round(s.total_minutes || 0);
    var runs = s.total_runs || 0;
    $('historyTotalMinutes').textContent = minutes;
    $('historyTotalRuns').textContent = runs;
    var card = $('historyLitersCard');
    var lEl = $('historyTotalLiters');
    var sub = $('historyLitersSub');
    if (s.has_liters) {
      card.hidden = false;
      lEl.textContent = Math.round(s.total_liters || 0);
      sub.hidden = !s.liters_partial;
    } else {
      // No flow data anywhere — hide liters tile.
      card.hidden = true;
    }
  }

  // ---------- Chart ----------
  function renderChart() {
    var d = state.lastData;
    if (!d) return;
    var canvas = $('historyChart');
    var empty = $('historyChartEmpty');
    if (!canvas || !window.Chart) return;
    empty.textContent = 'За выбранный период нет запусков';
    var daily = d.daily || [];
    var hasAnyRun = daily.some(function (x) { return (x.runs || 0) > 0; });
    empty.hidden = hasAnyRun;

    var labels = daily.map(function (x) { return formatDateShort(x.date); });
    var fact = daily.map(function (x) { return Math.round((x.actual_minutes || 0) * 10) / 10; });
    var plan = daily.map(function (x) { return x.plan_minutes; });
    var summary = d.summary || {};
    var planAvailable = summary.plan_available !== undefined
      ? summary.plan_available === true
      : summary.has_plan === true;
    var hasPlan = planAvailable && plan.some(function (v) { return v != null; });

    // Marker colors
    var todayIso = (new Date()).toISOString().slice(0, 10);
    // Today is the local date; compare by suffix.
    var todayLocal = (function () {
      var n = new Date(); var y = n.getFullYear(); var m = String(n.getMonth() + 1).padStart(2, '0'); var d_ = String(n.getDate()).padStart(2, '0');
      return y + '-' + m + '-' + d_;
    })();
    void todayIso;

    var pointColors = daily.map(function (x) {
      if (x.date === todayLocal) return '#4caf50';   // today — green
      if ((x.runs || 0) >= 2) return '#ff9800';      // dense day — orange
      return '#2196f3';
    });
    var pointRadii = daily.map(function (x) {
      if (x.date === todayLocal || (x.runs || 0) >= 2) return 5;
      return 3;
    });

    var datasets = [{
      label: 'Факт (мин)',
      data: fact,
      borderColor: '#2196f3',
      backgroundColor: 'rgba(33, 150, 243, 0.15)',
      pointBackgroundColor: pointColors,
      pointBorderColor: pointColors,
      pointRadius: pointRadii,
      tension: 0.25,
      fill: true,
    }];
    if (state.compare && hasPlan) {
      datasets.push({
        label: 'План (мин)',
        data: plan,
        borderColor: '#9e9e9e',
        backgroundColor: 'transparent',
        borderDash: [6, 4],
        pointRadius: 0,
        tension: 0.25,
        fill: false,
      });
    }

    if (state.chart) { try { state.chart.destroy(); } catch (e) {} state.chart = null; }
    state.chart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: true, position: 'bottom', labels: { boxWidth: 12, padding: 10 } },
          tooltip: {
            callbacks: {
              afterBody: function (items) {
                if (!items || !items.length) return '';
                var i = items[0].dataIndex;
                var day = daily[i];
                if (!day) return '';
                var parts = ['Запусков: ' + (day.runs || 0)];
                if (day.liters != null) parts.push('Литров: ' + Math.round(day.liters));
                return parts;
              }
            }
          }
        },
        scales: {
          y: { beginAtZero: true, title: { display: true, text: 'Минуты' } },
          x: { ticks: { maxRotation: 0, autoSkip: true } },
        },
      },
    });
  }

  function formatDateShort(iso) {
    // 'YYYY-MM-DD' -> 'DD.MM'
    if (!iso || iso.length < 10) return iso || '';
    return iso.slice(8, 10) + '.' + iso.slice(5, 7);
  }

  // ---------- Savings banner ----------
  function renderBanner() {
    var d = state.lastData; if (!d) return;
    var banner = $('historySavingsBanner');
    var noplan = $('historyNoPlanNote');
    var s = d.summary || {};
    if (!state.compare) { banner.hidden = true; noplan.hidden = true; return; }
    var savingsAvailable = s.savings_available !== undefined
      ? s.savings_available === true
      : s.has_plan === true;
    if (!savingsAvailable) {
      banner.hidden = true;
      noplan.hidden = false;
      var unavailableReason = String(s.savings_unavailable_reason || '');
      if (unavailableReason === 'plan_unavailable' || s.plan_available === false) {
        noplan.textContent = 'Базовый план за выбранный период недоступен — экономия не рассчитывается';
      } else if (unavailableReason === 'historical_zone_cohort_changed' || s.cohort_matches_current === false) {
        noplan.textContent = 'Состав зон изменился — сравнение экономии для этого периода недоступно';
      } else if (unavailableReason === 'actual_run_open' || s.actuals_complete === false) {
        noplan.textContent = 'Расчёт экономии появится после завершения текущего полива';
      } else {
        noplan.textContent = 'Для выбранного периода нет сопоставимого плана — сравнить не с чем';
      }
      return;
    }
    noplan.hidden = true;
    var planMin = Math.round(s.plan_minutes || 0);
    var actMin = Math.round(s.total_minutes || 0);
    var saved = planMin - actMin;
    if (saved > 0) {
      banner.hidden = false;
      banner.className = 'history-banner is-positive';
      banner.textContent = '🌱 Сэкономлено ' + saved + ' мин (план ' + planMin + ', факт ' + actMin + ')';
    } else if (saved < 0) {
      banner.hidden = false;
      banner.className = 'history-banner is-negative';
      banner.textContent = '⚠ Полив превысил план на ' + (-saved) + ' мин (план ' + planMin + ', факт ' + actMin + ')';
    } else {
      banner.hidden = false;
      banner.className = 'history-banner';
      banner.textContent = 'Полив совпал с планом (' + planMin + ' мин)';
    }
  }

  // ---------- Runs list (grouped by day, desc) ----------
  function summarizeActualRuns(runs) {
    return (Array.isArray(runs) ? runs : []).reduce(function (total, run) {
      // The backend owns accounting policy. In particular, an unconfirmed
      // aborted run stays visible in the list but must not inflate actual
      // minutes or run counts.
      if (!run || run.counts_as_actual !== true) return total;
      var minutes = Number(run.duration_min);
      total.count += 1;
      if (Number.isFinite(minutes)) total.minutes += minutes;
      return total;
    }, { count: 0, minutes: 0 });
  }

  function renderRuns() {
    var d = state.lastData; if (!d) return;
    var box = $('historyRunsList'); if (!box) return;
    var runs = d.runs || [];
    if (!runs.length) {
      box.innerHTML = '<div class="history-runs__empty">Нет запусков за выбранный период</div>';
      return;
    }
    // Group by local date.
    var groups = {};
    runs.forEach(function (r) {
      var local = utcIsoToLocalDate(r.start_utc);
      if (!groups[local]) groups[local] = [];
      groups[local].push(r);
    });
    var dates = Object.keys(groups).sort().reverse();
    var html = '';
    dates.forEach(function (date) {
      var dayRuns = groups[date];
      var actual = summarizeActualRuns(dayRuns);
      var dayMin = actual.minutes;
      var dayCount = actual.count;
      var headExtra = dayCount > 1
        ? ' · ' + dayCount + ' запуска · ' + dayMin + ' мин'
        : '';
      html += '<div class="history-runs__day-header">' + safeText(formatDateLong(date)) + headExtra + '</div>';
      dayRuns.forEach(function (r) {
        html += renderRunRow(r);
      });
    });
    box.innerHTML = html;
  }

  var SOURCE_LABELS = { program: 'Программа', manual: 'Вручную', api: 'API' };

  function renderRunRow(r) {
    // A run the relay never physically confirmed is recorded status='failed'
    // — show it distinctly ("не полито"), not as a normal interruption.
    var failed = (r.status === 'failed');
    var interrupted = !failed && ((r.status && r.status !== 'ok') || (r.duration_min === 0));
    var cls = 'history-run' + (failed ? ' is-failed' : (interrupted ? ' is-interrupted' : ''));
    var icon = failed ? '⚠️' : (interrupted ? '⏹' : '✓');
    var tStart = utcIsoToLocalTime(r.start_utc);
    var tEnd = utcIsoToLocalTime(r.end_utc);
    var time = tEnd ? (tStart + ' → ' + tEnd) : tStart;
    var dur = (r.duration_min != null) ? r.duration_min : '—';
    var zoneLabel = (r.zone_name) ? ('#' + r.zone_id + ' ' + r.zone_name) : ('#' + r.zone_id);
    var srcLabel = r.source ? (SOURCE_LABELS[r.source] || r.source) : '';
    var sourceBadge = srcLabel
      ? '<span class="history-run__source history-run__source--' + safeText(r.source) + '">' + safeText(srcLabel) + '</span>'
      : '';
    var statusBadge = failed
      ? '<span class="history-run__status history-run__status--failed">не полито</span>'
      : '';
    return '<div class="' + cls + '">'
      + '<span class="history-run__icon">' + icon + '</span>'
      + '<span class="history-run__time">' + safeText(time) + '</span>'
      + '<span class="history-run__zone">' + safeText(zoneLabel) + '</span>'
      + sourceBadge
      + statusBadge
      + '<span class="history-run__dur">' + safeText(dur) + ' мин</span>'
      + '</div>';
  }

  function utcIsoToLocalDate(s) {
    if (!s) return '';
    var d = new Date(s);
    if (isNaN(d.getTime())) return '';
    var y = d.getFullYear(); var m = String(d.getMonth() + 1).padStart(2, '0'); var dd = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + dd;
  }
  function utcIsoToLocalTime(s) {
    if (!s) return '';
    var d = new Date(s);
    if (isNaN(d.getTime())) return '';
    var h = String(d.getHours()).padStart(2, '0'); var m = String(d.getMinutes()).padStart(2, '0');
    return h + ':' + m;
  }
  var WEEKDAY_RU = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  var MONTH_RU = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
  function formatDateLong(iso) {
    if (!iso || iso.length < 10) return iso || '';
    var d = new Date(iso + 'T00:00:00');
    if (isNaN(d.getTime())) return iso;
    return WEEKDAY_RU[d.getDay()] + ', ' + d.getDate() + ' ' + MONTH_RU[d.getMonth()];
  }

  // ---------- Footer ----------
  function renderFooter() {
    var d = state.lastData; if (!d) return;
    var f = $('historyFooterStats'); if (!f) return;
    var s = d.summary || {};
    var bits = [
      (s.total_runs || 0) + ' запусков',
      Math.round(s.total_minutes || 0) + ' мин',
    ];
    if (s.has_liters) bits.push(Math.round(s.total_liters || 0) + ' л' + (s.liters_partial ? '*' : ''));
    f.textContent = bits.join(' · ');

    // CSV button only makes sense for per-zone view.
    var csvBtn = $('historyCsvBtn');
    if (csvBtn) csvBtn.hidden = (state.zoneId === 'all');
  }

  // ---------- CSV export ----------
  function onCsvClick() {
    if (state.zoneId === 'all') return;
    var url = '/api/zones/' + state.zoneId + '/history.csv?days=' + state.days;
    window.location.href = url;
  }

  // ---------- Public ----------
  window.historyModal = {
    open: open,
    openGlobal: openGlobal,
    openForZone: openForZone,
    openForGroup: openForGroup,
    close: close,
  };
})();
