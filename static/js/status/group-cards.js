// status/group-cards.js — Group card rendering, patching, actions

    function getStatusText(group) {
        const mob = window.innerWidth < 1024;
        if (group.status === 'watering' && group.current_zone) {
            const src = String(group.current_zone_source || '').toLowerCase();
            if (src === 'schedule') return mob ? '💧 Полив (расписание)' : 'Полив - активно поливается (по расписанию)';
            return mob ? '💧 Полив (вручную)' : 'Полив - активно поливается (запущено вручную)';
        }
        switch (group.status) {
            case 'waiting': return mob ? '✅ Ожидание' : 'Ожидание - готов к поливу';
            case 'error': return mob ? '❌ Ошибка' : 'Ошибка - проблема с системой';
            case 'postponed': {
                const r = (group.postpone_reason || '').toString();
                if (r === 'rain') return mob ? '⏸ Дождь' : 'Отложено - полив отложен из-за дождя';
                if (r === 'manual') return mob ? '⏸ Отложено' : 'Отложено - полив отложен пользователем';
                if (r === 'emergency') return mob ? '⏸ Авария' : 'Отложено - полив отложен из-за аварии';
                return mob ? '⏸ Отложено' : 'Отложено - полив отложен';
            }
            default: return mob ? '✅ Ожидание' : 'Ожидание - готов к поливу';
        }
    }

    async function initGroupTimer(group) {
        const span = document.getElementById(`group-timer-${group.id}`);
        if (!span) return;
        // Try local calc first (instant, no fetch)
        try {
            var zone = group.current_zone ? (zonesData || []).find(function(z){ return z.id === group.current_zone; }) : null;
            if (zone && zone.planned_end_time) {
                var endDate = parseDate(zone.planned_end_time);
                if (endDate) {
                    var endMs = endDate.getTime();
                    var remain = Math.max(0, Math.floor((endMs - Date.now()) / 1000));
                    if (remain > 0) {
                        span.dataset.remainingSeconds = String(remain);
                        span.textContent = formatSeconds(remain);
                        return;
                    }
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
                // Don't overwrite if timer already has a valid value (drift correction handles it)
                if (!span.dataset.remainingSeconds || Number(span.dataset.remainingSeconds) <= 0) {
                    span.dataset.remainingSeconds = '';
                    span.textContent = '--:--';
                }
            }
        } catch (e) {
            // Don't overwrite working timer on fetch error
            if (!span.dataset.remainingSeconds || Number(span.dataset.remainingSeconds) <= 0) {
                span.textContent = '--:--';
            }
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
        const resumeBtn = document.getElementById('resume-btn');
        if (statusData.emergency_stop) { resumeBtn.style.display = 'inline-block'; } else { resumeBtn.style.display = 'none'; }

        // Track which group IDs are in the new data
        var newGroupIds = {};
        statusData.groups.forEach(function(g) { newGroupIds[String(g.id)] = true; });

        // Remove cards for groups that no longer exist
        container.querySelectorAll('[data-group-id]').forEach(function(el) {
            if (!newGroupIds[el.getAttribute('data-group-id')]) el.remove();
        });

        for (const group of statusData.groups) {
            const flowActive = group.status === 'watering';
            const newClassName = `card ${group.status} ${flowActive ? 'flow-active' : ''}`;
            const statusText = getStatusText(group);
            // Доп. информация: при поливе — зона и таймер; при отложке — дата/время; при ошибке — текст ошибки; иначе — '—'
            let extraText = '—';
            if (group.status === 'watering' && group.current_zone) {
                // Compute remaining time INLINE (sync) to avoid --:-- flash
                var _gZone = (zonesData || []).find(function(z){ return z.id === group.current_zone; });
                var _gRemain = 0;
                var _gTimerText = '--:--';
                if (_gZone && _gZone.planned_end_time) {
                    var _gEndD = parseDate(_gZone.planned_end_time);
                    if (_gEndD) {
                        // Use server-synced time to avoid timezone mismatch
                        var _gNow = Date.now() + (_serverTimeOffset || 0);
                        _gRemain = Math.max(0, Math.floor((_gEndD.getTime() - _gNow) / 1000));
                        if (_gRemain > 0) _gTimerText = formatSeconds(_gRemain);
                    }
                }
                var _gRemainAttr = _gRemain > 0 ? String(_gRemain) : '';
                extraText = `Зона ${group.current_zone}: осталось <span class="group-timer" id="group-timer-${group.id}" data-group-id="${group.id}" data-zone-id="${group.current_zone}" data-remaining-seconds="${_gRemainAttr}">${_gTimerText}</span>`;
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

            // DOM patching: reuse existing card if present
            let card = document.getElementById(`group-card-${group.id}`);
            const isNewCard = !card;
            if (isNewCard) {
                card = document.createElement('div');
                card.id = `group-card-${group.id}`;
                card.setAttribute('data-group-id', String(group.id));
            }
            card.className = newClassName;

            // For watering groups, preserve timer span if it exists (avoid DOM rebuild flash)
            const existingTimer = !isNewCard ? card.querySelector('.group-timer') : null;
            const hasActiveTimer = existingTimer !== null;

            if (group.status === 'watering' && group.current_zone && hasActiveTimer) {
                // Patch without destroying timer: update status text and buttons, keep timer span
                const statusEl = card.querySelector(`#group-status-${group.id}`);
                if (statusEl) statusEl.innerHTML = statusText;
                // Update buttons (they may change between start/stop)
                const btnGroups = card.querySelectorAll('.btn-group');
                if (btnGroups.length >= 2) {
                    btnGroups[0].innerHTML = `
                        <button class="delay" onclick="delayGroup(${group.id}, 1)">${_mob ? '1 день' : 'Остановить полив на 1 день'}</button>
                        <button class="delay" onclick="delayGroup(${group.id}, 2)">${_mob ? '2 дня' : 'Остановить полив на 2 дня'}</button>
                        <button class="delay" onclick="delayGroup(${group.id}, 3)">${_mob ? '3 дня' : 'Остановить полив на 3 дня'}</button>
                        ${group.status === 'postponed' && group.postpone_until && !statusData.emergency_stop ? `<button class="cancel-postpone" onclick="cancelPostpone(${group.id})">${_mob ? 'Продолжить' : 'Продолжить по расписанию'}</button>` : ''}`;
                    btnGroups[1].innerHTML = groupActionHtml;
                }
                // Update data-attributes for timer (zone may have changed)
                if (existingTimer) {
                    existingTimer.setAttribute('data-zone-id', String(group.current_zone));
                    // If timer has empty/zero remaining, recompute from zonesData
                    if (!existingTimer.dataset.remainingSeconds || Number(existingTimer.dataset.remainingSeconds) <= 0) {
                        var _pZone = (zonesData || []).find(function(z){ return z.id === group.current_zone; });
                        if (_pZone && _pZone.planned_end_time) {
                            var _pEnd = parseDate(_pZone.planned_end_time);
                            if (_pEnd) {
                                var _pNow = Date.now() + (_serverTimeOffset || 0);
                                var _pRemain = Math.max(0, Math.floor((_pEnd.getTime() - _pNow) / 1000));
                                if (_pRemain > 0) {
                                    existingTimer.dataset.remainingSeconds = String(_pRemain);
                                    existingTimer.textContent = formatSeconds(_pRemain);
                                }
                            }
                        }
                    }
                }
            } else {
                // Full rebuild of card content (first render or status changed)
                card.innerHTML = `
                    <div class="group-header">${escapeHtml(group.name)}</div>
                    <div id="group-status-${group.id}">${statusText}</div>
                    <div class="postpone-until">${extraText}</div>
                    ${groupButtons}
                    ${mvBlock}
                `;
                // No initGroupTimer call needed — remaining is computed inline above.
                // tickCountdowns() + drift correction handle ongoing updates.
            }
            if (isNewCard) {
                container.appendChild(card);
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
            const flowActive = group.status === 'watering';
            card.className = `card ${group.status} ${flowActive ? 'flow-active' : ''}`;
            const statusText = getStatusText(group);
            let extraText2 = '—';
            if (group.status === 'watering' && group.current_zone) {
                var _gZone2 = (zonesData || []).find(function(z){ return z.id === group.current_zone; });
                var _gRemain2 = 0;
                var _gTimerText2 = '--:--';
                if (_gZone2 && _gZone2.planned_end_time) {
                    var _gEndD2 = parseDate(_gZone2.planned_end_time);
                    if (_gEndD2) {
                        var _gNow2 = Date.now() + (_serverTimeOffset || 0);
                        _gRemain2 = Math.max(0, Math.floor((_gEndD2.getTime() - _gNow2) / 1000));
                        if (_gRemain2 > 0) _gTimerText2 = formatSeconds(_gRemain2);
                    }
                }
                var _gRemainAttr2 = _gRemain2 > 0 ? String(_gRemain2) : '';
                extraText2 = `Зона ${group.current_zone}: осталось <span class="group-timer" id="group-timer-${group.id}" data-group-id="${group.id}" data-zone-id="${group.current_zone}" data-remaining-seconds="${_gRemainAttr2}">${_gTimerText2}</span>`;
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
            const _m3 = window.innerWidth < 1024;
            const groupActionHtml2 = anyZoneOnThisGroup2
                ? `<button class=\"group-action-btn group-action-stop\" onclick=\"stopGroup(${group.id})\">${_m3 ? '⏹ Стоп' : 'Остановить полив группы'}</button>`
                : `<button class=\"group-action-btn group-action-start\" onclick=\"startGroupFromFirst(${group.id})\">${_m3 ? '▶ Запустить' : 'Запустить полив группы'}</button>`;
            const _mob2 = window.innerWidth < 1024;
            const groupButtons = `
                <div class=\"btn-group\">
                    <button class=\"delay\" onclick=\"delayGroup(${group.id}, 1)\">${_mob2 ? '1 день' : 'Остановить полив на 1 день'}</button>
                    <button class=\"delay\" onclick=\"delayGroup(${group.id}, 2)\">${_mob2 ? '2 дня' : 'Остановить полив на 2 дня'}</button>
                    <button class=\"delay\" onclick=\"delayGroup(${group.id}, 3)\">${_mob2 ? '3 дня' : 'Остановить полив на 3 дня'}</button>
                    ${group.status === 'postponed' && group.postpone_until && !statusData.emergency_stop ? `<button class=\"cancel-postpone\" onclick=\"cancelPostpone(${group.id})\">${_mob2 ? 'Продолжить' : 'Продолжить по расписанию'}</button>` : ''}
                </div>
                <div class=\"btn-group\" style=\"width:100%\">${groupActionHtml2}</div>`;
            card.innerHTML = `
                <div class="group-header">${escapeHtml(group.name)}</div>
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
                await loadZonesData().then(function(){ return loadStatusData(); });
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
                await loadZonesData().then(function(){ return loadStatusData(); });
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
                await loadZonesData().then(function(){ return loadStatusData(); });
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
                await loadZonesData().then(function(){ return loadStatusData(); });
                // Скрыть кнопку возобновления
                document.getElementById('resume-btn').style.display = 'none';
            } else {
                showNotification(data.message || 'Ошибка возобновления полива', 'error');
            }
        } catch (error) {
            showNotification('Ошибка возобновления полива', 'error');
        }
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
