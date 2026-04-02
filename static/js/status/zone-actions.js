// status/zone-actions.js — Zone run/stop, duration popup, edit sheet

    // Run/stop zone
    function toggleZoneRun(id) {
        showLoading(((zonesData||[]).find(function(z){return z.id===id;})||{}).state==='on' ? 'Остановка...' : 'Запуск...');
        var z = (zonesData || []).find(function(z) { return z.id === id; });
        if (!z) return;
        var wantOn = z.state !== 'on';
        var url = wantOn ? '/api/zones/' + id + '/mqtt/start' : '/api/zones/' + id + '/mqtt/stop';
        // Optimistic: set state + times BEFORE fetch for instant timer
        z.state = wantOn ? 'on' : 'off';
        if (wantOn) {
            z.watering_start_time = new Date().toISOString().slice(0,19).replace('T',' ');
            z.planned_end_time = new Date(Date.now() + (z.duration||10) * 60 * 1000).toISOString().slice(0,19).replace('T',' ');
        }
        renderZoneCards();
        renderGroupTabs();
        fetch(url, { method: 'POST' }).then(function(r) { return r.json(); }).then(function(data) {
            if (data && data.success) {
                hideLoading();
                showZoneToast(wantOn ? '▶ Зона #' + id + ' запущена' : '⏹ Зона #' + id + ' остановлена', wantOn ? 'success' : '');
                // Light refresh status (groups) after 2 sec
                setTimeout(function() { loadStatusData(); }, 2000);
            } else {
                z.state = wantOn ? 'off' : 'on';
                renderZoneCards();
                showZoneToast((data && data.message) || 'Ошибка', 'error');
            }
        }).catch(function() {
            hideLoading();
            z.state = wantOn ? 'off' : 'on';
            renderZoneCards();
            showZoneToast('Ошибка сети', 'error');
        });
    }

    // Duration +/-
    var durDebounceTimers = {};
    function changeZoneDur(id, delta) {
        var z = (zonesData || []).find(function(z) { return z.id === id; });
        if (!z) return;
        z.duration = Math.max(1, Math.min(120, (z.duration || 10) + delta));
        var el = document.getElementById('zdur-' + id);
        if (el) el.textContent = z.duration;
        var badge = document.getElementById('zbadge-' + id);
        if (badge) badge.textContent = z.duration + ' мин';
        // Debounce API call
        clearTimeout(durDebounceTimers[id]);
        durDebounceTimers[id] = setTimeout(function() {
            api.put('/api/zones/' + id, { duration: z.duration }).catch(function() {});
        }, 500);
    }

    // Run selected group
    function runSelectedGroup() {
        var gid = currentGroupFilter;
        var gName = 'все группы';
        if (gid) {
            var g = (zoneGroupsCache || []).find(function(g){ return g.id === gid; });
            gName = g ? g.name : 'Группа';
        }
        // Show popup with two options
        showGroupRunPopup(gid, gName);
    }
    
    function showGroupRunPopup(gid, gName) {
        runPopupGroupId = gid;
        runPopupZoneId = null;
        _runPopupAllGroups = !gid;
        var title = gid ? '▶ ' + gName : '▶ Все группы';
        document.getElementById('runPopupTitle').textContent = title;
        runPopupDur = 15;
        // Show "with defaults" button for group
        var defBtn = document.getElementById('runPopupDefaults');
        if (defBtn) defBtn.style.display = 'block';
        initDialTicks();
        updateDial();
        document.getElementById('runPopupOverlay').classList.add('show');
        document.getElementById('runPopup').classList.add('show');
        setTimeout(initDialDrag, 100);
    }
    
    function runGroupWithDefaults() {
        // Run group with existing zone durations (no dial)
        var gid = currentGroupFilter;
        if (gid) {
            fetch('/api/groups/' + gid + '/start-from-first', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                showZoneToast(data && data.success ? '▶ Группа запущена' : ((data && data.message) || 'Ошибка'), data && data.success ? 'success' : 'error');
                setTimeout(function() { loadZonesData().then(function(){ return loadStatusData(); }); }, 1500);
            }).catch(function() { showZoneToast('Ошибка', 'error'); });
        } else {
            (zoneGroupsCache || []).filter(function(g){return g.id !== 999;}).forEach(function(g) {
                fetch('/api/groups/' + g.id + '/start-from-first', { method: 'POST' }).catch(function() {});
            });
            showZoneToast('▶ Все группы запущены', 'success');
            setTimeout(function() { loadZonesData().then(function(){ return loadStatusData(); }); }, 1500);
        }
    }

    // Edit sheet
    function openZoneSheet(id) {
        editingZoneId = id;
        var z = (zonesData || []).find(function(z) { return z.id === id; });
        if (!z) return;
        document.getElementById('sheetTitle').textContent = '✏️ #' + z.id + ' ' + z.name;
        document.getElementById('editZoneName').value = z.name || '';
        document.getElementById('editZoneDuration').value = z.duration || 10;
        document.getElementById('editZoneIcon').value = z.icon || '🌿';
        // Populate groups
        var gs = document.getElementById('editZoneGroup');
        gs.innerHTML = (zoneGroupsCache || []).map(function(g) {
            return '<option value="' + g.id + '"' + (g.id === z.group_id ? ' selected' : '') + '>' + escapeHtml(g.name) + '</option>';
        }).join('');
        document.getElementById('sheetOverlay').classList.add('show');
        document.getElementById('bottomSheet').classList.add('show');
    }

    function closeZoneSheet() {
        document.getElementById('sheetOverlay').classList.remove('show');
        document.getElementById('bottomSheet').classList.remove('show');
        editingZoneId = null;
    }

    function saveZoneEdit() {
        if (!editingZoneId) return;
        var payload = {
            name: document.getElementById('editZoneName').value,
            duration: parseInt(document.getElementById('editZoneDuration').value) || 10,
            icon: document.getElementById('editZoneIcon').value,
            group_id: parseInt(document.getElementById('editZoneGroup').value) || 1,
        };
        api.put('/api/zones/' + editingZoneId, payload).then(function(data) {
            closeZoneSheet();
            showZoneToast('✅ Зона сохранена', 'success');
            loadZonesData();
        }).catch(function() { showZoneToast('Ошибка сохранения', 'error'); });
    }

    // Run Duration Popup with Circular Dial
    var runPopupZoneId = null;
    var runPopupGroupId = null;
    var _runPopupAllGroups = false;
    var runPopupDur = 10;
    var MAX_DUR = 120;
    var DIAL_R = 85;
    var DIAL_CIRC = 2 * Math.PI * DIAL_R;

    function updateDial() {
        var frac = runPopupDur / MAX_DUR;
        var arc = document.getElementById('dialArc');
        var handle = document.getElementById('dialHandle');
        var valEl = document.getElementById('dialValue');
        if (arc) arc.setAttribute('stroke-dashoffset', String(DIAL_CIRC * (1 - frac)));
        if (valEl) valEl.textContent = runPopupDur;
        if (handle) {
            var angle = frac * 360 - 90;
            var rad = angle * Math.PI / 180;
            var hx = 100 + DIAL_R * Math.cos(rad);
            var hy = 100 + DIAL_R * Math.sin(rad);
            handle.setAttribute('cx', String(hx));
            handle.setAttribute('cy', String(hy));
        }
    }

    function initDialTicks() {
        var g = document.getElementById('dialTicks');
        if (!g) return;
        var html = '';
        for (var i = 0; i <= 120; i += 10) {
            var angle = (i / MAX_DUR) * 360 - 90;
            var rad = angle * Math.PI / 180;
            var x1 = 100 + 72 * Math.cos(rad), y1 = 100 + 72 * Math.sin(rad);
            var x2 = 100 + 78 * Math.cos(rad), y2 = 100 + 78 * Math.sin(rad);
            var tx = 100 + 65 * Math.cos(rad), ty = 100 + 65 * Math.sin(rad);
            html += '<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="#bbb" stroke-width="1.5"/>';
            if (i > 0 && i % 30 === 0) html += '<text x="'+tx+'" y="'+ty+'" text-anchor="middle" dominant-baseline="central" font-size="10" fill="#999">'+i+'</text>';
        }
        g.innerHTML = html;
    }

    function initDialDrag() {
        var svg = document.getElementById('dialSvg');
        if (!svg) return;
        var dragging = false;
        function angleFromEvent(e) {
            var rect = svg.getBoundingClientRect();
            var cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
            var clientX = e.touches ? e.touches[0].clientX : e.clientX;
            var clientY = e.touches ? e.touches[0].clientY : e.clientY;
            var angle = Math.atan2(clientY - cy, clientX - cx) * 180 / Math.PI + 90;
            if (angle < 0) angle += 360;
            return angle;
        }
        function onMove(e) {
            if (!dragging) return;
            e.preventDefault();
            var angle = angleFromEvent(e);
            var dur = Math.round((angle / 360) * MAX_DUR);
            runPopupDur = Math.max(1, Math.min(MAX_DUR, dur));
            updateDial();
        }
        svg.addEventListener('mousedown', function(e) { dragging = true; onMove(e); });
        svg.addEventListener('touchstart', function(e) { dragging = true; onMove(e); }, {passive:false});
        document.addEventListener('mousemove', onMove);
        document.addEventListener('touchmove', onMove, {passive:false});
        document.addEventListener('mouseup', function() { dragging = false; });
        document.addEventListener('touchend', function() { dragging = false; });
    }

    function showRunPopup(zoneId, defaultDur) {
        runPopupZoneId = zoneId;
        runPopupGroupId = null;
        _runPopupAllGroups = false;
        runPopupDur = defaultDur || 10;
        var z = (zonesData || []).find(function(z){ return z.id === zoneId; });
        var title = z ? '▶ #' + z.id + ' ' + z.name : '▶ Запустить';
        document.getElementById('runPopupTitle').textContent = title;
        // Hide "with defaults" button for single zone
        var defBtn = document.getElementById('runPopupDefaults');
        if (defBtn) defBtn.style.display = 'none';
        initDialTicks();
        updateDial();
        document.getElementById('runPopupOverlay').classList.add('show');
        document.getElementById('runPopup').classList.add('show');
        setTimeout(initDialDrag, 100);
    }
    function closeRunPopup() {
        document.getElementById('runPopupOverlay').classList.remove('show');
        document.getElementById('runPopup').classList.remove('show');
        runPopupZoneId = null;
        _runPopupAllGroups = false;
    }
    function setRunDur(val) {
        runPopupDur = val;
        updateDial();
    }
    function confirmRun() {
        // _runPopupAllGroups flag: true when "all groups" was selected (gid=null)
        if (!runPopupZoneId && !runPopupGroupId && !_runPopupAllGroups) return;
        var dur = runPopupDur;
        var savedZoneId = runPopupZoneId;
        var savedGroupId = runPopupGroupId;
        var savedAllGroups = _runPopupAllGroups;
        closeRunPopup();
        
        if (savedGroupId || savedAllGroups) {
            // Group run: pass override_duration to API (does NOT change base durations in DB)
            if (savedAllGroups && !savedGroupId) {
                // All groups: start each group with override
                showLoading('Запуск всех групп...');
                var allGroups = (zoneGroupsCache || []).filter(function(g) { return g.id !== 999; });
                Promise.all(allGroups.map(function(g) {
                    return fetch('/api/groups/' + g.id + '/start-from-first', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({override_duration: dur})
                    }).catch(function() {});
                })).then(function() {
                    hideLoading();
                    showZoneToast('▶ Все группы запущены на ' + dur + ' мин', 'success');
                    setTimeout(function() { loadZonesData().then(function(){ return loadStatusData(); }); }, 1500);
                });
                return;
            }
            var gid = savedGroupId;
            var groupZones = (zonesData || []).filter(function(z) { return z.group_id === gid && z.group_id !== 999; });
            // Optimistic: set local times for instant timer display
            groupZones.forEach(function(z) {
                z.state = 'on';
                z.watering_start_time = new Date().toISOString().slice(0,19).replace('T',' ');
                z.planned_end_time = new Date(Date.now() + dur * 60 * 1000).toISOString().slice(0,19).replace('T',' ');
            });
            renderZoneCards();
            renderGroupTabs();
            fetch('/api/groups/' + gid + '/start-from-first', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({override_duration: dur})
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                showZoneToast(data && data.success ? '▶ Группа запущена' : 'Ошибка', data && data.success ? 'success' : 'error');
                setTimeout(function() { loadZonesData().then(function(){ return loadStatusData(); }); }, 1500);
            });
            return;
        }
        
        // Single zone run — duration override (one-time, doesn't change base)
        var id = savedZoneId;
        var z = (zonesData || []).find(function(z){ return z.id === id; });
        var wasRunning = z && z.state === 'on';
        
        // If already running — stop first, then restart
        showLoading('Запуск зоны #' + id + '...');
        var startFn = function() {
            // Optimistic: set state + times BEFORE fetch for instant timer
            if (z) {
                z.state = 'on';
                z.watering_start_time = new Date().toISOString().slice(0,19).replace('T',' ');
                z.planned_end_time = new Date(Date.now() + dur * 60 * 1000).toISOString().slice(0,19).replace('T',' ');
            }
            renderZoneCards();
            renderGroupTabs();
            fetch('/api/zones/' + id + '/mqtt/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({duration: dur}) })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data && data.success) {
                    hideLoading();
                    showZoneToast('▶ #' + id + ' запущена на ' + dur + ' мин', 'success');
                    // Refresh timer (server times may differ slightly)
                    initZoneTimer(z);
                    setTimeout(function() { loadStatusData(); }, 2000);
                } else {
                    if (z) z.state = 'off';
                    renderZoneCards();
                    hideLoading();
                    showZoneToast((data && data.message) || 'Ошибка', 'error');
                }
            }).catch(function() { hideLoading(); showZoneToast('Ошибка сети', 'error'); });
        };
        
        if (wasRunning) {
            fetch('/api/zones/' + id + '/mqtt/stop', { method: 'POST' })
            .then(function() { return new Promise(function(r) { setTimeout(r, 500); }); })
            .then(startFn);
        } else {
            startFn();
        }
    }
    function confirmRunWithDefaults() {
        showLoading('Запуск группы...');
        var gid = runPopupGroupId;
        closeRunPopup();
        if (gid) {
            fetch('/api/groups/' + gid + '/start-from-first', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                hideLoading();
                showZoneToast(data && data.success ? '▶ Группа запущена с настройками зон' : 'Ошибка', data && data.success ? 'success' : 'error');
                setTimeout(function() { loadZonesData().then(function(){ return loadStatusData(); }); }, 1500);
            });
        } else {
            (zoneGroupsCache || []).filter(function(g){return g.id !== 999;}).forEach(function(g) {
                fetch('/api/groups/' + g.id + '/start-from-first', { method: 'POST' }).catch(function(){});
            });
            hideLoading();
            showZoneToast('▶ Все группы запущены', 'success');
            setTimeout(function() { loadZonesData().then(function(){ return loadStatusData(); }); }, 1500);
        }
    }
