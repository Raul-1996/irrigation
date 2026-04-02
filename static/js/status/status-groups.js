    // === Group card rendering: updateStatusDisplay and refreshSingleGroup ===

    async function initGroupTimer(group) {
        const span = document.getElementById(`group-timer-${group.id}`);
        if (!span) return;
        // Try local calc first (instant, no fetch)
        try {
            var zone = group.current_zone ? (zonesData || []).find(function(z){ return z.id === group.current_zone; }) : null;
            if (zone && zone.planned_end_time) {
                var endMs = new Date(zone.planned_end_time).getTime();
                var remain = Math.max(0, Math.floor((endMs - Date.now()) / 1000));
                if (remain > 0) {
                    span.dataset.remainingSeconds = String(remain);
                    span.textContent = formatSeconds(remain);
                    return;
                }
            }
        } catch(e) {}
        // Fallback: fetch from API
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

    // Helper: build group card inner HTML (shared between updateStatusDisplay and refreshSingleGroup)
    function _buildGroupCardContent(group) {
        const statusText = getStatusText(group);
        // Extra info
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
        const _m = window.innerWidth < 1024;
        const groupActionHtml = anyZoneOnThisGroup
            ? `<button class="group-action-btn group-action-stop" onclick="stopGroup(${group.id})">${_m ? '⏹ Стоп' : 'Остановить полив группы'}</button>`
            : `<button class="group-action-btn group-action-start" onclick="startGroupFromFirst(${group.id})">${_m ? '▶ Запустить' : 'Запустить полив группы'}</button>`;
        const _mob = window.innerWidth < 1024;
        const groupButtons = `
            <div class="btn-group">
                <button class="delay" onclick="delayGroup(${group.id}, 1)">${_mob ? '1 день' : 'Остановить полив на 1 день'}</button>
                <button class="delay" onclick="delayGroup(${group.id}, 2)">${_mob ? '2 дня' : 'Остановить полив на 2 дня'}</button>
                <button class="delay" onclick="delayGroup(${group.id}, 3)">${_mob ? '3 дня' : 'Остановить полив на 3 дня'}</button>
                ${group.status === 'postponed' && group.postpone_until && !statusData.emergency_stop ? `<button class="cancel-postpone" onclick="cancelPostpone(${group.id})">${_mob ? 'Продолжить' : 'Продолжить по расписанию'}</button>` : ''}
            </div>
            <div class="btn-group" style="width:100%">${groupActionHtml}</div>`;

        // Info grid (MV, pressure, flow)
        const _flag = v => { try { if (v===true||v===1) return true; const s=String(v).trim().toLowerCase(); return s==='1'||s==='true'||s==='on'||s==='yes'; } catch(e){ return false; } };
        const mvEnabled = (group.use_master_valve === true) || (group.use_master_valve === 1);
        const mvState = String(group.master_valve_state || 'unknown');
        const pressureOn = _flag(group.use_pressure_sensor);
        const flowOn = _flag(group.use_water_meter);
        const gridCells = [];
        if (mvEnabled) {
            const dotCls = mvState==='open' ? 'open' : (mvState==='closed' ? 'closed' : '');
            const actionText = (mvState==='open') ? 'Закрыть мастер-клапан' : 'Открыть мастер-клапан';
            const stateText = mvState==='open' ? 'Открыт' : (mvState==='closed' ? 'Закрыт' : '—');
            const mvBtn = `<button id="mv-btn-${group.id}" class="mv-button" data-mv-state="${mvState}" onclick="toggleMasterValve(${group.id})">
                    <span class="mv-action">${actionText}</span>
                    <span class="mv-dot ${dotCls}"></span>
                    <span class="mv-state-text">(${stateText})</span>
                </button>`;
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
        const mvBlock = gridCells.length ? `<div class="group-info-grid">${gridCells.join('')}${gridCells.length % 2 ? '<div class="grid-item"></div>' : ''}</div>` : '';

        return {
            statusText: statusText,
            html: `
                <div class="group-header">${escapeHtml(group.name)}</div>
                <div id="group-status-${group.id}">${statusText}</div>
                <div class="postpone-until">${extraText}</div>
                ${groupButtons}
                ${mvBlock}
            `
        };
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
            const built = _buildGroupCardContent(group);
            card.innerHTML = built.html;
            card.id = `group-card-${group.id}`;
            container.appendChild(card);
            if (group.status === 'watering' && group.current_zone) {
                initGroupTimer(group);
            }
        }
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
            const built = _buildGroupCardContent(group);
            card.innerHTML = built.html;
            if (group.status === 'watering' && group.current_zone) {
                initGroupTimer(group);
            }
        } catch (e) {}
    }

    // SSE handler
    function handleZoneUpdateFromSse(zoneId, newState) {
        try {
            const z = zonesData.find(x => Number(x.id) === Number(zoneId));
            if (z) z.state = newState;
            const groupId = z ? z.group_id : null;
            if (groupId) {
                refreshSingleGroup(groupId);
            } else {
                loadStatusData();
            }
        } catch (e) {}
    }
    
    async function updateZonesTable() {
        const tbody = document.getElementById('zones-table-body');
        const countSpan = document.getElementById('zones-count');
        
        if (!tbody) return; // V2: table removed, cards used instead
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
                 <td>${escapeHtml(zone.name)}</td>
                 <td>${escapeHtml(zone.icon)}</td>
                 <td>${zone.duration} мин</td>
                 <td>${escapeHtml(groupNameById[zone.group_id] || zone.group_id)}</td>
                 <td class="hide-mobile col-last-watering">—</td>
                 <td class="col-next" data-label="Следующий полив">${nextWatering}</td>
                 ${showWaterCols ? `<td class="admin-only">${avgFlow}</td>` : ''}
                 ${showWaterCols ? `<td class="admin-only">${totalLiters}</td>` : ''}
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
