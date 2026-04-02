    // === Data loading and admin columns ===

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
            // Fetch zones + groups in PARALLEL
            var needGroups = !zoneGroupsCache || !zoneGroupsCache.length;
            var promises = [
                fetch('/api/zones?ts=' + Date.now(), { cache: 'no-store' }).then(function(r){return r.json();}).catch(function(){return [];}),
            ];
            if (needGroups) {
                promises.push(fetch('/api/groups').then(function(r){return r.json();}).catch(function(){return [];}));
            }
            var results = await Promise.all(promises);
            zonesData = Array.isArray(results[0]) ? results[0] : [];
            if (needGroups && results[1]) zoneGroupsCache = results[1];

            // Render V2 zones IMMEDIATELY
            renderGroupTabs();
            renderZoneCards();

            // Fetch next-watering bulk ASYNC (non-blocking)
            var filteredZones = zonesData.filter(function(z) { return z.group_id !== 999; });
            (async function() {
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
                        else z._nextWatering = '—';
                    });
                    // Re-render with next-watering data
                    renderZoneCards();
                } catch(e3) {}
            })();

            // Update sidebar indicators
            try { updateActiveZoneIndicator(zonesData); } catch(e) {}
            try { updateWaterMeter(zonesData); } catch(e) {}

            hideConnectionError();
        } catch (error) {
            console.error('Ошибка загрузки зон:', error);
            showConnectionError();
        }
    }

    // Быстрая синхронизация строк зон с текущим статусом групп из statusData
    function reconcileZoneRowsWithGroupStatus() {
        try {
            if (!statusData || !statusData.groups || !statusData.groups.length) return;
            const wateringByGroup = {};
            (statusData.groups || []).forEach(g => {
                if (g && g.status === 'watering' && g.current_zone) {
                    wateringByGroup[String(g.id)] = Number(g.current_zone);
                }
            });
            const tbody = document.getElementById('zones-table-body');
            if (!tbody) return;
            const rows = tbody.querySelectorAll('tr');
            rows.forEach(row => {
                try {
                    const idCell = row.querySelector('td:nth-child(2)');
                    const grpCell = row.querySelector('td:nth-child(7)');
                    if (!idCell || !grpCell) return;
                    const zid = Number(idCell.textContent.trim());
                    const gidAttr = grpCell.getAttribute('data-group-id');
                    const gid = gidAttr ? String(Number(gidAttr)) : String(grpCell.textContent.trim());
                    const runningZoneId = wateringByGroup[gid];
                    if (typeof runningZoneId === 'undefined') return;
                    const isOn = (zid === runningZoneId);
                    const ind = row.querySelector('.indicator');
                    if (ind) { ind.classList.remove('on','off'); ind.classList.add(isOn ? 'on' : 'off'); }
                    const btn = row.querySelector('.zone-start-btn');
                    if (btn) {
                        btn.textContent = isOn ? '⏹' : '▶';
                        const emergency = !!(statusData && statusData.emergency_stop);
                        const action = emergency ? "showNotification('Аварийная остановка активна. Сначала отключите режим.', 'warning')" : ("startOrStopZone(" + zid + ", '" + (isOn ? 'on' : 'off') + "')");
                        btn.setAttribute('onclick', action);
                    }
                } catch(e) {}
            });
        } catch (e) {}
    }

    async function refreshAllUI() {
        try {
            await Promise.all([loadStatusData(), loadZonesData()]);
        } catch (e) {}
    }
