// status/zone-cards.js — Zone card rendering, filtering, tabs

    var zoneGroupsCache = [];
    var currentGroupFilter = null; // null = all
    var zoneSearchQuery = '';
    var editingZoneId = null;

    function getZoneTypeInfo(icon) {
        var map = {
            '🌿': { label: 'Роторные', bg: '#e8f5e9' },
            '💧': { label: 'Капельный', bg: '#e3f2fd' },
            '🌊': { label: 'Спрей', bg: '#fff3e0' },
            '🌳': { label: 'Деревья', bg: '#f1f8e9' },
            '🌹': { label: 'Цветы', bg: '#fce4ec' },
            '🥕': { label: 'Огород', bg: '#fff8e1' },
        };
        return map[icon] || { label: icon || '—', bg: '#f5f5f5' };
    }

    function getFilteredZonesV2() {
        var zones = (zonesData || []).filter(function(z) { return z.group_id !== 999; });
        if (currentGroupFilter !== null) {
            zones = zones.filter(function(z) { return z.group_id === currentGroupFilter; });
        }
        if (zoneSearchQuery) {
            var q = zoneSearchQuery.toLowerCase();
            zones = zones.filter(function(z) {
                return (z.name || '').toLowerCase().indexOf(q) !== -1 || String(z.id).indexOf(q) !== -1;
            });
        }
        return zones;
    }

    function renderGroupTabs() {
        var c = document.getElementById('groupTabs');
        if (!c) return;
        var allZones = (zonesData || []).filter(function(z) { return z.group_id !== 999; });
        var groups = zoneGroupsCache || [];
        var runningCount = allZones.filter(function(z) { return z.state === 'on'; }).length;

        var html = '<button class="group-tab ' + (currentGroupFilter === null ? 'active' : '') + '" onclick="selectZoneGroup(null)">Все<span class="tab-count">' + allZones.length + '</span></button>';

        groups.filter(function(g) { return g.id !== 999; }).forEach(function(g) {
            var gZones = allZones.filter(function(z) { return z.group_id === g.id; });
            var gRunning = gZones.filter(function(z) { return z.state === 'on'; }).length;
            var gStatus = 'waiting';
            if (statusData && statusData.groups) {
                var sg = statusData.groups.find(function(sg) { return sg.id === g.id; });
                if (sg) gStatus = sg.status || 'waiting';
            }
            html += '<button class="group-tab ' + (currentGroupFilter === g.id ? 'active' : '') + '" onclick="selectZoneGroup(' + g.id + ')">';
            html += '<span class="tab-status ' + gStatus + '"></span>' + escapeHtml(g.name);
            html += '<span class="tab-count">' + (gRunning ? '▶' + gRunning : gZones.length) + '</span></button>';
        });
        c.innerHTML = html;

        // Update run button text
        var btn = document.getElementById('zoneRunGroupBtn');
        if (btn) {
            if (currentGroupFilter !== null) {
                var gName = '';
                groups.forEach(function(g) { if (g.id === currentGroupFilter) gName = g.name; });
                btn.textContent = '▶ ' + (gName || 'Группу');
            } else {
                btn.textContent = '▶ Запустить все';
            }
        }
    }

    function renderZoneCards() {
        var c = document.getElementById('zoneList');
        if (!c) return;
        var isAdmin = !!(statusData && statusData.is_admin);
        var zones = getFilteredZonesV2();
        var groups = zoneGroupsCache || [];
        var groupNameById = {};
        groups.forEach(function(g) { groupNameById[g.id] = g.name; });

        if (!zones.length) {
            c.innerHTML = '<div style="text-align:center;padding:30px;color:#999;font-size:14px">🔍 Зоны не найдены</div>';
            updateZoneStats(zones);
            return;
        }

        // Check if we can patch existing cards (same zone IDs in same order)
        var existingCards = c.querySelectorAll('.zone-card[data-zone-id]');
        var existingIds = [];
        existingCards.forEach(function(el) { existingIds.push(el.getAttribute('data-zone-id')); });
        var newIds = zones.map(function(z) { return String(z.id); });
        var canPatch = existingIds.length > 0 && existingIds.length === newIds.length && existingIds.every(function(id, i) { return id === newIds[i]; });
        var showSections = currentGroupFilter === null && !zoneSearchQuery;

        if (canPatch) {
            // DOM PATCHING: update existing cards without destroying them
            zones.forEach(function(z) {
                var card = document.getElementById('zcard-' + z.id);
                if (!card) return;
                var t = getZoneTypeInfo(z.icon);
                var isRunning = z.state === 'on';
                var statusCls = isRunning ? 'zs-running' : 'zs-enabled';
                var gName2 = groupNameById[z.group_id] || '';

                // Update card class (without losing 'open')
                var isOpen = card.classList.contains('open');
                card.className = 'zone-card ' + statusCls + (isOpen ? ' open' : '');

                // Patch next-watering text (in header)
                var nextEl = card.querySelector('.zc-next');
                if (nextEl) {
                    if (isRunning) {
                        var valEl = nextEl.querySelector('.zc-next-val');
                        var lblEl = nextEl.querySelector('.zc-next-lbl');
                        if (valEl) { valEl.textContent = '⏱'; valEl.style.color = '#2196f3'; valEl.style.fontSize = ''; }
                        if (lblEl) lblEl.textContent = 'полив';
                    } else {
                        var nextText = z._nextWatering || '';
                        var valEl2 = nextEl.querySelector('.zc-next-val');
                        var lblEl2 = nextEl.querySelector('.zc-next-lbl');
                        if (nextText && nextText !== 'Никогда' && nextText !== '—') {
                            var parts = nextText.split(' ');
                            var timeOnly = parts.length >= 2 ? parts[1].slice(0, 5) : nextText.slice(0, 5);
                            if (valEl2) { valEl2.textContent = timeOnly; valEl2.style.color = ''; valEl2.style.fontSize = ''; }
                            if (lblEl2) lblEl2.textContent = 'след.';
                        } else if (nextText === 'Никогда') {
                            if (valEl2) { valEl2.textContent = '—'; valEl2.style.color = '#ccc'; valEl2.style.fontSize = '11px'; }
                            if (lblEl2) lblEl2.textContent = 'нет';
                        }
                    }
                }

                // Patch duration badge
                var badge = document.getElementById('zbadge-' + z.id);
                if (badge) badge.textContent = z.duration + ' мин';

                // Handle running state transition
                var existingRunning = card.querySelector('.zc-running');
                var existingProgress = card.querySelector('.zc-progress');
                if (isRunning) {
                    // If timer is already ticking (has remaining seconds), DON'T touch it
                    var timerEl = document.getElementById('ztimer-' + z.id);
                    var hasActiveTimer = timerEl && timerEl.dataset.remainingSeconds && Number(timerEl.dataset.remainingSeconds) > 0;
                    if (!existingRunning) {
                        // Zone just started — insert running block
                        var mainEl = card.querySelector('.zone-card-main');
                        if (mainEl) {
                            var _timerText = '--:--';
                            var _pctText = '';
                            var _progWidth = '0%';
                            var _remain = 0;
                            if (z.planned_end_time && z.watering_start_time) {
                                var _endD = parseDate(z.planned_end_time);
                                var _startD = parseDate(z.watering_start_time);
                                var _endMs = _endD ? _endD.getTime() : 0;
                                var _startMs = _startD ? _startD.getTime() : 0;
                                _remain = _endMs ? Math.max(0, Math.floor((_endMs - Date.now()) / 1000)) : 0;
                                var _total = (_endMs && _startMs) ? Math.max(60, Math.floor((_endMs - _startMs) / 1000)) : (z.duration || 10) * 60;
                                _timerText = formatSeconds(_remain);
                                var _pct = Math.min(100, Math.max(0, ((_total - _remain) / _total) * 100));
                                _pctText = Math.round(_pct) + '%';
                                _progWidth = _pct + '%';
                            }
                            var runDiv = document.createElement('div');
                            runDiv.className = 'zc-running';
                            runDiv.innerHTML = '<span class="zc-running-dot"></span><span>Осталось</span><span class="zc-running-timer" id="ztimer-' + z.id + '" data-remaining-seconds="' + (_remain != null ? _remain : '') + '">' + _timerText + '</span><span class="zc-running-pct" id="zpct-' + z.id + '">' + _pctText + '</span>';
                            mainEl.after(runDiv);
                            var progDiv = document.createElement('div');
                            progDiv.className = 'zc-progress';
                            progDiv.innerHTML = '<div class="zc-progress-bar" id="zprog-' + z.id + '" style="width:' + _progWidth + '"></div>';
                            runDiv.after(progDiv);
                            initZoneTimer(z);
                        }
                    }
                    // If timer exists but no active countdown, re-init with server data
                    if (existingRunning && !hasActiveTimer) {
                        initZoneTimer(z);
                    }
                } else {
                    // Zone stopped — remove running block if present
                    if (existingRunning) existingRunning.remove();
                    if (existingProgress) existingProgress.remove();
                }

                // Patch expanded details
                var detailGrid = card.querySelector('.zc-detail-grid');
                if (detailGrid) {
                    var items = detailGrid.querySelectorAll('.zc-detail-item .zc-d-value');
                    if (items.length >= 4) {
                        items[0].textContent = z.duration + ' мин';
                        items[1].textContent = gName2;
                        var nextFull = z._nextWatering || '—';
                        items[2].textContent = nextFull;
                        items[2].className = 'zc-d-value' + (nextFull !== '—' && nextFull !== 'Никогда' ? ' highlight' : '');
                        items[3].textContent = z.last_watering_time ? z.last_watering_time.replace('T',' ').slice(0,16) : '—';
                    }
                }

                // Patch action buttons (run/stop state may change)
                var actions = card.querySelector('.zc-actions');
                if (actions) {
                    var emergency = !!(statusData && statusData.emergency_stop);
                    var startAction = emergency ? "showNotification('Аварийная остановка активна','warning')" : "toggleZoneRun(" + z.id + ")";
                    var actHtml = '';
                    if (isRunning) {
                        actHtml = '<button class="zc-btn-stop" onclick="event.stopPropagation();' + startAction + '">⏹ Стоп</button>';
                    } else {
                        actHtml = '<button class="zc-btn-run" onclick="event.stopPropagation();showRunPopup(' + z.id + ',' + z.duration + ')">▶ Запустить</button>';
                    }
                    if (isAdmin) {
                        actHtml += '<button class="zc-btn-edit" onclick="event.stopPropagation();openZoneSheet(' + z.id + ')">✏️</button>';
                    }
                    actions.innerHTML = actHtml;
                }
            });
            updateZoneStats(zones);
            // Signal render complete for perf
            try{ window.dispatchEvent(new CustomEvent('zones-rendered')); }catch(e){}
            return;
        }

        // FULL RENDER: first render or zone list changed
        // Preserve open accordion state across re-renders
        var openIds = {};
        c.querySelectorAll('.zone-card.open').forEach(function(el) {
            var zid = el.getAttribute('data-zone-id');
            if (zid) openIds[zid] = true;
        });

        var html = '';
        var lastGroupId = null;

        zones.forEach(function(z) {
            if (showSections && z.group_id !== lastGroupId) {
                var gName = groupNameById[z.group_id] || ('Группа ' + z.group_id);
                var gCount = (zonesData || []).filter(function(zz) { return zz.group_id === z.group_id && zz.group_id !== 999; }).length;
                html += '<div class="group-section"><span class="group-section-name">' + escapeHtml(gName) + '</span><span class="group-section-line"></span><span class="group-section-count">' + gCount + ' зон</span></div>';
                lastGroupId = z.group_id;
            }

            var t = getZoneTypeInfo(z.icon);
            var isRunning = z.state === 'on';
            var statusCls = isRunning ? 'zs-running' : 'zs-enabled';
            var gName2 = groupNameById[z.group_id] || '';

            // Next watering
            var nextHtml = '';
            if (isRunning) {
                nextHtml = '<div class="zc-next"><div class="zc-next-val" style="color:#2196f3">⏱</div><div class="zc-next-lbl">полив</div></div>';
            } else {
                var nextText = z._nextWatering || '';
                if (nextText && nextText !== 'Никогда' && nextText !== '—') {
                    var parts = nextText.split(' ');
                    var timeOnly = parts.length >= 2 ? parts[1].slice(0, 5) : nextText.slice(0, 5);
                    nextHtml = '<div class="zc-next"><div class="zc-next-val">' + timeOnly + '</div><div class="zc-next-lbl">след.</div></div>';
                } else if (nextText === 'Никогда') {
                    nextHtml = '<div class="zc-next"><div class="zc-next-val" style="color:#ccc;font-size:11px">—</div><div class="zc-next-lbl">нет</div></div>';
                }
            }

            // Running info — compute timer inline to avoid --:-- flash on re-render
            var runningHtml = '';
            if (isRunning) {
                var _timerText = '--:--';
                var _pctText = '';
                var _progWidth = '0%';
                if (z.planned_end_time && z.watering_start_time) {
                    var _endD = parseDate(z.planned_end_time);
                    var _startD = parseDate(z.watering_start_time);
                    var _endMs = _endD ? _endD.getTime() : 0;
                    var _startMs = _startD ? _startD.getTime() : 0;
                    var _remain = _endMs ? Math.max(0, Math.floor((_endMs - Date.now()) / 1000)) : 0;
                    var _total = (_endMs && _startMs) ? Math.max(60, Math.floor((_endMs - _startMs) / 1000)) : (z.duration || 10) * 60;
                    _timerText = formatSeconds(_remain);
                    var _pct = Math.min(100, Math.max(0, ((_total - _remain) / _total) * 100));
                    _pctText = Math.round(_pct) + '%';
                    _progWidth = _pct + '%';
                }
                runningHtml = '<div class="zc-running"><span class="zc-running-dot"></span><span>Осталось</span><span class="zc-running-timer" id="ztimer-' + z.id + '" data-remaining-seconds="' + (_remain != null ? _remain : '') + '">' + _timerText + '</span><span class="zc-running-pct" id="zpct-' + z.id + '">' + _pctText + '</span></div>';
                runningHtml += '<div class="zc-progress"><div class="zc-progress-bar" id="zprog-' + z.id + '" style="width:' + _progWidth + '"></div></div>';
            }

            var emergency = !!(statusData && statusData.emergency_stop);
            var startAction = emergency ? "showNotification('Аварийная остановка активна','warning')" : "toggleZoneRun(" + z.id + ")";

            html += '<div class="zone-card ' + statusCls + '" id="zcard-' + z.id + '" data-zone-id="' + z.id + '">';
            html += '<div class="zone-card-main" onclick="toggleZoneCard(' + z.id + ')">';
            html += '<div class="zc-icon" style="background:' + t.bg + '">' + (z.icon || '🌿') + '</div>';
            html += '<div class="zc-info"><div class="zc-name">#' + z.id + ' ' + escapeHtml(z.name || '') + '</div>';
            html += '<div class="zc-meta"><span>' + t.label + '</span><span style="color:#ddd">·</span><span class="zc-dur-badge" id="zbadge-' + z.id + '">' + z.duration + ' мин</span>';
            if (!showSections) html += '<span style="color:#ddd">·</span><span>' + escapeHtml(gName2) + '</span>';
            html += '</div></div>';
            html += nextHtml;
            html += '<span class="zc-chevron">▼</span>';
            html += '</div>'; // end zone-card-main

            html += runningHtml;

            // Expanded
            html += '<div class="zc-expanded">';
            html += '<div class="zc-detail-grid">';
            html += '<div class="zc-detail-item"><div class="zc-d-label">Длительность</div><div class="zc-d-value">' + z.duration + ' мин</div></div>';
            html += '<div class="zc-detail-item"><div class="zc-d-label">Группа</div><div class="zc-d-value">' + escapeHtml(gName2) + '</div></div>';
            var nextFull = z._nextWatering || '—';
            html += '<div class="zc-detail-item"><div class="zc-d-label">След. полив</div><div class="zc-d-value ' + (nextFull !== '—' && nextFull !== 'Никогда' ? 'highlight' : '') + '">' + nextFull + '</div></div>';
            html += '<div class="zc-detail-item"><div class="zc-d-label">Послед. полив</div><div class="zc-d-value">' + (z.last_watering_time ? z.last_watering_time.replace('T',' ').slice(0,16) : '—') + '</div></div>';
            html += '</div>'; // detail-grid

            html += '<div class="zc-actions">';
            if (isRunning) {
                html += '<button class="zc-btn-stop" onclick="event.stopPropagation();' + startAction + '">⏹ Стоп</button>';
            } else {
                html += '<button class="zc-btn-run" onclick="event.stopPropagation();showRunPopup(' + z.id + ',' + z.duration + ')">▶ Запустить</button>';
            }
            if (isAdmin) {
                html += '<button class="zc-btn-edit" onclick="event.stopPropagation();openZoneSheet(' + z.id + ')">✏️</button>';
            }
            html += '</div>';

            html += '</div>'; // zc-expanded
            html += '</div>'; // zone-card
        });

        c.innerHTML = html;
        // Restore open accordion state
        Object.keys(openIds).forEach(function(zid) {
            var el = document.getElementById('zcard-' + zid);
            if (el) el.classList.add('open');
        });
        updateZoneStats(zones);

        // Init running timers
        zones.forEach(function(z) {
            if (z.state === 'on') initZoneTimer(z);
        });
        // Signal render complete for perf
        try{ window.dispatchEvent(new CustomEvent('zones-rendered')); }catch(e){}
    }

    function updateZoneStats(zones) {
        var all = (zonesData || []).filter(function(z) { return z.group_id !== 999; });
        var running = all.filter(function(z) { return z.state === 'on'; }).length;
        var groups = (zoneGroupsCache || []).filter(function(g) { return g.id !== 999; });
        var totalWater = 0;
        all.forEach(function(z) { if (z.last_total_liters > 0) totalWater += z.last_total_liters; });

        var el;
        el = document.getElementById('statZonesTotal'); if (el) el.textContent = all.length;
        el = document.getElementById('statZonesActive'); if (el) el.textContent = running;
        el = document.getElementById('statZonesGroups'); if (el) el.textContent = groups.length;
        el = document.getElementById('statZonesWater'); if (el) el.textContent = totalWater > 0 ? Math.round(totalWater) : '—';
        // Also update old zones-count for backward compat
        el = document.getElementById('zones-count'); if (el) el.textContent = all.length;
    }

    function initZoneTimer(zone) {
        function applyTimer(remain) {
            var total;
            if (zone.planned_end_time && zone.watering_start_time) {
                var endD = parseDate(zone.planned_end_time);
                var startD = parseDate(zone.watering_start_time);
                total = (endD && startD) ? Math.max(60, Math.floor((endD.getTime() - startD.getTime()) / 1000)) : (zone.duration || 10) * 60;
            } else {
                total = (zone.duration || 10) * 60;
            }
            var pct = Math.min(100, Math.max(0, ((total - remain) / total) * 100));
            var timerEl = document.getElementById('ztimer-' + zone.id);
            var pctEl = document.getElementById('zpct-' + zone.id);
            var progEl = document.getElementById('zprog-' + zone.id);
            if (timerEl) { timerEl.textContent = formatSeconds(remain); timerEl.dataset.remainingSeconds = String(remain); }
            if (pctEl) pctEl.textContent = Math.round(pct) + '%';
            if (progEl) progEl.style.width = pct + '%';
        }
        // Try local calc first (instant)
        try {
            if (zone.planned_end_time) {
                var endD = parseDate(zone.planned_end_time);
                if (endD) {
                    var remain = Math.max(0, Math.floor((endD.getTime() - Date.now()) / 1000));
                    if (remain > 0) { applyTimer(remain); return; }
                }
            }
        } catch(e) {}
        // Fallback: fetch
        try {
            fetch('/api/zones/' + zone.id + '/watering-time?ts=' + Date.now(), { cache: 'no-store' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data || !data.success || !data.is_watering) return;
                var remain = data.remaining_seconds || (data.remaining_time * 60);
                applyTimer(remain);
            }).catch(function() {});
        } catch(e) {}
    }

    // Accordion toggle
    function toggleZoneCard(id) {
        var card = document.getElementById('zcard-' + id);
        if (card) card.classList.toggle('open');
    }

    // Group selection
    function selectZoneGroup(groupId) {
        currentGroupFilter = groupId;
        renderGroupTabs();
        renderZoneCards();
    }

    // Search
    function toggleZoneSearch() {
        var wrap = document.getElementById('zoneSearchWrap');
        if (!wrap) return;
        var visible = wrap.style.display !== 'none';
        wrap.style.display = visible ? 'none' : 'block';
        if (!visible) document.getElementById('searchInput').focus();
        else { zoneSearchQuery = ''; document.getElementById('searchInput').value = ''; renderZoneCards(); }
    }

    function filterZonesBySearch() {
        zoneSearchQuery = (document.getElementById('searchInput') || {}).value || '';
        renderZoneCards();
    }
