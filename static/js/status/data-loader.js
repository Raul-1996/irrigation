// status/data-loader.js — Data fetching, polling, connection state

    // Загрузка и обновление данных статуса
    let statusData = null;
    let zonesData = [];
    let connectionError = false;
    let mqttNoServers = false;
    let mqttNoConnection = false;
    let envProbeTimer = null;
    let envProbeAttempts = 0;
    
    // Функция обновления времени (локальное, без fetch)
    var _serverTimeOffset = 0;
    async function syncServerTime() {
        try {
            const r = await fetch('/api/server-time?ts=' + Date.now(), { cache: 'no-store' });
            const j = await r.json();
            if (j && j.now_iso) {
                var serverMs = new Date(j.now_iso).getTime();
                _serverTimeOffset = serverMs - Date.now();
            }
        } catch (e) {}
    }
    function updateDateTime() {
        var now = new Date(Date.now() + _serverTimeOffset);
        var pad = function(n){ return String(n).padStart(2,'0'); };
        var dt = now.getFullYear()+'-'+pad(now.getMonth()+1)+'-'+pad(now.getDate())+' '+pad(now.getHours())+':'+pad(now.getMinutes())+':'+pad(now.getSeconds());
        var el = document.getElementById('datetime');
        if (el) el.textContent = dt;
    }
    
    function anyGroupUsesWaterMeter() {
        try {
            const groups = (statusData && Array.isArray(statusData.groups)) ? statusData.groups : [];
            const _flag = v => { try { if (v===true||v===1) return true; const s=String(v).trim().toLowerCase(); return s==='1'||s==='true'||s==='on'||s==='yes'; } catch(e){ return false; } };
            return groups.some(g => _flag(g.use_water_meter));
        } catch(e) { return false; }
    }

    function removeAdminCellsFromRows(){
        try{
            const tbody = document.getElementById('zones-table-body'); if (!tbody) return;
            tbody.querySelectorAll('tr').forEach(row=>{
                row.querySelectorAll('td.admin-only').forEach(td=> td.remove());
            });
        }catch(e){}
    }

    function updateAdminHeaderColumns(){
        try {
            const head = document.getElementById('zones-table-head');
            if (!head) return;
            const isAdmin = !!(statusData && statusData.is_admin);
            const wantWaterCols = isAdmin && anyGroupUsesWaterMeter();
            const hasAdmin = head.querySelectorAll('th.admin-only').length > 0;
            const tr = head.querySelector('tr');
            if (!tr) return;
            if (wantWaterCols && !hasAdmin) {
                const thAvg = document.createElement('th'); thAvg.className = 'admin-only'; thAvg.innerHTML = 'Средний расход<br>(л/мин)';
                const thTot = document.createElement('th'); thTot.className = 'admin-only'; thTot.innerHTML = 'Расход (л)<br>за прошлый полив';
                tr.insertBefore(thAvg, tr.lastElementChild);
                tr.insertBefore(thTot, tr.lastElementChild);
                // как только добавили заголовки — убедимся, что в строках есть ячейки
                try { ensureAdminCellsInRows(); } catch(e){}
                // и сразу заполним их текущими значениями из zonesData, если они уже есть
                try { fillAdminCellsFromZonesData(); } catch(e){}
            } else if ((!wantWaterCols) && hasAdmin) {
                head.querySelectorAll('th.admin-only').forEach(el=> el.remove());
                try { removeAdminCellsFromRows(); } catch(e){}
            }
        } catch(e) {}
    }

    function ensureAdminCellsInRows(){
        try{
            const head = document.getElementById('zones-table-head');
            const need = !!(head && head.querySelector('th.admin-only'));
            if (!need) return;
            const tbody = document.getElementById('zones-table-body'); if (!tbody) return;
            tbody.querySelectorAll('tr').forEach(row=>{
                const adminTds = row.querySelectorAll('td.admin-only');
                if (adminTds.length >= 2) return;
                const photoTd = row.lastElementChild; // фото — последняя колонка
                const tdAvg = document.createElement('td'); tdAvg.className = 'admin-only'; tdAvg.textContent = 'НД';
                const tdTot = document.createElement('td'); tdTot.className = 'admin-only'; tdTot.textContent = 'НД';
                if (photoTd && photoTd.parentElement === row) {
                    row.insertBefore(tdAvg, photoTd);
                    row.insertBefore(tdTot, photoTd);
                } else {
                    row.appendChild(tdAvg); row.appendChild(tdTot);
                }
            });
        }catch(e){}
    }

    function fillAdminCellsFromZonesData(){
        try{
            if (!Array.isArray(zonesData) || !zonesData.length) return;
            const byId = {};
            zonesData.forEach(z=>{ byId[String(z.id)] = z; });
            const tbody = document.getElementById('zones-table-body'); if (!tbody) return;
            tbody.querySelectorAll('tr').forEach(row=>{
                const idCell = row.querySelector('td:nth-child(2)');
                const adminCells = row.querySelectorAll('td.admin-only');
                if (!idCell || adminCells.length < 2) return;
                const zid = idCell.textContent.trim();
                const z = byId[zid]; if (!z) return;
                let avg = (z.last_avg_flow_lpm!=null && z.last_avg_flow_lpm!=='') ? z.last_avg_flow_lpm : 'НД';
                let tot = (z.last_total_liters!=null && z.last_total_liters!=='') ? z.last_total_liters : 'НД';
                if (avg !== 'НД') {
                    const n = Number(avg);
                    if (!Number.isNaN(n)) avg = String(Math.round(n));
                }
                adminCells[0].textContent = avg;
                adminCells[1].textContent = tot;
            });
        }catch(e){}
    }

    async function loadStatusData() {
        try {
            statusData = await api.get('/api/status');
            updateAdminHeaderColumns();
            updateStatusDisplay();
            hideConnectionError();
            updateMqttWarnings();
            // Согласуем таблицу зон с карточками групп (мгновенно)
            try { reconcileZoneRowsWithGroupStatus(); } catch(e) {}
        } catch (error) {
            console.error('Ошибка загрузки статуса:', error);
            showConnectionError();
        }
    }
    
    async function loadZonesData() {
        try {
            // Fetch zones + groups + next-watering in PARALLEL (single render)
            var needGroups = !zoneGroupsCache || !zoneGroupsCache.length;
            var promises = [
                fetch('/api/zones?ts=' + Date.now(), { cache: 'no-store' }).then(function(r){return r.json();}).catch(function(){return null;}),
            ];
            if (needGroups) {
                promises.push(fetch('/api/groups').then(function(r){return r.json();}).catch(function(){return [];}));
            } else {
                promises.push(null); // placeholder
            }
            var results = await Promise.all(promises);
            var fetchedZones = results[0];

            // On fetch error, preserve previous data
            if (!Array.isArray(fetchedZones)) {
                // Network error — keep existing zonesData, don't re-render with empty
                if (zonesData && zonesData.length) {
                    // Keep showing cached data
                    hideConnectionError();
                    return;
                }
                fetchedZones = [];
            }
            zonesData = fetchedZones;
            if (needGroups && results[1]) zoneGroupsCache = results[1];

            // Fetch next-watering bulk (parallel with zones already done, now fetch NW)
            var filteredZones = zonesData.filter(function(z) { return z.group_id !== 999; });
            // Cache previous next-watering data
            var prevNwCache = {};
            zonesData.forEach(function(z) { if (z._nextWatering) prevNwCache[z.id] = z._nextWatering; });

            try {
                var nwResp = await fetch('/api/zones/next-watering-bulk', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ zone_ids: filteredZones.map(function(z){return z.id;}) })
                });
                var nwData = await nwResp.json();
                var nwMap = {};
                (nwData.items || []).forEach(function(it) {
                    nwMap[it.zone_id] = it.next_datetime || (it.next_watering === 'Никогда' ? 'Никогда' : null);
                });
                zonesData.forEach(function(z) {
                    var v = nwMap[z.id];
                    if (statusData && statusData.emergency_stop) z._nextWatering = 'До отмены аварии';
                    else if (v === 'Никогда') z._nextWatering = 'Никогда';
                    else if (v) z._nextWatering = String(v).replace('T',' ').slice(0,16);
                    else z._nextWatering = prevNwCache[z.id] || '—';
                });
            } catch(e3) {
                // On NW fetch error, preserve cached next-watering data
                zonesData.forEach(function(z) {
                    if (!z._nextWatering && prevNwCache[z.id]) z._nextWatering = prevNwCache[z.id];
                });
            }

            // Single render after ALL data is ready
            renderGroupTabs();
            renderZoneCards();

            // Update sidebar indicators
            try { updateActiveZoneIndicator(zonesData); } catch(e) {}
            try { updateWaterMeter(zonesData); } catch(e) {}

            hideConnectionError();
        } catch (error) {
            console.error('Ошибка загрузки зон:', error);
            showConnectionError();
        }
    }

    function showConnectionError() {
        if (!connectionError) {
            connectionError = true;
            document.getElementById('connection-status').classList.add('show');
        }
    }
    
    function hideConnectionError() {
        if (connectionError) {
            connectionError = false;
            document.getElementById('connection-status').classList.remove('show');
        }
    }

    function updateMqttWarnings() {
        try {
            const noServers = !statusData || !Number(statusData.mqtt_servers_count || 0);
            const notConnected = !noServers && (statusData && statusData.mqtt_connected === false);
            const elNoServers = document.getElementById('mqtt-no-servers');
            const elNoConn = document.getElementById('mqtt-no-connection');
            if (noServers && !mqttNoServers) { mqttNoServers = true; elNoServers.classList.add('show'); }
            if (!noServers && mqttNoServers) { mqttNoServers = false; elNoServers.classList.remove('show'); }
            if (notConnected && !mqttNoConnection) { mqttNoConnection = true; elNoConn.classList.add('show'); }
            if (!notConnected && mqttNoConnection) { mqttNoConnection = false; elNoConn.classList.remove('show'); }
        } catch (e) {}
    }

    function handleZoneUpdateFromSse(zoneId, newState) {
        try {
            // Обновим локальные zonesData, чтобы таблица зон была согласованной
            const z = zonesData.find(x => Number(x.id) === Number(zoneId));
            if (z) z.state = newState;
            // Найдём группу и обновим только её карточку
            const groupId = z ? z.group_id : null;
            if (groupId) {
                refreshSingleGroup(groupId);
            } else {
                // Если вдруг не нашли — перезагрузим статус целиком как запасной вариант
                loadStatusData();
            }
        } catch (e) {}
    }

    // Точечное обновление строк зон группы (без полной перерисовки таблицы)
    async function refreshZonesRowsForGroup(groupId) {
        try {
            const tbody = document.getElementById('zones-table-body');
            if (!tbody) return;
            const zonesForGroup = (zonesData || []).filter(z => String(z.group_id) === String(groupId));
            const promises = zonesForGroup.map(async (zone) => {
                try {
                    const resp = await fetch(`/api/zones/${zone.id}/next-watering`, { cache: 'no-store' });
                    const data = await resp.json();
                    let nextText = '—';
                    if (data && data.next_datetime) {
                        nextText = String(data.next_datetime).replace('T',' ').slice(0,19);
                    } else if (data && data.next_watering === 'Никогда') {
                        nextText = 'Никогда';
                    }
                    // Найти строку зоны по её № (вторая колонка)
                    const rows = tbody.querySelectorAll('tr');
                    rows.forEach(row => {
                        const cells = row.querySelectorAll('td');
                        if (cells.length >= 9) {
                            const idCell = cells[1];
                            if (idCell && String(idCell.textContent.trim()) === String(zone.id)) {
                                // Колонка "Следующий полив" — девятая (index 8)
                                const nextCell = cells[8];
                                if (nextCell) nextCell.textContent = nextText;
                            }
                        }
                    });
                } catch (e) { /* ignore single zone error */ }
            });
            await Promise.all(promises);
        } catch (e) { /* ignore group update error */ }
    }

    // Load zones FIRST, then status (status needs zonesData for timers)
    async function refreshAllData() {
        try {
            await loadZonesData();
            await loadStatusData();
        } catch (e) {}
    }

    async function refreshAllUI() {
        await refreshAllData();
    }
