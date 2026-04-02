// status/timers.js — Timer countdown, drift correction, date parsing

    // Cross-browser date parsing (Safari doesn't support "YYYY-MM-DD HH:MM:SS" format)
    function parseDate(s) {
        if (!s) return null;
        var d = new Date(String(s).replace(' ', 'T'));
        return isNaN(d.getTime()) ? null : d;
    }

    function formatSeconds(total) {
        const sec = Math.max(0, Math.floor(total));
        const mm = String(Math.floor(sec / 60)).padStart(2, '0');
        const ss = String(sec % 60).padStart(2, '0');
        return `${mm}:${ss}`;
    }

    function tickCountdowns() {
        // Tick zone card timers
        document.querySelectorAll('.zc-running-timer').forEach(function(el) {
            var val = el.dataset.remainingSeconds;
            if (!val) return;
            var sec = Number(val);
            if (isNaN(sec) || sec <= 0) { el.textContent = '00:00'; el.dataset.remainingSeconds = ''; return; }
            sec--;
            // Drift correction: compare with real planned_end_time (using server-synced time)
            var zid = el.id.replace('ztimer-', '');
            var zone = (zonesData || []).find(function(z) { return String(z.id) === zid; });
            if (zone && zone.planned_end_time) {
                var endD = parseDate(zone.planned_end_time);
                if (endD) {
                    var _dNow = Date.now() + (_serverTimeOffset || 0);
                    var realRemain = Math.max(0, Math.floor((endD.getTime() - _dNow) / 1000));
                    if (Math.abs(sec - realRemain) > 2) sec = realRemain;
                }
            }
            el.dataset.remainingSeconds = String(sec);
            el.textContent = formatSeconds(sec);
            // Update progress bar
            var progEl = document.getElementById('zprog-' + zid);
            if (progEl && zone) {
                var total;
                if (zone.planned_end_time && zone.watering_start_time) {
                    var endMs2 = parseDate(zone.planned_end_time);
                    var startMs2 = parseDate(zone.watering_start_time);
                    total = (endMs2 && startMs2) ? Math.max(60, Math.floor((endMs2.getTime() - startMs2.getTime()) / 1000)) : (zone.duration || 10) * 60;
                } else {
                    total = (zone.duration || 10) * 60;
                }
                var pct = Math.min(100, Math.max(0, ((total - sec) / total) * 100));
                progEl.style.width = pct + '%';
                var pctEl = document.getElementById('zpct-' + zid);
                if (pctEl) pctEl.textContent = Math.round(pct) + '%';
            }
        });
        // Tick group timers
        const spans = document.querySelectorAll('.group-timer');
        spans.forEach(span => {
            const val = span.dataset.remainingSeconds;
            if (!val) return;
            let sec = Number(val);
            if (Number.isNaN(sec) || sec <= 0) {
                span.textContent = '00:00';
                span.dataset.remainingSeconds = '';
                // Попросим актуальный статус группы и перерисуем её карточку без полной перезагрузки страницы
                const gid = span.dataset.groupId;
                if (gid) refreshSingleGroup(parseInt(gid, 10));
                return;
            }
            sec = sec - 1;
            // Drift correction for group timers
            var gZoneId = span.dataset.zoneId;
            if (gZoneId) {
                var gZone = (zonesData || []).find(function(z) { return String(z.id) === String(gZoneId); });
                if (gZone && gZone.planned_end_time) {
                    var gEndD = parseDate(gZone.planned_end_time);
                    if (gEndD) {
                        var _gDNow = Date.now() + (_serverTimeOffset || 0);
                        var gRealRemain = Math.max(0, Math.floor((gEndD.getTime() - _gDNow) / 1000));
                        if (Math.abs(sec - gRealRemain) > 2) sec = gRealRemain;
                    }
                }
            }
            span.dataset.remainingSeconds = String(sec);
            span.textContent = formatSeconds(sec);
        });
    }

    // Recalculate ALL timers from real time (planned_end_time vs Date.now()).
    // Called on visibilitychange (return from background) to fix frozen-timer drift.
    // This is a SEPARATE function — tickCountdowns() is NOT modified.
    function recalcTimersFromRealTime() {
        var nowMs = Date.now();
        // --- Zone card timers ---
        document.querySelectorAll('.zc-running-timer').forEach(function(el) {
            var zid = el.id.replace('ztimer-', '');
            var zone = (zonesData || []).find(function(z) { return String(z.id) === zid; });
            if (!zone || !zone.planned_end_time) return;
            var endD = parseDate(zone.planned_end_time);
            if (!endD) return;
            var remain = Math.max(0, Math.floor((endD.getTime() - nowMs) / 1000));
            el.dataset.remainingSeconds = String(remain);
            el.textContent = formatSeconds(remain);
            // Update progress bar
            var progEl = document.getElementById('zprog-' + zid);
            if (progEl && zone.watering_start_time) {
                var startD = parseDate(zone.watering_start_time);
                if (startD) {
                    var total = Math.max(60, Math.floor((endD.getTime() - startD.getTime()) / 1000));
                    var pct = Math.min(100, Math.max(0, ((total - remain) / total) * 100));
                    progEl.style.width = pct + '%';
                    var pctEl = document.getElementById('zpct-' + zid);
                    if (pctEl) pctEl.textContent = Math.round(pct) + '%';
                }
            }
        });
        // --- Group timers ---
        document.querySelectorAll('.group-timer').forEach(function(span) {
            var gZoneId = span.dataset.zoneId;
            if (!gZoneId) return;
            var zone = (zonesData || []).find(function(z) { return String(z.id) === String(gZoneId); });
            if (!zone || !zone.planned_end_time) return;
            var endD = parseDate(zone.planned_end_time);
            if (!endD) return;
            var remain = Math.max(0, Math.floor((endD.getTime() - nowMs) / 1000));
            span.dataset.remainingSeconds = String(remain);
            span.textContent = formatSeconds(remain);
        });
    }
