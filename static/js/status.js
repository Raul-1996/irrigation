    // UI timing helpers
    (function(){
      function nowMs(){ return performance && performance.now ? performance.now() : Date.now(); }
      function logUiTiming(kind, detail, ms){
        try{
          console.log(`[UI Timing] ${kind} ${detail}: ${Math.round(ms)}ms`);
        }catch(e){}
      }
      // Wrap fetch to time control actions
      const _fetch = window.fetch;
      window.fetch = async function(input, init){
        const url = (typeof input === 'string') ? input : (input && input.url) || '';
        const isControl = /\/api\/(zones\/.+\/(mqtt\/)?(start|stop)|groups\/\d+\/(start-from-first|stop)|emergency-(stop|resume)|postpone)/.test(url);
        const t0 = nowMs();
        const resp = await _fetch(input, init);
        const t1 = nowMs();
        if (isControl){ logUiTiming('HTTP', url, t1 - t0); }
        return resp;
      };
      // Time button clicks to response end
      function wireBtnTiming(){
        const btnSelectors = [
          '.zone-start-btn', '#emergency-btn', '#resume-btn',
        ];
        btnSelectors.forEach(sel=>{
          document.querySelectorAll(sel).forEach(btn=>{
            if (btn.__timed) return; btn.__timed = true;
            btn.addEventListener('click', ()=>{ btn.__t0 = nowMs(); }, {capture:true});
          });
        });
        // Generic listener to measure end of network roundtrip via DOM updates
        document.addEventListener('zones-rendered', ()=>{
          try{
            const tNow = nowMs();
            document.querySelectorAll('.zone-start-btn').forEach(b=>{
              if (b.__t0){ logUiTiming('UI', 'zone-toggle->render', tNow - b.__t0); b.__t0 = null; }
            });
          }catch(e){}
        });
      }
      document.addEventListener('DOMContentLoaded', wireBtnTiming);
    })();

    // Загрузка и обновление данных статуса
    let statusData = null;
    let zonesData = [];
    let connectionError = false;
    let mqttNoServers = false;
    let mqttNoConnection = false;
    let envProbeTimer = null;
    let envProbeAttempts = 0;
    
    // Функция обновления времени
    async function updateDateTime() {
        try {
            const r = await fetch('/api/server-time?ts=' + Date.now(), { cache: 'no-store' });
            const j = await r.json();
            if (j && j.now_iso) {
                document.getElementById('datetime').textContent = j.now_iso.replace('T',' ');
                return;
            }
        } catch (e) {}
        const now = new Date();
        const pad = (n)=> String(n).padStart(2,'0');
        const dt = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
        document.getElementById('datetime').textContent = dt;
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
            // 1) Быстрый рендер зон по /api/zones (напрямую через fetch, без обёртки)
            let zonesRespJson = [];
            try {
                const zr = await fetch('/api/zones?ts=' + Date.now(), { cache: 'no-store' });
                zonesRespJson = await zr.json();
            } catch (e) {
                zonesRespJson = [];
            }
            zonesData = Array.isArray(zonesRespJson) ? zonesRespJson : [];
            const tbody = document.getElementById('zones-table-body');
            try { updateAdminHeaderColumns(); } catch(e){}
            // ВАЖНО: сначала убедимся, что в существующих строках есть админ-ячейки,
            // чтобы ниже мы могли сразу заполнить их актуальными значениями без задержки
            try { ensureAdminCellsInRows(); } catch(e){}
            try { fillAdminCellsFromZonesData(); } catch(e){}
            const existingRows = {};
            tbody.querySelectorAll('tr').forEach(row=>{
                const idCell = row.querySelector('td:nth-child(2)');
                if (idCell) existingRows[idCell.textContent.trim()] = row;
            });
            const filteredZones = (zonesData || []).filter(z=>z.group_id !== 999);
            const frag = document.createDocumentFragment();
            filteredZones.forEach(zone=>{
                const rowId = String(zone.id);
                const row = existingRows[rowId];
                if (row){
                    // только изменившиеся поля
                    const ind = row.querySelector('.indicator');
                    if (ind){ ind.classList.remove('on','off'); ind.classList.add(zone.state); }
                    const btn = row.querySelector('.zone-start-btn');
                    if (btn){
                        btn.textContent = zone.state==='on'?'⏹':'▶';
                        const emergency = !!(statusData && statusData.emergency_stop);
                        const action = emergency ? "showNotification('Аварийная остановка активна. Сначала отключите режим.', 'warning')" : ("startOrStopZone(" + zone.id + ", '" + zone.state + "')");
                        btn.setAttribute('onclick', action);
                    }
                    if (!row.getAttribute('data-zone-id')) {
                        try { row.setAttribute('data-zone-id', String(zone.id)); } catch(e) {}
                    }
                    // обновим админ-ячейки, если есть и нужно показывать
                    const isAdmin = (!!(statusData && statusData.is_admin));
                    const showWaterCols = isAdmin && anyGroupUsesWaterMeter();
                    if (isAdmin) {
                        const adminCells = row.querySelectorAll('td.admin-only');
                        if (showWaterCols && adminCells && adminCells.length>=2){
                            let avg = (zone.last_avg_flow_lpm!=null && zone.last_avg_flow_lpm!=='') ? zone.last_avg_flow_lpm : 'НД';
                            const tot = (zone.last_total_liters!=null && zone.last_total_liters!=='') ? zone.last_total_liters : 'НД';
                            if (avg !== 'НД') {
                                const n = Number(avg);
                                if (!Number.isNaN(n)) avg = String(Math.round(n));
                            }
                            adminCells[0].textContent = avg;
                            adminCells[1].textContent = tot;
                        } else if (!showWaterCols && adminCells && adminCells.length){
                            adminCells.forEach(td=> td.remove());
                        }
                    }
                } else {
                    // создаём новую строку с placeholders для группы и next
                    const tr = document.createElement('tr');
                    const emergency = !!(statusData && statusData.emergency_stop);
                    const action = emergency ? "showNotification('Аварийная остановка активна. Сначала отключите режим.', 'warning')" : ("startOrStopZone(" + zone.id + ", '" + zone.state + "')");
                    const isAdmin = (!!(statusData && statusData.is_admin));
                    const showWaterCols = isAdmin && anyGroupUsesWaterMeter();
                    let avgFlow = (zone.last_avg_flow_lpm!=null && zone.last_avg_flow_lpm!=='') ? zone.last_avg_flow_lpm : 'НД';
                    if (avgFlow !== 'НД') {
                        const n = Number(avgFlow);
                        if (!Number.isNaN(n)) avgFlow = String(Math.round(n));
                    }
                    const totalLiters = (zone.last_total_liters!=null && zone.last_total_liters!=='') ? zone.last_total_liters : 'НД';
                    tr.innerHTML = `
                        <td><span class="indicator ${zone.state}"></span></td>
                        <td>${zone.id}</td>
                        <td><button class="zone-start-btn" onclick="${action}">${zone.state==='on' ? '⏹' : '▶'}</button></td>
                        <td>${zone.name}</td>
                        <td>${zone.icon}</td>
                        <td>${zone.duration} мин</td>
                        <td data-group-id="${zone.group_id}">${zone.group_id}</td>
                        <td class="hide-mobile col-last-watering">—</td>
                        <td class="col-next" data-label="Следующий полив">—</td>
                        ${showWaterCols ? `<td class=\"admin-only\">${avgFlow}</td>` : ''}
                        ${showWaterCols ? `<td class=\"admin-only\">${totalLiters}</td>` : ''}
                        <td class="col-photo" data-label="Фото">
                            <div class="zone-photo">
                                ${zone.photo_path ? 
                                    `<img src="/api/zones/${zone.id}/photo" alt="Фото зоны ${zone.id}" onclick="showPhotoModal('/api/zones/${zone.id}/photo')" title="Нажмите для просмотра">` :
                                    `<div class="no-photo" title="Нет фото">📷</div>`
                                }
                            </div>
                        </td>`;
                    try { tr.setAttribute('data-zone-id', String(zone.id)); } catch(e) {}
                    frag.appendChild(tr);
                }
            });
            if (frag.childNodes.length) tbody.appendChild(frag);
            // Повторная страховка на случай вновь созданных строк
            try { ensureAdminCellsInRows(); } catch(e){}
            try { fillAdminCellsFromZonesData(); } catch(e){}
            document.getElementById('zones-count').textContent = filteredZones.length;
            hideConnectionError();
            // После первичного рендера — синхронизация с актуальным статусом групп
            try { reconcileZoneRowsWithGroupStatus(); } catch(e) {}
            // 2) Подгружаем имена групп без блокировки
            (async()=>{
                try{
                    const groups = await api.get('/api/groups');
                    const nameById = {}; (groups||[]).forEach(g=>{ nameById[g.id]=g.name; });
                    tbody.querySelectorAll('tr').forEach(row=>{
                        const cell = row.querySelector('td:nth-child(7)');
                        if (!cell) return;
                        const gidAttr = cell.getAttribute('data-group-id');
                        const gid = gidAttr ? Number(gidAttr) : Number(cell.textContent.trim());
                        if (nameById[gid]) cell.textContent = nameById[gid];
                    });
                }catch(e){}
            })();
            // 3) Подгружаем next-watering без блокировки
            (async()=>{
                try{
                    const ids = filteredZones.map(z=>z.id);
                    if (!ids.length) return;
                    const resp = await fetch('/api/zones/next-watering-bulk',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({zone_ids: ids})});
                    if (!resp.ok) return;
                    const bulk = await resp.json();
                    const nextMap = {}; (bulk.items||[]).forEach(it=>{ nextMap[it.zone_id]= it.next_datetime || (it.next_watering==='Никогда'?'Никогда': null); });
                    tbody.querySelectorAll('tr').forEach(row=>{
                        const idCell = row.querySelector('td:nth-child(2)');
                        const nextCell = row.querySelector('td:nth-child(9)');
                        if (!idCell || !nextCell) return;
                        const zid = Number(idCell.textContent.trim());
                        if (!(zid in nextMap)) return;
                        const v = nextMap[zid];
                        let txt = '—';
                        if (statusData && statusData.emergency_stop) txt = 'До отмены аварии';
                        else if (v==='Никогда') txt = 'Никогда';
                        else if (v) txt = String(v).replace('T',' ').slice(0,19);
                        nextCell.textContent = txt;
                    });
                }catch(e){}
            })();
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
    
    function formatSeconds(total) {
        const sec = Math.max(0, Math.floor(total));
        const mm = String(Math.floor(sec / 60)).padStart(2, '0');
        const ss = String(sec % 60).padStart(2, '0');
        return `${mm}:${ss}`;
    }

    function getStatusText(group) {
        if (group.status === 'watering' && group.current_zone) {
            const src = String(group.current_zone_source || '').toLowerCase();
            // Оставляем два статуса: по расписанию, либо вручную (включая удаленно)
            if (src === 'schedule') return 'Полив - активно поливается(по расписанию)';
            return 'Полив - активно поливается(запущено вручную)';
        }
        switch (group.status) {
            case 'waiting': return 'Ожидание - готов к поливу';
            case 'error': return 'Ошибка - проблема с системой';
            case 'postponed': {
                const r = (group.postpone_reason || '').toString();
                if (r === 'rain') return 'Отложено - полив отложен из за дождя';
                if (r === 'manual') return 'Отложено - полив отложен пользователем';
                if (r === 'emergency') return 'Отложено - полив отложен из за аварии';
                return 'Отложено - полив отложен';
            }
            default: return 'Ожидание - готов к поливу';
        }
    }

    async function initGroupTimer(group) {
        const span = document.getElementById(`group-timer-${group.id}`);
        if (!span) return;
        try {
            const res = await fetch(`/api/zones/${group.current_zone}/watering-time?ts=${Date.now()}`, { cache: 'no-store' });
            const data = await res.json();
            if (data && data.success && data.is_watering) {
                span.dataset.remainingSeconds = String(data.remaining_seconds ?? (data.remaining_time * 60));
                span.textContent = formatSeconds(Number(span.dataset.remainingSeconds));
            } else {
                span.dataset.remainingSeconds = '';
                span.textContent = '--:--';
            }
        } catch (e) {
            span.textContent = '--:--';
        }
    }
    
    async function updateStatusDisplay() {
        if (!statusData) return;
        updateDateTime();
        // Температура/влажность: показываем блоки, только если датчики включены
        const tb = document.getElementById('temp-box');
        const hb = document.getElementById('hum-box');
        const tv = document.getElementById('temp-value');
        const hv = document.getElementById('hum-value');
        if (statusData.temperature === null || typeof statusData.temperature === 'undefined') {
            tb.style.display = 'none';
        } else {
            tb.style.display = 'inline-block';
            tv.textContent = (statusData.temperature === 'нет данных') ? 'нет данных' : String(Math.round(Number(statusData.temperature)));
        }
        if (statusData.humidity === null || typeof statusData.humidity === 'undefined') {
            hb.style.display = 'none';
        } else {
            hb.style.display = 'inline-block';
            hv.textContent = (statusData.humidity === 'нет данных') ? 'нет данных' : String(Math.round(Number(statusData.humidity)));
        }
        // Отображение датчика дождя: показывать, только если глобально включен
        (function(){
            const rb = document.getElementById('rain-box');
            const rv = document.getElementById('rain-value');
            const enabled = !!(statusData && statusData.rain_enabled);
            if (!enabled) {
                rb.style.display = 'none';
                return;
            }
            rb.style.display = 'inline-block';
            const s = String(statusData.rain_sensor || '').toLowerCase();
            rv.textContent = (s.indexOf('идёт дожд') !== -1 || s.indexOf('идет дожд') !== -1) ? 'дождь идет' : 'нет дождя';
        })();

        // Быстрый пробник: если сейчас отображается "нет данных", опрашиваем /api/env чаще (до 10 попыток)
        if ((tv.textContent === 'нет данных' || hv.textContent === 'нет данных') && !envProbeTimer) {
            envProbeAttempts = 0;
            envProbeTimer = setInterval(async () => {
                try {
                    envProbeAttempts += 1;
                    const resp = await fetch(`/api/env?ts=${Date.now()}`, { cache: 'no-store' });
                    const js = await resp.json();
                    const val = js && js.values ? js.values : {};
                    if (typeof val.temp !== 'undefined' && val.temp !== null) {
                        tb.style.display = 'inline-block';
                        tv.textContent = String(Math.round(Number(val.temp)));
                    }
                    if (typeof val.hum !== 'undefined' && val.hum !== null) {
                        hb.style.display = 'inline-block';
                        hv.textContent = String(Math.round(Number(val.hum)));
                    }
                    if (tv.textContent !== 'нет данных' && hv.textContent !== 'нет данных') {
                        clearInterval(envProbeTimer); envProbeTimer = null;
                    }
                    if (envProbeAttempts >= 10) { clearInterval(envProbeTimer); envProbeTimer = null; }
                } catch (e) {
                    if (envProbeAttempts >= 10) { clearInterval(envProbeTimer); envProbeTimer = null; }
                }
            }, 1000);
        }
        const container = document.getElementById('groups-container');
        container.innerHTML = '';
        const resumeBtn = document.getElementById('resume-btn');
        if (statusData.emergency_stop) { resumeBtn.style.display = 'inline-block'; } else { resumeBtn.style.display = 'none'; }
        for (const group of statusData.groups) {
            const card = document.createElement('div');
            const flowActive = group.status === 'watering' && Math.random() > 0.3;
            card.className = `card ${group.status} ${flowActive ? 'flow-active' : ''}`;
            const statusText = getStatusText(group);
            // Доп. информация: при поливе — зона и таймер; при отложке — дата/время; при ошибке — текст ошибки; иначе — '—'
            let extraText = '—';
            if (group.status === 'watering' && group.current_zone) {
                extraText = `Зона ${group.current_zone}: осталось <span class="group-timer" id="group-timer-${group.id}" data-group-id="${group.id}" data-zone-id="${group.current_zone}" data-remaining-seconds="">--:--</span>`;
            } else if (group.status === 'postponed' && group.postpone_until) {
                const pu = String(group.postpone_until);
                const reason = String(group.postpone_reason || '').toLowerCase();
                if (reason === 'emergency' || pu.trim().toLowerCase().startsWith('до ')) {
                    extraText = pu;
                } else {
                    extraText = `До ${pu}`;
                }
            } else if (group.status === 'error' && group.error_message) {
                extraText = String(group.error_message);
            }
            const anyZoneOnThisGroup = (String(group.status||'').toLowerCase()==='watering' && group.current_zone);
            const groupActionHtml = anyZoneOnThisGroup
                ? `<button class="group-action-btn group-action-stop" onclick="stopGroup(${group.id})">Остановить полив группы</button>`
                : `<button class="group-action-btn group-action-start" onclick="startGroupFromFirst(${group.id})">Запустить полив группы</button>`;
            const groupButtons = `
                <div class="btn-group">
                    <button class="delay" onclick="delayGroup(${group.id}, 1)">Остановить полив на 1 день</button>
                    <button class="delay" onclick="delayGroup(${group.id}, 2)">Остановить полив на 2 дня</button>
                    <button class="delay" onclick="delayGroup(${group.id}, 3)">Остановить полив на 3 дня</button>
                    ${group.status === 'postponed' && group.postpone_until && !statusData.emergency_stop ? `<button class="cancel-postpone" onclick="cancelPostpone(${group.id})">Продолжить по расписанию</button>` : ''}
                </div>
                <div class="btn-group" style="width:100%">${groupActionHtml}</div>`;
            // Optional feature blocks by group flags
            const mvEnabled = (group.use_master_valve === true) || (group.use_master_valve === 1);
            const mvState = String(group.master_valve_state || 'unknown');
            const mvIndicator = mvState === 'open' ? 'Открыт' : (mvState === 'closed' ? 'Закрыт' : '—');
            const _flag = v => { try { if (v===true||v===1) return true; const s=String(v).trim().toLowerCase(); return s==='1'||s==='true'||s==='on'||s==='yes'; } catch(e){ return false; } };
            const pressureOn = _flag(group.use_pressure_sensor);
            const flowOn = _flag(group.use_water_meter);
            const gridCells = [];
            if (mvEnabled) {
                const dotCls = mvState==='open' ? 'open' : (mvState==='closed' ? 'closed' : '');
                const actionText = (mvState==='open') ? 'Закрыть мастер-клапан' : 'Открыть мастер-клапан';
                const stateText = mvState==='open' ? 'Открыт' : (mvState==='closed' ? 'Закрыт' : '—');
                const mvBtn = `<button id="mv-btn-${group.id}" class="mv-button" data-mv-state="${mvState}" onclick="toggleMasterValve(${group.id})">\n                    <span class="mv-action">${actionText}</span>\n                    <span class="mv-dot ${dotCls}"></span>\n                    <span class="mv-state-text">(${stateText})</span>\n                </button>`;
                gridCells.push(`<div class="grid-item grid-item-span2">${mvBtn}</div>`);
            }
            if (pressureOn && !flowOn) {
                gridCells.push(`<div class="grid-item grid-item-span2"><div class="info-chip"><span class="label">Давление:</span> <span id="pressure-${group.id}">${(group.pressure_value!=null&&group.pressure_value!=='')?group.pressure_value:'—'}</span> ${group.pressure_unit||''}</div></div>`);
            } else if (!pressureOn && flowOn) {
                const meter = (typeof group.meter_value_m3 !== 'undefined' && group.meter_value_m3 !== null) ? String(group.meter_value_m3) : '—';
                const flow = (typeof group.flow_value !== 'undefined' && group.flow_value !== null && group.flow_value !== '') ? String(group.flow_value) : '—';
                gridCells.push(`<div class="grid-item grid-item-span2"><div class="info-chip"><span class="label">Счётчик:</span> <span id="meter-${group.id}">${meter}</span> м³ (<span id="flow-${group.id}">${flow}</span> л/мин)</div></div>`);
            } else {
                if (pressureOn) {
                    gridCells.push(`<div class="grid-item"><div class="info-chip"><span class="label">Давление:</span> <span id="pressure-${group.id}">${(group.pressure_value!=null&&group.pressure_value!=='')?group.pressure_value:'—'}</span> ${group.pressure_unit||''}</div></div>`);
                }
                if (flowOn) {
                    const meter = (typeof group.meter_value_m3 !== 'undefined' && group.meter_value_m3 !== null) ? String(group.meter_value_m3) : '—';
                    const flow = (typeof group.flow_value !== 'undefined' && group.flow_value !== null && group.flow_value !== '') ? String(group.flow_value) : '—';
                    gridCells.push(`<div class="grid-item"><div class="info-chip"><span class="label">Счётчик:</span> <span id="meter-${group.id}">${meter}</span> м³ (<span id="flow-${group.id}">${flow}</span> л/мин)</div></div>`);
                }
            }
            // pad to keep even number of cells for 2x2 symmetry on desktop
            const mvBlock = gridCells.length ? `<div class="group-info-grid">${gridCells.join('')}${gridCells.length % 2 ? '<div class="grid-item"></div>' : ''}</div>` : '';
            card.innerHTML = `
                <div class="group-header">${group.name}</div>
                <div id="group-status-${group.id}">${statusText}</div>
                <div class="postpone-until">${extraText}</div>
                ${groupButtons}
                ${mvBlock}
            `;
            card.id = `group-card-${group.id}`;
            container.appendChild(card);
            if (group.status === 'watering' && group.current_zone) {
                initGroupTimer(group);
            }
        }
    }

    function tickCountdowns() {
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
            span.dataset.remainingSeconds = String(sec);
            span.textContent = formatSeconds(sec);
        });
    }

    async function refreshSingleGroup(groupId) {
        try {
            const resp = await fetch(`/api/status?ts=${Date.now()}`, {cache: 'no-store'});
            const data = await resp.json();
            if (!data || !data.groups) return;
            // Обновим глобальные данные статуса, чтобы кнопки/условия отображались корректно
            statusData = data;
            const group = (data.groups || []).find(g => String(g.id) === String(groupId));
            if (!group) return;
            const card = document.getElementById(`group-card-${group.id}`);
            if (!card) return;
            // Полностью пересоберем содержимое карточки по актуальным данным
            const flowActive = group.status === 'watering' && Math.random() > 0.3;
            card.className = `card ${group.status} ${flowActive ? 'flow-active' : ''}`;
            const statusText = getStatusText(group);
            let extraText2 = '—';
            if (group.status === 'watering' && group.current_zone) {
                extraText2 = `Зона ${group.current_zone}: осталось <span class="group-timer" id="group-timer-${group.id}" data-group-id="${group.id}" data-zone-id="${group.current_zone}" data-remaining-seconds="">--:--</span>`;
            } else if (group.status === 'postponed' && group.postpone_until) {
                const pu2 = String(group.postpone_until);
                const reason2 = String(group.postpone_reason || '').toLowerCase();
                if (reason2 === 'emergency' || pu2.trim().toLowerCase().startsWith('до ')) {
                    extraText2 = pu2;
                } else {
                    extraText2 = `До ${pu2}`;
                }
            } else if (group.status === 'error' && group.error_message) {
                extraText2 = String(group.error_message);
            }
            const mvEnabled2 = (group.use_master_valve === true) || (group.use_master_valve === 1); // показываем только если включено для группы
            const mvState2 = String(group.master_valve_state || 'unknown');
            const mvIndicator2 = mvState2 === 'open' ? 'Открыт' : (mvState2 === 'closed' ? 'Закрыт' : '—');
            const anyZoneOnThisGroup2 = (String(group.status||'').toLowerCase()==='watering' && group.current_zone);
            const groupActionHtml2 = anyZoneOnThisGroup2
                ? `<button class=\"group-action-btn group-action-stop\" onclick=\"stopGroup(${group.id})\">Остановить полив группы</button>`
                : `<button class=\"group-action-btn group-action-start\" onclick=\"startGroupFromFirst(${group.id})\">Запустить полив группы</button>`;
            const groupButtons = `
                <div class=\"btn-group\">
                    <button class=\"delay\" onclick=\"delayGroup(${group.id}, 1)\">Остановить полив на 1 день</button>
                    <button class=\"delay\" onclick=\"delayGroup(${group.id}, 2)\">Остановить полив на 2 дня</button>
                    <button class=\"delay\" onclick=\"delayGroup(${group.id}, 3)\">Остановить полив на 3 дня</button>
                    ${group.status === 'postponed' && group.postpone_until && !statusData.emergency_stop ? `<button class=\"cancel-postpone\" onclick=\"cancelPostpone(${group.id})\">Продолжить по расписанию</button>` : ''}
                </div>
                <div class=\"btn-group\" style=\"width:100%\">${groupActionHtml2}</div>`;
            card.innerHTML = `
                <div class="group-header">${group.name}</div>
                <div id="group-status-${group.id}">${statusText}</div>
                <div class="postpone-until">${extraText2}</div>
                ${groupButtons}
            `;
            // Append the same grid as in updateStatusDisplay
            (function(){
                const _flag2 = v => { try { if (v===true||v===1) return true; const s=String(v).trim().toLowerCase(); return s==='1'||s==='true'||s==='on'||s==='yes'; } catch(e){ return false; } };
                const pressureOn2 = _flag2(group.use_pressure_sensor);
                const flowOn2 = _flag2(group.use_water_meter);
                const cells = [];
                if (mvEnabled2) {
                    const dotCls2 = mvState2==='open' ? 'open' : (mvState2==='closed' ? 'closed' : '');
                    const actionText2 = (mvState2==='open') ? 'Закрыть мастер-клапан' : 'Открыть мастер-клапан';
                    const stateText2 = mvState2==='open' ? 'Открыт' : (mvState2==='closed' ? 'Закрыт' : '—');
                    const mvBtn2 = `<button id=\"mv-btn-${group.id}\" class=\"mv-button\" data-mv-state=\"${mvState2}\" onclick=\"toggleMasterValve(${group.id})\">`
                                  + `<span class=\"mv-action\">${actionText2}</span>`
                                  + `<span class=\"mv-dot ${dotCls2}\"></span>`
                                  + `<span class=\"mv-state-text\">(${stateText2})</span>`
                                  + `</button>`;
                    cells.push(`<div class=\"grid-item grid-item-span2\">${mvBtn2}</div>`);
                }
                if (pressureOn2 && !flowOn2) {
                    cells.push(`<div class=\"grid-item grid-item-span2\"><div class=\"info-chip\"><span class=\"label\">Давление:</span> <span id=\"pressure-${group.id}\">${(group.pressure_value!=null&&group.pressure_value!=='')?group.pressure_value:'—'}</span> ${group.pressure_unit||''}</div></div>`);
                } else if (!pressureOn2 && flowOn2) {
                    const meter2 = (typeof group.meter_value_m3 !== 'undefined' && group.meter_value_m3 !== null) ? String(group.meter_value_m3) : '—';
                    const flow2 = (typeof group.flow_value !== 'undefined' && group.flow_value !== null && group.flow_value !== '') ? String(group.flow_value) : '—';
                    cells.push(`<div class=\"grid-item grid-item-span2\"><div class=\"info-chip\"><span class=\"label\">Счётчик:</span> <span id=\"meter-${group.id}\">${meter2}</span> м³ (<span id=\"flow-${group.id}\">${flow2}</span> л/мин)</div></div>`);
                } else {
                    if (pressureOn2) {
                        cells.push(`<div class=\"grid-item\"><div class=\"info-chip\"><span class=\"label\">Давление:</span> <span id=\"pressure-${group.id}\">${(group.pressure_value!=null&&group.pressure_value!=='')?group.pressure_value:'—'}</span> ${group.pressure_unit||''}</div></div>`);
                    }
                    if (flowOn2) {
                        const meter2 = (typeof group.meter_value_m3 !== 'undefined' && group.meter_value_m3 !== null) ? String(group.meter_value_m3) : '—';
                        const flow2 = (typeof group.flow_value !== 'undefined' && group.flow_value !== null && group.flow_value !== '') ? String(group.flow_value) : '—';
                        cells.push(`<div class=\"grid-item\"><div class=\"info-chip\"><span class=\"label\">Счётчик:</span> <span id=\"meter-${group.id}\">${meter2}</span> м³ (<span id=\"flow-${group.id}\">${flow2}</span> л/мин)</div></div>`);
                    }
                }
                if (cells.length) {
                    const pad2 = cells.length % 2 ? '<div class=\"grid-item\"></div>' : '';
                    card.innerHTML += `<div class=\"group-info-grid\">${cells.join('')}${pad2}</div>`;
                }
            })();
            if (group.status === 'watering' && group.current_zone) {
                initGroupTimer(group);
            }
        } catch (e) {}
    }

    // Реакция на события MQTT через SSE для моментального обновления (используется ниже в DOMContentLoaded)

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
    
    async function updateZonesTable() {
        const tbody = document.getElementById('zones-table-body');
        const countSpan = document.getElementById('zones-count');
        
        tbody.innerHTML = '';
        
        // Фильтруем зоны, исключая группу 999 (БЕЗ ПОЛИВА)
        const filteredZones = zonesData.filter(zone => zone.group_id !== 999);
        
        countSpan.textContent = filteredZones.length;
        
        // Получаем имена групп для отображения вместо чисел
        const groups = await api.get('/api/groups');
        const groupNameById = {};
        groups.forEach(g => { groupNameById[g.id] = g.name; });

        // Загружаем следующий полив одним батчем (значительно быстрее на WB)
        let nextWateringData = [];
        try {
            const response = await fetch('/api/zones/next-watering-bulk', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ zone_ids: filteredZones.map(z=>z.id) })
            });
            const bulk = await response.json();
            nextWateringData = (bulk && bulk.items) ? bulk.items : [];
        } catch (e) {
            nextWateringData = [];
        }
        
        const frag = document.createDocumentFragment();
        filteredZones.forEach(zone => {
            let nextWatering = '—';
            if (statusData.emergency_stop) {
                nextWatering = 'До отмены аварии';
            } else {
                const item = nextWateringData.find(x => x.zone_id === zone.id) || {};
                const nextDT = item.next_datetime;
                if (nextDT) {
                    nextWatering = String(nextDT).replace('T',' ').slice(0,19);
                } else if (item.next_watering === 'Никогда') {
                    nextWatering = 'Никогда';
                }
            }
            const tr = document.createElement('tr');
            
            const isAdmin = (!!(statusData && statusData.is_admin));
            const showWaterCols = isAdmin && anyGroupUsesWaterMeter();
            const avgFlow = (zone.last_avg_flow_lpm!=null && zone.last_avg_flow_lpm!=='') ? zone.last_avg_flow_lpm : 'НД';
            const totalLiters = (zone.last_total_liters!=null && zone.last_total_liters!=='') ? zone.last_total_liters : 'НД';
             tr.innerHTML = `
                 <td><span class="indicator ${zone.state}"></span></td>
                 <td>${zone.id}</td>
                 <td><button class="zone-start-btn" onclick="${statusData.emergency_stop ? `showNotification('Аварийная остановка активна. Сначала отключите режим.', 'warning')` : `startOrStopZone(${zone.id}, '${zone.state}')`}">${zone.state==='on' ? '⏹' : '▶'}</button></td>
                 <td>${zone.name}</td>
                 <td>${zone.icon}</td>
                 <td>${zone.duration} мин</td>
                 <td>${groupNameById[zone.group_id] || zone.group_id}</td>
                 <td class="hide-mobile col-last-watering">—</td>
                 <td class="col-next" data-label="Следующий полив">${nextWatering}</td>
                 ${showWaterCols ? `<td class=\"admin-only\">${avgFlow}</td>` : ''}
                 ${showWaterCols ? `<td class=\"admin-only\">${totalLiters}</td>` : ''}
                 <td class="col-photo" data-label="Фото">
                     <div class="zone-photo">
                         ${zone.photo_path ? 
                             `<img src="/api/zones/${zone.id}/photo" alt="Фото зоны ${zone.id}" onclick="showPhotoModal('/api/zones/${zone.id}/photo')" title="Нажмите для просмотра">` :
                             `<div class="no-photo" title="Нет фото">📷</div>`
                         }
                     </div>
                 </td>
             `;
            
            frag.appendChild(tr);
        });
        tbody.appendChild(frag);
        // Signal render complete for perf
        try{ window.dispatchEvent(new CustomEvent('zones-rendered')); }catch(e){}
    }
    
    // Обработчики действий
    async function delayGroup(groupId, days) {
        try {
            // Блокируем кнопки в карточке группы на время запроса
            const card = document.getElementById(`group-card-${groupId}`);
            if (card) {
                const buttons = card.querySelectorAll('button');
                buttons.forEach(b=>b.disabled=true);
            }
            const response = await api.post('/api/postpone', {
                group_id: groupId,
                days: days,
                action: 'postpone'
            });
            
            if (response.success) {
                showNotification(response.message, 'success');
                // Точечно обновляем карточку группы и строки зон этой группы
                await refreshSingleGroup(groupId);
                await refreshZonesRowsForGroup(groupId);
            } else {
                showNotification(response.message, 'error');
            }
        } catch (error) {
            console.error('Ошибка при отложке полива:', error);
            showNotification('Ошибка при отложке полива', 'error');
        } finally {
            const card = document.getElementById(`group-card-${groupId}`);
            if (card) {
                const buttons = card.querySelectorAll('button');
                buttons.forEach(b=>b.disabled=false);
            }
        }
    }
    
    async function cancelPostpone(groupId) {
        try {
            // Блокируем кнопки в карточке группы на время запроса
            const card = document.getElementById(`group-card-${groupId}`);
            if (card) {
                const buttons = card.querySelectorAll('button');
                buttons.forEach(b=>b.disabled=true);
            }
            const response = await api.post('/api/postpone', {
                group_id: groupId,
                action: 'cancel'
            });
            
            if (response.success) {
                showNotification(response.message, 'success');
                // Точечно обновляем карточку группы и строки зон этой группы
                await refreshSingleGroup(groupId);
                await refreshZonesRowsForGroup(groupId);
            } else {
                showNotification(response.message, 'error');
            }
        } catch (error) {
            console.error('Ошибка при отмене отложенного полива:', error);
            showNotification('Ошибка при отмене отложенного полива', 'error');
        } finally {
            const card = document.getElementById(`group-card-${groupId}`);
            if (card) {
                const buttons = card.querySelectorAll('button');
                buttons.forEach(b=>b.disabled=false);
            }
        }
    }
    
    async function startGroupFromFirst(groupId) {
        try {
            const grp = (statusData && statusData.groups ? statusData.groups : []).find(g => String(g.id) === String(groupId));
            const gname = grp && grp.name ? grp.name : groupId;
            const res = await fetch(`/api/groups/${groupId}/start-from-first`, { method: 'POST' });
            const data = await res.json();
            if (data && data.success) {
                showNotification(`Группа "${gname}": ${data.message || 'запущена'}`, 'success');
                await loadStatusData();
                await loadZonesData();
            } else {
                showNotification(data.message || `Ошибка запуска группы "${gname}"`, 'error');
            }
        } catch (error) {
            showNotification('Ошибка при запуске полива группы', 'error');
        }
    }
    
    async function stopGroup(groupId) {
        try {
            const grp = (statusData && statusData.groups ? statusData.groups : []).find(g => String(g.id) === String(groupId));
            const gname = grp && grp.name ? grp.name : groupId;
            if (!confirm(`Остановить полив группы "${gname}"?`)) return;
        } catch (e) {
            if (!confirm(`Остановить полив группы ${groupId}?`)) return;
        }
        
        try {
            const res = await fetch(`/api/groups/${groupId}/stop`, { method: 'POST' });
            const data = await res.json();
            if (data && data.success) {
                showNotification(data.message, 'success');
                await loadStatusData();
                await loadZonesData();
            } else {
                showNotification(data.message || 'Ошибка остановки группы', 'error');
            }
        } catch (error) {
            showNotification('Ошибка при остановке группы', 'error');
        }
    }
    
    async function startOrStopZone(zoneId, currentState) {
        try {
            // Находим группу зоны
            const idx = zonesData.findIndex(z => z.id === zoneId);
            if (idx < 0) return;
            const zone = zonesData[idx];
            const groupId = zone.group_id;
            const wantOn = currentState !== 'on';
            // Оптимистическое обновление UI: переключим состояние сразу
            zonesData[idx].state = wantOn ? 'on' : 'off';
            // Мгновенно обновим строку зоны в таблице (индикатор и кнопка)
            try {
                const tbody = document.getElementById('zones-table-body');
                if (tbody) {
                    let row = tbody.querySelector(`tr[data-zone-id="${zoneId}"]`);
                    if (!row) {
                        const rows = tbody.querySelectorAll('tr');
                        rows.forEach(r => {
                            const cells = r.querySelectorAll('td');
                            if (cells.length > 1 && Number(cells[1].textContent.trim()) === zoneId) {
                                row = r;
                            }
                        });
                    }
                    if (row) {
                        const ind = row.querySelector('.indicator');
                        if (ind) { ind.classList.remove('on','off'); ind.classList.add(wantOn ? 'on' : 'off'); }
                        const btn = row.querySelector('.zone-start-btn');
                        if (btn) {
                            btn.textContent = wantOn ? '⏹' : '▶';
                            const emergency = !!(statusData && statusData.emergency_stop);
                            const action = emergency ? "showNotification('Аварийная остановка активна. Сначала отключите режим.', 'warning')" : ("startOrStopZone(" + zoneId + ", '" + (wantOn ? 'on' : 'off') + "')");
                            btn.setAttribute('onclick', action);
                        }
                    }
                }
            } catch (e) {}
            if (groupId) {
                // Обновим карточку группы и строки зон этой группы без полной перезагрузки
                refreshSingleGroup(groupId);
                refreshZonesRowsForGroup(groupId);
            }
            const url = wantOn ? `/api/zones/${zoneId}/mqtt/start` : `/api/zones/${zoneId}/mqtt/stop`;
            const res = await fetch(url, { method: 'POST' });
            let data = null;
            try { data = await res.json(); } catch(e) { data = null; }
            if (res.ok && data && data.success) {
                showNotification(data.message || (wantOn?'Зона запущена':'Зона остановлена'), 'success');
                // Подтянем актуальные данные для всей страницы без полной перезагрузки
                await refreshAllUI();
            } else {
                const msg = (data && data.message) ? data.message : (wantOn?'Ошибка запуска зоны':'Ошибка остановки зоны');
                showNotification(msg, 'error');
                // Откат оптимистического состояния
                zonesData[idx].state = currentState;
                await refreshAllUI();
            }
        } catch (error) {
            showNotification('Ошибка управления зоной', 'error');
        }
    }
    
    // === Master Valve toggle (UI-only; calls placeholder endpoint) ===
    async function toggleMasterValve(groupId) {
        try {
            // UI busy
            const card = document.getElementById(`group-card-${groupId}`);
            const btn = card ? card.querySelector(`#mv-btn-${groupId}`) : null;
            if (btn) { btn.disabled = true; btn.setAttribute('aria-busy','true'); }
            // Determine current state from button data attribute if present, fallback to text
            let currentState = (btn && btn.getAttribute('data-mv-state')) ? String(btn.getAttribute('data-mv-state')).toLowerCase() : '';
            if (!currentState) {
                const span = document.getElementById(`mv-state-${groupId}`);
                currentState = span ? (span.textContent || '').trim().toLowerCase() : '';
            }
            const wantOpen = currentState !== 'open' && currentState !== 'открыт';
            const url = wantOpen ? `/api/groups/${groupId}/master-valve/open` : `/api/groups/${groupId}/master-valve/close`;
            const res = await fetch(url, { method: 'POST' });
            let data = null; try { data = await res.json(); } catch(e) { data = null; }
            if (!res.ok || !(data && data.success)) {
                showNotification((data && data.message) || 'Не удалось выполнить операцию с мастер-клапаном', 'error');
                return;
            }
            // Wait for confirmation by reloading status a few times (2s total)
            for (let i=0;i<4;i++) {
                await new Promise(r=>setTimeout(r, 500));
                try { await refreshSingleGroup(groupId); } catch(e) {}
                const btn2 = document.getElementById(`mv-btn-${groupId}`);
                const stateAttr = btn2 ? String(btn2.getAttribute('data-mv-state')||'').toLowerCase() : '';
                if (wantOpen && (stateAttr==='open' || stateAttr==='открыт')) break;
                if (!wantOpen && (stateAttr==='closed' || stateAttr==='закрыт')) break;
            }
        } catch (e) {
            showNotification('Ошибка управления мастер-клапаном', 'error');
        } finally {
            const card = document.getElementById(`group-card-${groupId}`);
            const btn = card ? card.querySelector(`#mv-btn-${groupId}`) : null;
            if (btn) { btn.disabled = false; btn.removeAttribute('aria-busy'); }
        }
    }
    
    async function emergencyStop() {
        if (!confirm('Аварийная остановка всех зон?')) return;
        
        try {
            const res = await fetch('/api/emergency-stop', { method: 'POST' });
            const data = await res.json();
            if (data && data.success) {
                showNotification(data.message, 'warning');
                await loadStatusData();
                await loadZonesData();
                // Показать кнопку возобновления явно
                document.getElementById('resume-btn').style.display = 'inline-block';
            } else {
                showNotification(data.message || 'Ошибка аварийной остановки', 'error');
            }
        } catch (error) {
            showNotification('Ошибка при аварийной остановке', 'error');
        }
    }

    async function resumeSchedule() {
        try {
            const res = await fetch('/api/emergency-resume', { method: 'POST' });
            const data = await res.json();
            if (data && data.success) {
                showNotification(data.message, 'success');
                await loadStatusData();
                await loadZonesData();
                // Скрыть кнопку возобновления
                document.getElementById('resume-btn').style.display = 'none';
            } else {
                showNotification(data.message || 'Ошибка возобновления полива', 'error');
            }
        } catch (error) {
            showNotification('Ошибка возобновления полива', 'error');
        }
    }

    async function refreshAllUI() {
        try {
            await loadStatusData();
            await loadZonesData();
        } catch (e) {}
    }

    // Модальное окно для просмотра фотографий
    function showPhotoModal(photoUrl) {
        const img = document.getElementById('photoModalImg');
        img.src = photoUrl;
        const modal = document.getElementById('photoModal');
        modal.style.display = 'flex'; // чтобы сработало центрирование по flex
    }

    function closePhotoModal() {
        document.getElementById('photoModal').style.display = 'none';
    }
    
    // Инициализация
    document.addEventListener('DOMContentLoaded', () => {
        // Stopwatch wiring (delegated)
        try{
            function swStart(label){ try{ window.__sw && window.__sw.start(label||'click'); }catch(e){} }
            function swMark(label){ try{ window.__sw && window.__sw.mark(label||'mark'); }catch(e){} }
            document.addEventListener('click', (ev)=>{
                const el = ev.target;
                if (!(el instanceof Element)) return;
                if (el.closest('.zone-start-btn')) swStart('click .zone-start-btn');
                if (el.closest('#emergency-btn')) swStart('click #emergency-btn');
                if (el.closest('#resume-btn')) swStart('click #resume-btn');
                // Дополнительные кнопки управления
                if (el.closest('.delay')) {
                    // Определяем текст кнопки, чтобы различать 1/2/3 дня
                    const txt = (el.closest('.delay')?.textContent || '').trim();
                    swStart('click .delay ' + txt);
                }
                if (el.closest('.continue-group')) swStart('click .continue-group');
                if (el.closest('.stop-group')) swStart('click .stop-group');
            }, {capture:true});
            const _fetch = window.fetch;
            window.fetch = async function(input, init){
                const url = (typeof input === 'string') ? input : (input && input.url) || '';
                const isCtl = /\/api\/(zones\/.+\/(mqtt\/)?(start|stop)|groups\/\d+\/(start-from-first|stop)|emergency-(stop|resume)|postpone)/.test(url);
                if (isCtl) swMark('fetch:start '+url);
                const resp = await _fetch(input, init);
                if (isCtl) swMark('fetch:end '+url);
                return resp;
            };
            window.addEventListener('zones-rendered', ()=> swMark('zones-rendered'));
        }catch(e){}
        // Обновляем время сразу
        updateDateTime();
        
        loadStatusData();
        loadZonesData();
        
        // Обновление времени каждую секунду
        setInterval(updateDateTime, 1000);
        
        // Обновление данных каждые 30 секунд
        setInterval(() => {
            loadStatusData();
            loadZonesData();
        }, 30000);
        setInterval(tickCountdowns, 1000);
        
        // Обработчик аварийной остановки
        document.getElementById('emergency-btn').addEventListener('click', emergencyStop);
        document.getElementById('resume-btn').addEventListener('click', resumeSchedule);
        // Подписка на статусы зон через SSE
        try {
            let es; let retry = 0; let reconnectTimer = null;
            // WebSocket client (disabled by default; use SSE)
            const ENABLE_WS = false;
            let ws; let wsRetry = 0; let wsTimer = null;
            // Debounce для мгновенного обновления зон после смены состояния зоны
            let zonesReloadTimer = null;
            function requestZonesReload(){
                try{ clearTimeout(zonesReloadTimer); }catch(e){}
                zonesReloadTimer = setTimeout(()=>{ try{ loadZonesData(); }catch(e){} }, 500);
            }
            function connectWS(){
                if (!ENABLE_WS) return;
                try{ if (ws){ ws.close(); } }catch(e){}
                try{
                    const proto = (location.protocol === 'https:') ? 'wss' : 'ws';
                    ws = new WebSocket(`${proto}://${location.host}/ws`);
                    ws.onopen = ()=>{ wsRetry = 0; console.log('WS open'); };
                    ws.onerror = ()=>{ try{ ws.close(); }catch(e){} };
                    ws.onclose = ()=>{
                        const delay = Math.min(10000, 500 * Math.pow(2, Math.min(5, wsRetry++)));
                        clearTimeout(wsTimer); wsTimer = setTimeout(connectWS, delay);
                        console.log('WS closed, reconnect in', delay);
                    };
                    ws.onmessage = (ev)=>{
                        try{
                            const data = JSON.parse(ev.data);
                            if (typeof data.zone_id !== 'undefined') {
                                const idx = zonesData.findIndex(z=>z.id===data.zone_id);
                                if (idx>=0){ zonesData[idx].state = data.state; }
                                const gid = typeof data.group_id !== 'undefined' ? Number(data.group_id) : (idx>=0 ? zonesData[idx].group_id : null);
                                if (gid) { refreshSingleGroup(gid); }
                                if (String(data.state).toLowerCase() === 'off') { requestZonesReload(); }
                            } else if (typeof data.mv_group_id !== 'undefined') {
                                const gid = Number(data.mv_group_id);
                                const card = document.getElementById(`group-card-${gid}`);
                                if (card){
                                    const span = card.querySelector(`#mv-state-${gid}`);
                                    if (span) span.textContent = (data.mv_state === 'open' ? 'Открыт' : 'Закрыт');
                                    const chip = card.querySelector(`#mv-chip-${gid}`);
                                    if (chip){ chip.classList.remove('chip-green','chip-red'); chip.classList.add(data.mv_state==='open' ? 'chip-green' : 'chip-red'); }
                                    const btn = card.querySelector(`#mv-btn-${gid}`);
                                    if (btn){ btn.textContent = (data.mv_state === 'open' ? 'Закрыть мастер-клапан' : 'Открыть мастер-клапан'); btn.classList.add('delay'); }
                                }
                            } else if (typeof data.group_id !== 'undefined') {
                                // MQTT-агрегат по группе
                                const gid = Number(data.group_id);
                                const grp = (statusData && statusData.groups ? statusData.groups : []).find(g => Number(g.id)===gid);
                                if (grp){
                                    grp.status = String(data.status||'').toLowerCase();
                                    grp.current_zone = data.current_zone || null;
                                    refreshSingleGroup(gid);
                                }
                            }
                        }catch(e){}
                    };
                }catch(e){}
            }
            if (ENABLE_WS) connectWS();
            function connectSSE(){
                try { if (es) { try{ es.close(); }catch(e){} } } catch(e){}
                es = new EventSource('/api/mqtt/zones-sse');
                es.onopen = ()=>{ retry = 0; console.log('SSE open'); };
                es.onerror = ()=>{
                    try{ es.close(); }catch(e){}
                    const delay = Math.min(10000, 500 * Math.pow(2, Math.min(5, retry++)));
                    clearTimeout(reconnectTimer);
                    reconnectTimer = setTimeout(connectSSE, delay);
                    console.log('SSE error, reconnect in', delay);
                };
                es.onmessage = (ev)=>{
                    try{
                        const data = JSON.parse(ev.data);
                        if (typeof data.zone_id !== 'undefined') {
                            const idx = zonesData.findIndex(z=>z.id===data.zone_id);
                            if (idx>=0){ zonesData[idx].state = data.state; }
                            const gid = typeof data.group_id !== 'undefined' ? Number(data.group_id) : (idx>=0 ? zonesData[idx].group_id : null);
                            if (gid) { refreshSingleGroup(gid); }
                            if (String(data.state).toLowerCase() === 'off') { requestZonesReload(); }
                        } else if (typeof data.mv_group_id !== 'undefined') {
                            const gid = Number(data.mv_group_id);
                            const card = document.getElementById(`group-card-${gid}`);
                            if (card){
                                const span = card.querySelector(`#mv-state-${gid}`);
                                if (span) span.textContent = (data.mv_state === 'open' ? 'Открыт' : 'Закрыт');
                                const chip = card.querySelector(`#mv-chip-${gid}`);
                                if (chip){ chip.classList.remove('chip-green','chip-red'); chip.classList.add(data.mv_state==='open' ? 'chip-green' : 'chip-red'); }
                                const btn = card.querySelector(`#mv-btn-${gid}`);
                                if (btn){ btn.textContent = (data.mv_state === 'open' ? 'Закрыть мастер-клапан' : 'Открыть мастер-клапан'); btn.classList.add('delay'); }
                            }
                        } else if (typeof data.group_id !== 'undefined') {
                            // MQTT-агрегат по группе
                            const gid = Number(data.group_id);
                            const grp = (statusData && statusData.groups ? statusData.groups : []).find(g => Number(g.id)===gid);
                            if (grp){
                                grp.status = String(data.status||'').toLowerCase();
                                grp.current_zone = data.current_zone || null;
                                refreshSingleGroup(gid);
                            }
                        }
                    }catch(e){}
                };
            }
            connectSSE();
            document.addEventListener('visibilitychange', ()=>{
                if (document.visibilityState === 'visible'){
                    // re-arm SSE if needed
                    if (!es || es.readyState === EventSource.CLOSED) connectSSE();
                    if (ENABLE_WS) { try{ if (!ws || ws.readyState === WebSocket.CLOSED) connectWS(); }catch(e){} }
                }
            });
        } catch (e) {}
    });

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

    // Добавим/уберём админские колонки в заголовке по роли
    try {
        const head = document.getElementById('zones-table-head');
        if (head) {
            const isAdmin = !!(statusData && statusData.is_admin);
            const ths = head.querySelectorAll('tr th');
            const hasAdmin = head.querySelectorAll('th.admin-only').length > 0;
            if (isAdmin && !hasAdmin) {
                const tr = head.querySelector('tr');
                const thAvg = document.createElement('th'); thAvg.className = 'admin-only'; thAvg.innerHTML = 'Средний расход<br>(л/мин)';
                const thTot = document.createElement('th'); thTot.className = 'admin-only'; thTot.innerHTML = 'Расход (л)<br>за прошлый полив';
                tr.insertBefore(thAvg, tr.lastElementChild);
                tr.insertBefore(thTot, tr.lastElementChild);
                try { ensureAdminCellsInRows(); } catch(e){}
                try { fillAdminCellsFromZonesData(); } catch(e){}
            } else if (!isAdmin && hasAdmin) {
                head.querySelectorAll('th.admin-only').forEach(el=> el.remove());
            }
        }
    } catch(e) {}

    // --- Weather widget on dashboard ---
    async function refreshWeatherWidget() {
        try {
            const r = await fetch('/api/weather');
            const j = await r.json();
            const box = document.getElementById('weather-box');
            if (!box) return;
            if (!j || !j.available) { box.style.display = 'none'; return; }
            box.style.display = '';
            const tempEl = document.getElementById('weather-temp-val');
            const humEl = document.getElementById('weather-hum-val');
            const rainEl = document.getElementById('weather-rain-val');
            const coeffEl = document.getElementById('weather-coeff');
            const skipBadge = document.getElementById('weather-skip-badge');
            if (tempEl) tempEl.textContent = j.temperature !== null ? j.temperature.toFixed(1) : '—';
            if (humEl) humEl.textContent = j.humidity !== null ? j.humidity.toFixed(0) : '—';
            if (rainEl) rainEl.textContent = j.precipitation_24h !== null ? j.precipitation_24h.toFixed(1) : '—';
            if (coeffEl) {
                coeffEl.textContent = j.coefficient !== null && j.coefficient !== undefined ? j.coefficient : '—';
                // Color coding
                const c = j.coefficient || 100;
                if (c === 0) coeffEl.style.color = '#f44336';
                else if (c < 80) coeffEl.style.color = '#ff9800';
                else if (c > 120) coeffEl.style.color = '#2196f3';
                else coeffEl.style.color = '#4caf50';
            }
            if (skipBadge) {
                if (j.skip) {
                    skipBadge.style.display = 'inline';
                    skipBadge.title = j.skip_reason || '';
                } else {
                    skipBadge.style.display = 'none';
                }
            }
        } catch (e) { /* ignore weather fetch errors */ }
    }
    // Initial load + periodic refresh (every 5 min)
    refreshWeatherWidget();
    setInterval(refreshWeatherWidget, 5 * 60 * 1000);
