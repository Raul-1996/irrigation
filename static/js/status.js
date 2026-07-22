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
    let envProbeExhausted = false;
    let zonesDataRevision = 0;
    let statusRequestGeneration = 0;
    let zonesRequestGeneration = 0;
    const connectionErrorFeeds = new Set();
    const pendingZoneStates = Object.create(null);
    const pendingZoneDurations = Object.create(null);

    function assertJsonResponse(response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
    }

    function isZoneCasConflict(response) {
        if (!response || typeof response !== 'object') return false;
        return response.error_code === 'ZONE_VERSION_CONFLICT'
            || response.error_code === 'EXPECTED_VERSION_REQUIRED';
    }

    function zoneCasConflictMessage(response) {
        if (response && response.error_code === 'EXPECTED_VERSION_REQUIRED') {
            return 'Версия зоны устарела или отсутствует. Загружены актуальные данные; повторите изменение.';
        }
        return 'Зона уже изменена в другом окне. Загружены актуальные данные; повторите изменение.';
    }

    async function recoverFromZoneCasConflict(response, closeEditor) {
        if (closeEditor && editingZoneId) closeZoneSheet();
        showZoneToast(zoneCasConflictMessage(response), 'warning');
        await loadZonesData();
    }

    function optimisticTimestamp(timestamp) {
        return new Date(timestamp === undefined ? Date.now() : timestamp).toISOString();
    }

    function invalidateLiveDataRequests() {
        statusRequestGeneration += 1;
        zonesRequestGeneration += 1;
    }

    function rememberOptimisticZoneState(zone, state, wateringStart, plannedEnd) {
        if (!zone) return null;
        var token = ++zonesDataRevision;
        pendingZoneStates[zone.id] = {
            token: token,
            state: state,
            watering_start_time: wateringStart,
            planned_end_time: plannedEnd,
            previous: {
                state: zone.state,
                watering_start_time: zone.watering_start_time,
                planned_end_time: zone.planned_end_time,
            },
            confirmed: false,
            expires_at: 0,
        };
        zone.state = state;
        zone.watering_start_time = wateringStart;
        zone.planned_end_time = plannedEnd;
        return token;
    }

    function applyPendingZoneStates(zones) {
        var now = Date.now();
        var reconciled = false;
        (zones || []).forEach(function(zone) {
            var pending = pendingZoneStates[zone.id];
            if (!pending) return;
            if (pending.confirmed && (zone.state === pending.state || now >= pending.expires_at)) {
                delete pendingZoneStates[zone.id];
                reconciled = true;
                return;
            }
            zone.state = pending.state;
            zone.watering_start_time = pending.watering_start_time;
            zone.planned_end_time = pending.planned_end_time;
        });
        if (reconciled) zonesDataRevision += 1;
    }

    function applyPendingZoneDurations(zones) {
        (zones || []).forEach(function(zone) {
            var pending = pendingZoneDurations[zone.id];
            if (!pending) return;
            zone.duration = pending.duration;
            // Preserve the caller-owned token while the local edit is pending.
            // Adopting a newer poll version here would silently rebase this edit
            // over somebody else's write instead of producing a 409 conflict.
            zone.version = pending.expectedVersion;
        });
    }

    function revertOptimisticZoneState(zoneId, token) {
        var pending = pendingZoneStates[zoneId];
        if (!pending || pending.token !== token) return;
        delete pendingZoneStates[zoneId];
        zonesDataRevision += 1;
        var zone = (zonesData || []).find(function(item) { return item.id === zoneId; });
        if (!zone) return;
        zone.state = pending.previous.state;
        zone.watering_start_time = pending.previous.watering_start_time;
        zone.planned_end_time = pending.previous.planned_end_time;
    }

    function reconcileOptimisticZoneState(zoneId, token) {
        var pending = pendingZoneStates[zoneId];
        if (!pending || pending.token !== token) return Promise.resolve();
        pending.confirmed = true;
        pending.expires_at = Date.now() + 15000;
        return loadZonesData();
    }
    
    // Функция обновления времени (локальное, без fetch)
    var _serverTimeOffset = 0;
    async function syncServerTime() {
        try {
            const r = await fetch('/api/server-time?ts=' + Date.now(), { cache: 'no-store' });
            const j = await r.json();
            if (j && j.now_iso) {
                var serverMs = new Date(String(j.now_iso).replace(' ','T')).getTime();
                if (Number.isFinite(serverMs)) _serverTimeOffset = serverMs - Date.now();
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
    
    async function loadStatusData() {
        var requestGeneration = ++statusRequestGeneration;
        try {
            var nextStatus = await fetch('/api/status?ts=' + Date.now(), { cache: 'no-store' })
                .then(assertJsonResponse);
            if (requestGeneration !== statusRequestGeneration) return false;
            if (!nextStatus || typeof nextStatus !== 'object' || !Array.isArray(nextStatus.groups)) {
                throw new Error('Invalid status response');
            }
            statusData = nextStatus;
            updateStatusDisplay();
            updateWaterMeter();
            try { updateZoneStats(zonesData); } catch (e) {}
            hideConnectionError('status');
            updateMqttWarnings();
            return true;
        } catch (error) {
            if (requestGeneration !== statusRequestGeneration) return false;
            console.error('Ошибка загрузки статуса:', error);
            showConnectionError('status');
            return false;
        }
    }
    
    async function loadZonesData() {
        var requestGeneration = ++zonesRequestGeneration;
        var requestRevision = zonesDataRevision;
        try {
            // Fetch zones + groups in PARALLEL
            var needGroups = !zoneGroupsCache || !zoneGroupsCache.length;
            var promises = [
                fetch('/api/zones?ts=' + Date.now(), { cache: 'no-store' }).then(assertJsonResponse),
            ];
            if (needGroups) {
                promises.push(fetch('/api/groups').then(assertJsonResponse));
            }
            var results = await Promise.all(promises);
            if (requestGeneration !== zonesRequestGeneration) return false;
            if (requestRevision !== zonesDataRevision) return false;
            if (!Array.isArray(results[0])) throw new Error('Invalid zones response');
            var prevNW = {};
            (zonesData || []).forEach(function(z) { if (z && z._nextWatering) prevNW[z.id] = z._nextWatering; });
            applyPendingZoneStates(results[0]);
            applyPendingZoneDurations(results[0]);
            zonesData = results[0];
            zonesData.forEach(function(z) { if (prevNW[z.id]) z._nextWatering = prevNW[z.id]; });
            if (needGroups && Array.isArray(results[1])) zoneGroupsCache = results[1];

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
                    if (!nwResp.ok || !nwData || nwData.success === false) {
                        throw new Error((nwData && (nwData.message || nwData.error)) || 'Next watering unavailable');
                    }
                    if (requestGeneration !== zonesRequestGeneration) return;
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
            try { updateWaterMeter(); } catch(e) {}

            hideConnectionError('zones');
            return true;
        } catch (error) {
            if (requestGeneration !== zonesRequestGeneration) return false;
            console.error('Ошибка загрузки зон:', error);
            showConnectionError('zones');
            return false;
        }
    }

    function showConnectionError(feed) {
        connectionErrorFeeds.add(feed || 'unknown');
        connectionError = connectionErrorFeeds.size > 0;
        var el = document.getElementById('connection-status');
        if (!el) return;
        var labels = { status: 'статус', zones: 'зоны', unknown: 'сервер' };
        var failed = Array.from(connectionErrorFeeds).map(function(name) { return labels[name] || name; });
        el.textContent = '⚠️ Нет связи: ' + failed.join(', ') + '. Проверьте подключение.';
        el.classList.add('show');
    }
    
    function hideConnectionError(feed) {
        if (feed) connectionErrorFeeds.delete(feed);
        else connectionErrorFeeds.clear();
        connectionError = connectionErrorFeeds.size > 0;
        var el = document.getElementById('connection-status');
        if (!el) return;
        if (!connectionError) {
            el.classList.remove('show');
            return;
        }
        var labels = { status: 'статус', zones: 'зоны', unknown: 'сервер' };
        var failed = Array.from(connectionErrorFeeds).map(function(name) { return labels[name] || name; });
        el.textContent = '⚠️ Нет связи: ' + failed.join(', ') + '. Проверьте подключение.';
    }

    function deriveMqttWarningState(data) {
        data = data && typeof data === 'object' ? data : {};
        const serverCount = Number(data.mqtt_servers_count || 0);
        const enabledCount = Number(data.mqtt_enabled_count || 0);
        const health = data.mqtt_health && typeof data.mqtt_health === 'object'
            ? String(data.mqtt_health.status || '').trim().toLowerCase()
            : '';
        const problemStates = new Set(['degraded', 'down', 'unhealthy', 'error', 'failed', 'critical', 'disconnected']);
        const authoritativeProblem = problemStates.has(health);
        // A degraded/error health result can accompany zero counts when the
        // server list itself could not be decrypted/read. Do not mislabel that
        // as "no servers configured".
        const noServers = authoritativeProblem
            ? false
            : (!Number.isFinite(serverCount) || serverCount <= 0);
        const hasEnabledServers = Number.isFinite(enabledCount) && enabledCount > 0;
        let connectionProblem = false;

        if (authoritativeProblem) {
            connectionProblem = true;
        } else if (!noServers && hasEnabledServers) {
            if (health) {
                // "disabled" and "unknown" are explicitly not proof of a
                // broker disconnect.  Only an authoritative unhealthy state
                // may trigger the red connection warning.
                connectionProblem = problemStates.has(health);
            } else {
                // Compatibility with older /api/status responses that did not
                // expose mqtt_health yet.
                connectionProblem = data.mqtt_connected === false;
            }
        }

        return {
            noServers: noServers,
            connectionProblem: connectionProblem,
            degraded: health === 'degraded',
        };
    }

    function updateMqttWarnings() {
        try {
            const warningState = deriveMqttWarningState(statusData);
            const elNoServers = document.getElementById('mqtt-no-servers');
            const elNoConn = document.getElementById('mqtt-no-connection');
            mqttNoServers = warningState.noServers;
            mqttNoConnection = warningState.connectionProblem;
            if (elNoServers) elNoServers.classList.toggle('show', mqttNoServers);
            if (elNoConn) {
                elNoConn.textContent = warningState.degraded
                    ? '⚠️ MQTT работает нестабильно. Проверьте подключение.'
                    : '⚠️ Нет связи с MQTT сервером. Проверьте подключение.';
                elNoConn.classList.toggle('show', mqttNoConnection);
            }
        } catch (e) {}
    }
    
    function formatSeconds(total) {
        const sec = Math.max(0, Math.floor(total));
        const mm = String(Math.floor(sec / 60)).padStart(2, '0');
        const ss = String(sec % 60).padStart(2, '0');
        return `${mm}:${ss}`;
    }

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

    function renderGroupExtraHtml(group, zones) {
        group = group && typeof group === 'object' ? group : {};
        zones = Array.isArray(zones) ? zones : [];
        if (group.status === 'watering' && group.current_zone) {
            const zone = zones.find(function(candidate) { return candidate.id === group.current_zone; });
            const zoneId = escapeHtml(group.current_zone);
            const zoneLabel = zone && zone.name
                ? `#${escapeHtml(zone.id)} ${escapeHtml(zone.name)}`
                : `#${zoneId}`;
            const groupId = escapeHtml(group.id);
            return `Зона ${zoneLabel}: осталось <span class="group-timer" id="group-timer-${groupId}" data-group-id="${groupId}" data-zone-id="${zoneId}" data-remaining-seconds="">--:--</span>`;
        }
        if (group.status === 'postponed' && group.postpone_until) {
            const postponeUntil = String(group.postpone_until);
            const safePostponeUntil = escapeHtml(postponeUntil);
            // postpone_reason is used only as an exact enum and is never
            // interpolated into HTML. Unknown/malicious values get the
            // generic, safely escaped presentation.
            const reason = String(group.postpone_reason || '').toLowerCase();
            if (reason === 'emergency' || postponeUntil.trim().toLowerCase().startsWith('до ')) {
                return safePostponeUntil;
            }
            return `До ${safePostponeUntil}`;
        }
        if (group.status === 'error' && group.error_message) {
            return escapeHtml(group.error_message);
        }
        return '—';
    }

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
    
    function rainSensorStatusText(data) {
        const source = data || {};
        const configuredState = String(source.rain_sensor_state || '').trim().toLowerCase();
        if (configuredState === 'disabled' || source.rain_enabled === false) {
            return 'датчик выключен';
        }
        if (configuredState === 'reconnecting') {
            return 'подключение восстанавливается — полив заблокирован';
        }
        if (configuredState === 'offline' || source.rain_sensor_online === false) {
            return 'нет связи с датчиком — полив заблокирован';
        }
        if (configuredState === 'unknown') {
            return 'нет данных — полив заблокирован';
        }
        if (configuredState === 'rain') return 'идёт дождь';
        if (configuredState === 'dry') return 'дождя нет';

        // Compatibility with controllers that have not yet exposed the state
        // enum.  Only an explicit legacy rain/dry value may become definitive.
        const legacy = String(source.rain_sensor || '').trim().toLowerCase();
        if (legacy.indexOf('идёт дожд') !== -1 || legacy.indexOf('идет дожд') !== -1) {
            return 'идёт дождь';
        }
        if (legacy.indexOf('нет дожд') !== -1 || legacy.indexOf('дождя нет') !== -1 || legacy === 'dry') {
            return 'дождя нет';
        }
        return 'нет данных — полив заблокирован';
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
        // Unknown/offline is safety-significant and must never look like dry.
        (function(){
            const rb = document.getElementById('rain-box');
            const rv = document.getElementById('rain-value');
            if (!rb || !rv) return;
            const state = String(statusData.rain_sensor_state || '').trim().toLowerCase();
            const disabled = state === 'disabled' || statusData.rain_enabled === false;
            if (disabled) {
                rb.style.display = 'none';
                return;
            }
            rb.style.display = 'inline-block';
            const text = rainSensorStatusText(statusData);
            rv.textContent = text;
            rv.dataset.state = text.indexOf('заблокирован') !== -1 ? 'degraded' : state;
            rv.classList.toggle('degraded', text.indexOf('заблокирован') !== -1);
        })();

        // Быстрый пробник: если включённый датчик показывает "нет данных",
        // опрашиваем /api/env чаще, но не более десяти раз за один offline-период.
        var envNeedsProbe = statusData.temperature === 'нет данных' || statusData.humidity === 'нет данных';
        if (!envNeedsProbe) {
            envProbeExhausted = false;
            if (envProbeTimer) { clearInterval(envProbeTimer); envProbeTimer = null; }
        }
        if (envNeedsProbe && !envProbeTimer && !envProbeExhausted) {
            envProbeAttempts = 0;
            envProbeTimer = setInterval(async () => {
                try {
                    if (envProbeAttempts >= 10) {
                        envProbeExhausted = true;
                        clearInterval(envProbeTimer); envProbeTimer = null;
                        return;
                    }
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
                    var tempReady = statusData.temperature === null
                        || typeof statusData.temperature === 'undefined'
                        || tv.textContent !== 'нет данных';
                    var humReady = statusData.humidity === null
                        || typeof statusData.humidity === 'undefined'
                        || hv.textContent !== 'нет данных';
                    if (tempReady && humReady) {
                        clearInterval(envProbeTimer); envProbeTimer = null;
                    } else if (envProbeAttempts >= 10) {
                        envProbeExhausted = true;
                        clearInterval(envProbeTimer); envProbeTimer = null;
                    }
                } catch (e) {
                    if (envProbeAttempts >= 10) {
                        envProbeExhausted = true;
                        clearInterval(envProbeTimer); envProbeTimer = null;
                    }
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
            const extraText = renderGroupExtraHtml(group, zonesData);
            const anyZoneOnThisGroup = (String(group.status||'').toLowerCase()==='watering' && group.current_zone);
            const _m = window.innerWidth < 1024;
            const skipBtnHtml = (anyZoneOnThisGroup && Number(group.queue_remaining || 0) > 0)
                ? `<button class="group-action-btn group-action-skip" onclick="skipCurrentZone(${group.id})">${_m ? '⏭ Пропустить' : 'Пропустить зону'}</button>`
                : '';
            const groupActionHtml = anyZoneOnThisGroup
                ? `<button class="group-action-btn group-action-stop" onclick="stopGroup(${group.id})">${_m ? '⏹ Стоп' : 'Остановить полив группы'}</button>${skipBtnHtml}`
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
                gridCells.push(`<div class="grid-item grid-item-span2"><div class="info-chip"><span class="label">Давление:</span> <span id="pressure-${group.id}">${escapeHtml((group.pressure_value!=null&&group.pressure_value!=='')?group.pressure_value:'—')}</span> ${escapeHtml(group.pressure_unit||'')}</div></div>`);
            } else if (!pressureOn && flowOn) {
                const meter = (typeof group.meter_value_m3 !== 'undefined' && group.meter_value_m3 !== null) ? String(group.meter_value_m3) : '—';
                const flow = (typeof group.flow_value !== 'undefined' && group.flow_value !== null && group.flow_value !== '') ? String(group.flow_value) : '—';
                gridCells.push(`<div class="grid-item grid-item-span2"><div class="info-chip"><span class="label">Счётчик:</span> <span id="meter-${group.id}">${escapeHtml(meter)}</span> м³ (<span id="flow-${group.id}">${escapeHtml(flow)}</span> л/мин)</div></div>`);
            } else {
                if (pressureOn) {
                    gridCells.push(`<div class="grid-item"><div class="info-chip"><span class="label">Давление:</span> <span id="pressure-${group.id}">${escapeHtml((group.pressure_value!=null&&group.pressure_value!=='')?group.pressure_value:'—')}</span> ${escapeHtml(group.pressure_unit||'')}</div></div>`);
                }
                if (flowOn) {
                    const meter = (typeof group.meter_value_m3 !== 'undefined' && group.meter_value_m3 !== null) ? String(group.meter_value_m3) : '—';
                    const flow = (typeof group.flow_value !== 'undefined' && group.flow_value !== null && group.flow_value !== '') ? String(group.flow_value) : '—';
                    gridCells.push(`<div class="grid-item"><div class="info-chip"><span class="label">Счётчик:</span> <span id="meter-${group.id}">${escapeHtml(meter)}</span> м³ (<span id="flow-${group.id}">${escapeHtml(flow)}</span> л/мин)</div></div>`);
                }
            }
            // pad to keep even number of cells for 2x2 symmetry on desktop
            const mvBlock = gridCells.length ? `<div class="group-info-grid">${gridCells.join('')}${gridCells.length % 2 ? '<div class="grid-item"></div>' : ''}</div>` : '';
            card.innerHTML = `
                <div class="group-header">${escapeHtml(group.name)}</div>
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
        // Tick zone card timers.
        // Source of truth: planned_end_time (wall-clock derivative). Avoids drift
        // between local 1Hz decrement and the 5-second status refresh, which
        // previously caused the timer to jump backward by ~1s every refresh.
        document.querySelectorAll('.zc-running-timer').forEach(function(el) {
            var val = el.dataset.remainingSeconds;
            if (!val) return;
            var zid = el.id.replace('ztimer-', '');
            var zone = (zonesData || []).find(function(z) { return String(z.id) === zid; });
            var sec;
            if (zone && zone.planned_end_time) {
                var endMs = new Date(zone.planned_end_time).getTime();
                sec = Math.max(0, Math.floor((endMs - Date.now()) / 1000));
            } else {
                sec = Number(val);
                if (isNaN(sec)) sec = 0;
                else sec = Math.max(0, sec - 1);
            }
            if (sec <= 0) { el.textContent = '00:00'; el.dataset.remainingSeconds = ''; return; }
            el.dataset.remainingSeconds = String(sec);
            el.textContent = formatSeconds(sec);
            // Update progress bar
            var progEl = document.getElementById('zprog-' + zid);
            if (progEl && zone) {
                var total;
                if (zone.planned_end_time && zone.watering_start_time) {
                    var endMs2 = new Date(zone.planned_end_time).getTime();
                    var startMs = new Date(zone.watering_start_time).getTime();
                    total = Math.max(60, Math.floor((endMs2 - startMs) / 1000));
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
            span.dataset.remainingSeconds = String(sec);
            span.textContent = formatSeconds(sec);
        });
    }

    async function refreshSingleGroup(groupId) {
        const requestGeneration = ++statusRequestGeneration;
        try {
            const resp = await fetch(`/api/status?ts=${Date.now()}`, {cache: 'no-store'});
            const data = await assertJsonResponse(resp);
            if (requestGeneration !== statusRequestGeneration) return false;
            if (!data || !Array.isArray(data.groups)) throw new Error('Invalid status response');
            // /api/status is one coherent snapshot for every group.  Even when
            // an action originated from a single card, render the whole winning
            // snapshot so sibling cards cannot remain older than statusData.
            statusData = data;
            updateStatusDisplay();
            updateWaterMeter();
            try { updateZoneStats(zonesData); } catch (e) {}
            hideConnectionError('status');
            updateMqttWarnings();
            return true;
        } catch (e) {
            if (requestGeneration !== statusRequestGeneration) return false;
            showConnectionError('status');
            return false;
        }
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
                // Точечно обновляем карточку группы
                await refreshSingleGroup(groupId);
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
                // Точечно обновляем карточку группы
                await refreshSingleGroup(groupId);
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
                await Promise.all([loadStatusData(), loadZonesData()]);
            } else {
                showNotification(data.message || `Ошибка запуска группы "${gname}"`, 'error');
            }
        } catch (error) {
            showNotification('Ошибка при запуске полива группы', 'error');
        }
    }
    
    // Module-scoped debounce for skip-zone — one in-flight per group.
    // Absorbs double-clicks during the server's ~1-2s zone transition window.
    const _skipInFlight = new Set();
    async function skipCurrentZone(groupId) {
        const key = String(groupId);
        if (_skipInFlight.has(key)) return;
        _skipInFlight.add(key);
        try {
            const res = await fetch(`/api/groups/${groupId}/skip-current`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}'
            });
            const data = await res.json().catch(() => ({}));
            if (res.ok && data && data.success) {
                showNotification('Зона пропущена', 'success');
                await Promise.all([loadStatusData(), loadZonesData()]);
            } else {
                showNotification((data && data.message) || 'Не удалось пропустить зону', 'warning');
            }
        } catch (e) {
            showNotification('Ошибка при пропуске зоны', 'error');
        } finally {
            setTimeout(() => _skipInFlight.delete(key), 1500);
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
                await Promise.all([loadStatusData(), loadZonesData()]);
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
            if (groupId) {
                // Обновим карточку группы без полной перезагрузки
                refreshSingleGroup(groupId);
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
        invalidateLiveDataRequests();
        try {
            const res = await fetch('/api/emergency-stop', { method: 'POST' });
            const data = await res.json();
            if (data && data.success) {
                showNotification(data.message, 'warning');
                await Promise.all([loadStatusData(), loadZonesData()]);
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
        invalidateLiveDataRequests();
        try {
            const res = await fetch('/api/emergency-resume', { method: 'POST' });
            const data = await res.json();
            if (data && data.success) {
                showNotification(data.message, 'success');
                await Promise.all([loadStatusData(), loadZonesData()]);
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
            await Promise.all([loadStatusData(), loadZonesData()]);
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

    // Issue #11: outside-click + Esc handlers for the lightbox.
    // Wired once at IIFE setup time (module is included once per page load).
    (function () {
        var modal = document.getElementById('photoModal');
        if (!modal) return;
        modal.addEventListener('click', function (e) {
            if (e.target === modal) closePhotoModal();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && modal.style.display === 'flex') {
                closePhotoModal();
            }
        });
    })();

    // ===== Photo upload/delete/rotate (issue #6) =====
    // Parallel implementation to zones.js (separate IIFE scope).
    // Backend: POST/DELETE /api/zones/{id}/photo, POST /api/zones/{id}/photo/rotate
    var currentStatusPhotoZoneId = null;

    // Bump cache-busting timestamp on a zone in zonesData so img URLs reload.
    function _bumpZonePhotoTs(zoneId) {
        var z = (zonesData || []).find(function(zz) { return zz.id === zoneId; });
        if (z) z._photoTs = Date.now();
    }

    // After mutation: reload zones from server, then re-render and refresh sheet preview.
    async function _afterPhotoMutation(zoneId) {
        try { await loadZonesData(); } catch (e) {}
        // Preserve cache-bust timestamp set before reload (loadZonesData may overwrite array).
        _bumpZonePhotoTs(zoneId);
        try { renderZoneCards(); } catch (e) {}
        if (editingZoneId === zoneId) {
            var z = (zonesData || []).find(function(zz) { return zz.id === zoneId; });
            if (z) refreshSheetPhotoPreview(z);
        }
    }

    function uploadStatusPhoto(zoneId) {
        // If invoked from sheet without arg, use editingZoneId.
        var id = zoneId || editingZoneId;
        if (!id) return;
        currentStatusPhotoZoneId = id;
        var input = document.getElementById('photoInputStatus');
        if (input) input.click();
    }

    async function handleStatusPhotoUpload(event) {
        var file = event.target.files && event.target.files[0];
        var zoneId = currentStatusPhotoZoneId;
        // Always clear input + state, even on early return, to allow retrying same file.
        event.target.value = '';
        currentStatusPhotoZoneId = null;
        if (!file || !zoneId) return;

        if (!file.type || !file.type.startsWith('image/')) {
            showZoneToast('Выберите изображение', 'error');
            return;
        }
        if (file.size > 20 * 1024 * 1024) {
            showZoneToast('Файл больше 20 МБ', 'error');
            return;
        }

        var formData = new FormData();
        formData.append('photo', file);

        showZoneToast('Загрузка фото...', 'info');
        try {
            var resp = await fetch('/api/zones/' + zoneId + '/photo', {
                method: 'POST',
                body: formData
            });
            if (resp.ok) {
                _bumpZonePhotoTs(zoneId);
                showZoneToast('✅ Фото загружено', 'success');
                await _afterPhotoMutation(zoneId);
            } else {
                var err = {};
                try { err = await resp.json(); } catch (e) {}
                showZoneToast(err.message || 'Ошибка загрузки', 'error');
            }
        } catch (e) {
            showZoneToast('Ошибка загрузки', 'error');
        }
    }

    async function deleteStatusPhoto(zoneId) {
        var id = zoneId || editingZoneId;
        if (!id) return;
        if (!confirm('Удалить фото этой зоны?')) return;

        var btn = document.getElementById('sheetPhotoDeleteBtn');
        if (btn) btn.disabled = true;
        try {
            var resp = await fetch('/api/zones/' + id + '/photo', { method: 'DELETE' });
            if (resp.ok) {
                _bumpZonePhotoTs(id);
                showZoneToast('🗑 Фото удалено', 'success');
                await _afterPhotoMutation(id);
            } else {
                var err = {};
                try { err = await resp.json(); } catch (e) {}
                showZoneToast(err.message || 'Ошибка удаления', 'error');
            }
        } catch (e) {
            showZoneToast('Ошибка удаления', 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    async function rotateStatusPhoto(angle) {
        // Always operates on the zone currently being edited via the sheet.
        var id = editingZoneId;
        if (!id) {
            showZoneToast('Нет активной зоны для поворота', 'error');
            return;
        }

        var btn = document.getElementById('sheetPhotoRotateBtn');
        if (btn) btn.disabled = true;
        try {
            var resp = await fetch('/api/zones/' + id + '/photo/rotate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ angle: angle })
            });
            var data = {};
            try { data = await resp.json(); } catch (e) {}
            if (resp.ok && data && data.success) {
                _bumpZonePhotoTs(id);
                showZoneToast('Фото повёрнуто', 'success');
                await _afterPhotoMutation(id);
            } else {
                showZoneToast((data && data.message) || 'Ошибка поворота', 'error');
            }
        } catch (e) {
            showZoneToast('Ошибка поворота', 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
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
        // Обновляем время сразу (синхронизируем с сервером один раз)
        syncServerTime();
        updateDateTime();
        
        // SSR: instant render from inline data (zero fetch)
        if (window._ssrZones && window._ssrZones.length) {
            zonesData = window._ssrZones;
            zoneGroupsCache = window._ssrGroups || [];
            renderGroupTabs();
            renderZoneCards();
            try { updateActiveZoneIndicator(zonesData); } catch(e) {}
            try { updateWaterMeter(); } catch(e) {}
            // Then refresh in background for live data
            setTimeout(function() {
                Promise.all([loadStatusData(), loadZonesData()]).catch(function(){});
            }, 1000);
        } else {
            // No SSR data, fetch normally
            Promise.all([loadStatusData(), loadZonesData()]).catch(function(){});
        }
        
        // Синхронизация времени раз в 5 минут
        setInterval(syncServerTime, 5 * 60 * 1000);
        
        // Обновление времени каждую секунду
        setInterval(updateDateTime, 1000);
        
        // Обновление данных каждые 5 секунд
        setInterval(() => {
            Promise.all([loadStatusData(), loadZonesData()]).catch(function(){});
        }, 5000);
        setInterval(tickCountdowns, 1000);
        
        // Обработчик аварийной остановки
        document.getElementById('emergency-btn').addEventListener('click', emergencyStop);
        document.getElementById('resume-btn').addEventListener('click', resumeSchedule);
        // SSE disabled — polling every 5s provides updates; SSE caused event loop death on ARM
        // MQTT→DB sync still works via sse_hub backend (no browser SSE connections)
    });


    // Export V2 zone functions to global scope for onclick handlers
    window.selectZoneGroup = selectZoneGroup;
    window.toggleZoneSearch = toggleZoneSearch;
    window.filterZonesBySearch = filterZonesBySearch;
    window.runSelectedGroup = runSelectedGroup;
    window.closeZoneSheet = closeZoneSheet;
    window.saveZoneEdit = saveZoneEdit;
    window.toggleZoneCard = toggleZoneCard;
    window.showPhotoModal = showPhotoModal;
    window.closePhotoModal = closePhotoModal;
    window.startOrStopZone = startOrStopZone;
    // Photo upload/delete/rotate (issue #6)
    window.uploadStatusPhoto = uploadStatusPhoto;
    window.handleStatusPhotoUpload = handleStatusPhotoUpload;
    window.deleteStatusPhoto = deleteStatusPhoto;
    window.rotateStatusPhoto = rotateStatusPhoto;

    // --- Weather Widget (full) ---
    var WEATHER_ICONS = {
        0: '☀️', 1: '🌤️', 2: '⛅', 3: '☁️',
        45: '🌫️', 48: '🌫️',
        51: '🌦️', 53: '🌦️', 55: '🌧️',
        56: '🌧️', 57: '🌧️',
        61: '🌧️', 63: '🌧️', 65: '🌧️',
        66: '🌧️', 67: '🌧️',
        71: '❄️', 73: '❄️', 75: '❄️', 77: '❄️',
        80: '🌦️', 81: '🌧️', 82: '🌧️',
        85: '🌨️', 86: '🌨️',
        95: '⛈️', 96: '⛈️', 99: '⛈️'
    };
    var WEATHER_DESCS = {
        0: 'Ясно', 1: 'Малооблачно', 2: 'Переменная облачность', 3: 'Пасмурно',
        45: 'Туман', 48: 'Изморозь',
        51: 'Слабая морось', 53: 'Морось', 55: 'Сильная морось',
        56: 'Морось с заморозком', 57: 'Сильная морось с заморозком',
        61: 'Небольшой дождь', 63: 'Дождь', 65: 'Сильный дождь',
        66: 'Дождь с заморозком', 67: 'Сильный дождь с заморозком',
        71: 'Небольшой снег', 73: 'Снег', 75: 'Сильный снег', 77: 'Снежная крупа',
        80: 'Кратковр. дождь', 81: 'Ливень', 82: 'Сильный ливень',
        85: 'Снегопад', 86: 'Сильный снегопад',
        95: 'Гроза', 96: 'Гроза с градом', 99: 'Сильная гроза с градом'
    };

    function getWeatherIcon(code) {
        return WEATHER_ICONS[code] || '🌡️';
    }
    function getWeatherDesc(code) {
        return WEATHER_DESCS[code] || '';
    }
    function formatTemp(v) {
        if (v === null || v === undefined) return '—';
        var numeric = Number(v);
        if (!Number.isFinite(numeric)) return '—';
        var sign = numeric > 0 ? '+' : '';
        return sign + Math.round(numeric);
    }
    function formatWeatherNumber(v, digits, fallback) {
        var fallbackValue = fallback === undefined ? '—' : fallback;
        if (v === null || v === undefined || v === '') return fallbackValue;
        var numeric = Number(v);
        if (!Number.isFinite(numeric)) return fallbackValue;
        return digits == null ? String(numeric) : numeric.toFixed(digits);
    }
    function formatWeatherMetric(v, digits, unit, fallback) {
        var number = formatWeatherNumber(v, digits, '');
        return number === '' ? (fallback === undefined ? '—' : fallback) : number + unit;
    }
    function coeffColor(c) {
        if (c === 0) return 'var(--danger-color, #f44336)';
        if (c < 80) return 'var(--warning-color, #ff9800)';
        if (c > 120) return 'var(--primary-color, #2196f3)';
        return 'var(--success-color, #4caf50)';
    }
    // H1 Zimmerman is the only applied coefficient.  H2 remains a dated shadow
    // diagnostic and must never be presented as the active watering mode.
    function renderCoeffSecondOpinion(adj) {
        if (!adj) return '';
        var status = String(adj.balance_status || 'unavailable').toLowerCase();
        var numericBalance = Number(adj.coefficient_balance);
        var hasBalance = adj.coefficient_balance !== null
            && adj.coefficient_balance !== undefined
            && Number.isFinite(numericBalance);
        var date = adj.balance_last_recalc_date
            ? escapeHtml(String(adj.balance_last_recalc_date))
            : '';
        var age = Number(adj.balance_age_days);
        var detail;
        if (status === 'fresh' && hasBalance) {
            detail = numericBalance + '% · свежий' + (date ? ' · пересчёт ' + date : '');
        } else if (status === 'stale' && hasBalance) {
            detail = numericBalance + '% · устарел'
                + (Number.isFinite(age) ? ' (' + Math.max(0, Math.round(age)) + ' дн.)' : '')
                + (date ? ' · пересчёт ' + date : '');
        } else if (status === 'future') {
            detail = (hasBalance ? numericBalance + '% · ' : '')
                + 'дата из будущего'
                + (date ? ' · ' + date : '');
        } else {
            detail = 'нет данных' + (date ? ' · последний пересчёт ' + date : '');
        }
        var html = '<span class="coeff-mode-badge">Зимм. (H1)</span>';
        html += ' <span class="coeff-second">H2 shadow: ' + detail + '</span>';
        return html;
    }
    function formatCacheAge(sec) {
        if (!sec && sec !== 0) return '';
        if (sec < 60) return Math.round(sec) + ' сек назад';
        return Math.round(sec / 60) + ' мин назад';
    }
    function formatSourceLine(j) {
        var parts = [];
        var sensors = j.sensors || {};
        if (sensors.temperature && sensors.temperature.online) parts.push('📡 WB-MSW (t°)');
        else if (sensors.temperature && sensors.temperature.enabled && !sensors.temperature.online) parts.push('⚠️📡 WB-MSW (offline)');
        if (sensors.rain && sensors.rain.enabled) parts.push('📡 (🌧)');
        parts.push('🌐 Open-Meteo');
        if (j.cache_age_sec !== undefined && j.cache_age_sec !== null) parts.push(formatCacheAge(j.cache_age_sec));
        return parts.join(' · ');
    }
    // Issue #33: причина пропуска полива из weather adjustment приходит латиницей
    // (rain_skip, freeze_skip, ...). Маппим в человекочитаемый русский — UI на главной.
    function localizeWeatherReason(reason) {
        if (!reason) return '';
        var s = String(reason);
        var head = s.split(':', 1)[0].trim().toLowerCase();
        var tail = s.indexOf(':') >= 0 ? s.slice(s.indexOf(':') + 1).trim() : '';
        var map = {
            'rain_skip': 'Дождь',
            'rain_forecast_skip': 'Прогноз дождя',
            'freeze_skip': 'Заморозки',
            'freeze_forecast_skip': 'Прогноз заморозков',
            'wind_skip': 'Ветер',
            'wind_forecast_skip': 'Прогноз ветра'
        };
        var label = map[head];
        if (!label) return s; // незнакомая причина — отдаём как есть
        return tail ? (label + ': ' + tail) : label;
    }
    // Issue #33b: фраза "из-за <причина>" — для виджета "Полив отложен (из-за дождя)".
    function weatherReasonPhrase(reason) {
        if (!reason) return '';
        var head = String(reason).split(':', 1)[0].trim().toLowerCase();
        var map = {
            'rain_skip': 'из-за дождя',
            'rain_forecast_skip': 'из-за прогноза дождя',
            'freeze_skip': 'из-за заморозков',
            'freeze_forecast_skip': 'из-за прогноза заморозков',
            'wind_skip': 'из-за ветра',
            'wind_forecast_skip': 'из-за прогноза ветра'
        };
        return map[head] || '';
    }

    function renderWeatherSummary(j) {
        var cur = j.current || {};
        var adj = j.adjustment || {};
        // Icon + desc
        var code = cur.weather_code;
        if (code === undefined || code === null) code = j.weather_code;
        var iconEl = document.getElementById('w-icon');
        var descEl = document.getElementById('w-desc');
        if (iconEl) iconEl.textContent = (code !== undefined && code !== null) ? getWeatherIcon(code) : '🌡️';
        if (descEl) descEl.textContent = (cur.weather_desc) ? cur.weather_desc : ((code !== undefined && code !== null) ? getWeatherDesc(code) : '');
        // Temperature
        var tempVal = (cur.temperature && cur.temperature.value !== undefined) ? cur.temperature.value : j.temperature;
        var tempEl = document.getElementById('w-temp');
        if (tempEl) tempEl.textContent = formatTemp(tempVal);
        // H1 is applied; H2 is rendered below only as a dated shadow value.
        var coeffApplied = (adj.coefficient_legacy !== undefined && adj.coefficient_legacy !== null)
            ? adj.coefficient_legacy
            : ((adj.coefficient_applied !== undefined && adj.coefficient_applied !== null)
                ? adj.coefficient_applied
                : ((adj.coefficient !== undefined) ? adj.coefficient : j.coefficient));
        var skip = (adj.skip !== undefined) ? adj.skip : j.skip;
        var coeffEl = document.getElementById('w-coeff');
        var coeffModeEl = document.getElementById('w-coeff-mode');
        if (coeffEl) {
            if (skip) {
                var skipReason = adj.skip_reason || j.skip_reason;
                var phrase = weatherReasonPhrase(skipReason);
                coeffEl.innerHTML = '<span class="skip-main">Полив отложен</span>'
                    + (phrase ? '<span class="skip-reason">(' + escapeHtml(phrase) + ')</span>' : '');
                coeffEl.style.color = 'var(--danger-color, #f44336)';
                coeffEl.classList.add('skip');
                if (coeffModeEl) coeffModeEl.textContent = '';
            } else {
                coeffEl.textContent = (coeffApplied !== null && coeffApplied !== undefined) ? coeffApplied + '%' : '—';
                var numericApplied = Number(coeffApplied);
                coeffEl.style.color = coeffColor(Number.isFinite(numericApplied) ? numericApplied : 100);
                coeffEl.classList.remove('skip');
                if (coeffModeEl) coeffModeEl.innerHTML = renderCoeffSecondOpinion(adj);
            }
        }
        // Metrics
        var humVal = (cur.humidity && cur.humidity.value !== undefined) ? cur.humidity.value : j.humidity;
        var windVal = (cur.wind_speed && cur.wind_speed.value !== undefined) ? cur.wind_speed.value : j.wind_speed;
        var precipVal = (j.stats && j.stats.precipitation_24h !== undefined) ? j.stats.precipitation_24h : j.precipitation_24h;
        var metricsEl = document.getElementById('w-metrics');
        if (metricsEl) {
            metricsEl.innerHTML = '<span>💧 ' + escapeHtml(formatWeatherMetric(humVal, 0, '%')) + '</span>'
                + '<span>💨 ' + escapeHtml(formatWeatherMetric(windVal, 1, ' м/с')) + '</span>'
                + '<span>🌧 ' + escapeHtml(formatWeatherMetric(precipVal, 1, ' мм')) + '</span>';
        }
        // Source
        var srcEl = document.getElementById('w-source');
        if (srcEl) srcEl.textContent = formatSourceLine(j);
    }

    function renderForecast24h(items) {
        var el = document.getElementById('w-hours');
        if (!el) return;
        if (!items || !items.length) { el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Нет данных</div>'; return; }
        // Фильтруем до 6 интервалов (каждый 4-й час)
        var filtered = [];
        if (items.length >= 18) {
            for (var i = 0; i < items.length; i += 4) {
                filtered.push(items[i]);
                if (filtered.length >= 6) break;
            }
        } else {
            filtered = items.slice(0, 6);
        }
        var html = '';
        for (var i = 0; i < filtered.length; i++) {
            var it = filtered[i];
            var icon = it.icon || getWeatherIcon(it.weather_code);
            html += '<div class="hour-cell">'
                + '<div class="hour-time">' + escapeHtml(it.time || '') + '</div>'
                + '<div class="hour-icon">' + escapeHtml(icon) + '</div>'
                + '<div class="hour-temp">' + escapeHtml(formatTemp(it.temp)) + '°</div>'
                + '<div class="hour-detail">'
                + escapeHtml(formatWeatherNumber(it.precip, 1, '0')) + 'мм · '
                + escapeHtml(formatWeatherMetric(it.wind, 1, 'м/с'))
                + '</div></div>';
        }
        el.innerHTML = html;
    }

    function renderForecast3d(items) {
        var el = document.getElementById('w-days');
        if (!el) return;
        if (!items || !items.length) { el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Нет данных</div>'; return; }
        var html = '';
        for (var i = 0; i < items.length; i++) {
            var it = items[i];
            var icon = it.icon || getWeatherIcon(it.weather_code);
            html += '<div class="weather-day">'
                + '<span class="weather-day-dow">' + escapeHtml(it.day_name || '') + '</span>'
                + '<span class="weather-day-icon">' + escapeHtml(icon) + '</span>'
                + '<span class="weather-day-temps">' + escapeHtml(formatTemp(it.temp_min)) + '° / ' + escapeHtml(formatTemp(it.temp_max)) + '°</span>'
                + '<span class="weather-day-rain">🌧 ' + escapeHtml(formatWeatherNumber(it.precip_sum, 1, '0')) + ' мм</span>'
                + '</div>';
        }
        el.innerHTML = html;
    }

    function renderWeatherDetails(j) {
        var el = document.getElementById('w-details');
        if (!el) return;
        var stats = j.stats || {};
        var astro = j.astronomy || {};
        var precipFc = (stats.precipitation_forecast_6h !== undefined) ? stats.precipitation_forecast_6h : j.precipitation_forecast_6h;
        var precip24 = (stats.precipitation_24h !== undefined) ? stats.precipitation_24h : j.precipitation_24h;
        var et0 = (stats.daily_et0 !== undefined) ? stats.daily_et0 : j.daily_et0;
        var html = '<div class="weather-params">';
        if (astro.sunrise) html += '<span class="weather-params-label">🌅 Восход</span><span class="weather-params-val">' + escapeHtml(astro.sunrise) + '</span>';
        if (astro.sunset) html += '<span class="weather-params-label">🌇 Закат</span><span class="weather-params-val">' + escapeHtml(astro.sunset) + '</span>';
        html += '<span class="weather-params-label">🌧 Осадки 24ч</span><span class="weather-params-val">' + escapeHtml(formatWeatherMetric(precip24, 1, ' мм')) + '</span>';
        html += '<span class="weather-params-label">🔮 Прогноз 6ч</span><span class="weather-params-val">' + escapeHtml(formatWeatherMetric(precipFc, 1, ' мм')) + '</span>';
        html += '<span class="weather-params-label">🔬 ET₀</span><span class="weather-params-val">' + escapeHtml(formatWeatherMetric(et0, 2, ' мм/день')) + '</span>';
        html += '</div>';
        el.innerHTML = html;
    }

    function renderWeatherFactors(adj) {
        var el = document.getElementById('w-factors');
        if (!el) return;
        var factors = adj.factors || {};
        var factorNames = {
            rain: '🌧️ Дождь',
            heat: '🌡️ Жара',
            freeze: '❄️ Заморозок',
            wind: '💨 Ветер',
            humidity: '💧 Влажность'
        };
        var order = ['rain', 'heat', 'freeze', 'wind', 'humidity'];
        var html = '';
        for (var i = 0; i < order.length; i++) {
            var key = order[i];
            var f = factors[key];
            if (!f) continue;
            var statusCls = 'wf-ok';
            if (f.status === 'warn') statusCls = 'wf-warn';
            else if (f.status === 'danger') statusCls = 'wf-danger';
            var statusMark = f.status === 'ok' ? '✓' : (f.status === 'danger' ? '✕' : '⚠');
            html += '<div class="weather-factor">'
                + '<div class="weather-factor-row">'
                + '<span class="weather-factor-name">' + escapeHtml(factorNames[key] || key) + '</span>'
                + '<span class="weather-factor-status ' + statusCls + '">' + statusMark + ' ' + escapeHtml(f.detail || '') + '</span>'
                + '</div></div>';
        }
        // Summary line
        var coeff = adj.coefficient;
        var skip = adj.skip;
        if (coeff !== undefined || skip) {
            var summaryColor = skip ? 'var(--danger-color, #f44336)' : 'var(--success-color, #4caf50)';
            var skipPhrase = weatherReasonPhrase(adj.skip_reason || '');
            var summaryText = skip
                ? ('Полив отложен' + (skipPhrase ? ' ' + skipPhrase : ''))
                : ('Коэффициент: ' + formatWeatherNumber(coeff, 0, '—') + '%');
            html += '<div style="margin-top:0.5rem;padding:0.4rem;background:rgba(33,150,243,0.08);border-radius:6px;text-align:center;font-size:0.8rem;">'
                + '<strong style="color:' + summaryColor + ';">' + escapeHtml(summaryText) + '</strong></div>';
        }
        el.innerHTML = html;
    }

    async function renderWeatherHistory() {
        var el = document.getElementById('w-history');
        if (!el) return;
        try {
            var r = await fetch('/api/weather/decisions?days=7&limit=10');
            var j = await r.json();
            var items = (j && j.decisions) ? j.decisions : [];
            if (!items.length) {
                el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Нет данных</div>';
                return;
            }
            var html = '';
            for (var i = 0; i < items.length; i++) {
                var it = items[i];
                var date = (it.date || '').slice(5).replace('-', '.');
                var badgeCls = 'weather-badge-ok';
                var badgeText = 'ПОЛИВ';
                if (it.decision === 'skip' || it.decision === 'stop') {
                    badgeCls = 'weather-badge-skip';
                    badgeText = 'Отложен';
                } else if (it.decision === 'adjust' || (it.coefficient && it.coefficient < 100)) {
                    badgeCls = 'weather-badge-adj';
                    badgeText = it.coefficient + '%';
                } else if (it.decision === 'postpone') {
                    badgeCls = 'weather-badge-adj';
                    badgeText = 'ОТЛОЖЕН';
                }
                html += '<div class="weather-hist-item">'
                    + '<span class="weather-hist-date">' + escapeHtml(date) + '</span>'
                    + '<span class="weather-badge ' + badgeCls + '">' + escapeHtml(badgeText) + '</span>'
                    + '<span>' + escapeHtml(localizeWeatherReason(it.reason || '')) + '</span>'
                    + '</div>';
            }
            el.innerHTML = html;
        } catch (e) {
            el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Ошибка загрузки</div>';
        }
    }

    async function refreshWeatherWidget() {
        try {
            var r = await fetch('/api/weather');
            var j = await r.json();
            var widget = document.getElementById('weather-widget');
            if (!widget) return;
            if (!j || !j.available) { widget.style.display = 'none'; return; }
            widget.style.display = '';
            renderWeatherSummary(j);
            renderForecast24h(j.forecast_24h || []);
            renderForecast3d(j.forecast_3d || []);
            renderWeatherDetails(j);
            renderWeatherFactors(j.adjustment || {});
            renderWeatherHistory();
        } catch (e) { /* ignore weather fetch errors */ }
    }
    // Initial load + periodic refresh (every 5 min)
    refreshWeatherWidget();
    setInterval(refreshWeatherWidget, 5 * 60 * 1000);

    // --- Active Zone Indicator ---
    function updateActiveZoneIndicator(zones) {
        var el = document.getElementById('sidebar-active-zone');
        if (!el) return;
        var active = null;
        for (var i = 0; i < zones.length; i++) {
            if (zones[i].state === 'on') {
                active = zones[i];
                break;
            }
        }
        if (!active) {
            el.style.display = 'none';
            return;
        }
        el.style.display = '';
        var nameEl = document.getElementById('active-zone-name');
        var timerEl = document.getElementById('active-zone-timer');
        var progressEl = document.getElementById('active-zone-progress');
        var nextEl = document.getElementById('active-zone-next');
        if (nameEl) nameEl.textContent = active.name;
        // Timer
        if (active.planned_end_time && timerEl) {
            var end = new Date(active.planned_end_time);
            var now = new Date();
            var remain = Math.max(0, Math.floor((end - now) / 1000));
            var mins = Math.floor(remain / 60);
            var secs = remain % 60;
            timerEl.innerHTML = 'осталось <strong>' + mins + ':' + (secs < 10 ? '0' : '') + secs + '</strong>';
            // Progress
            if (active.watering_start_time && progressEl) {
                var start = new Date(active.watering_start_time);
                var total = (end - start) / 1000;
                var elapsed = (now - start) / 1000;
                var pct = Math.min(100, Math.max(0, (elapsed / total) * 100));
                progressEl.style.width = pct + '%';
            }
        }
        // Next zone
        if (nextEl) {
            var next = null;
            for (var j = 0; j < zones.length; j++) {
                if (zones[j].scheduled_start_time && zones[j].state !== 'on') {
                    if (!next || zones[j].scheduled_start_time < next.scheduled_start_time) {
                        next = zones[j];
                    }
                }
            }
            nextEl.textContent = next ? ('Следующая: ' + next.name + ' → ' + next.scheduled_start_time.split(' ')[1].slice(0,5)) : '';
        }
    }

    // --- Water Meter ---
    function getAuthoritativeWaterToday(data) {
        const water = data && data.water_today;
        if (!water || typeof water !== 'object') {
            return { available: false, liters: null, date: null, source: null };
        }
        const source = typeof water.source === 'string' ? water.source : null;
        if (source === 'unavailable' || water.error_code) {
            return { available: false, liters: null, date: null, source: source };
        }
        const noRecordedUsage = water.has_data === false || source === 'none';
        const liters = noRecordedUsage ? 0 : Number(water.liters);
        if (!Number.isFinite(liters) || liters < 0) {
            return { available: false, liters: null, date: null, source: source };
        }
        return {
            available: true,
            liters: liters,
            date: typeof water.date === 'string' ? water.date : null,
            source: source,
        };
    }

    function updateWaterMeter() {
        var el = document.getElementById('sidebar-water-meter');
        if (!el) return;
        var usage = getAuthoritativeWaterToday(statusData);
        var groups = statusData && Array.isArray(statusData.groups) ? statusData.groups : [];
        var hasFlowMeter = groups.some(function(group) {
            return group.use_water_meter === true || group.use_water_meter === 1;
        });
        if (!hasFlowMeter) {
            el.style.display = 'none';
            return;
        }
        el.style.display = '';
        var valEl = document.getElementById('water-meter-value');
        var detEl = document.getElementById('water-meter-detail');
        if (!usage.available) {
            if (valEl) valEl.innerHTML = '— <span class="unit">л</span>';
            if (detEl) detEl.textContent = 'Данные за сутки недоступны';
            return;
        }
        if (valEl) valEl.innerHTML = Math.round(usage.liters).toLocaleString() + ' <span class="unit">л</span>';
        if (detEl) {
            detEl.textContent = usage.date
                ? ('За ' + usage.date + (usage.source === 'none' ? ': расход не зафиксирован' : ''))
                : 'За текущие сутки контроллера';
        }
    }

    // ===== ZONES V2: Hunter-style rendering =====
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
        var zones = (zonesData || []).slice();
        if (currentGroupFilter !== null) {
            zones = zones.filter(function(z) { return z.group_id === currentGroupFilter; });
        } else {
            zones = zones.filter(function(z) { return z.group_id !== 999; });
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

        groups.forEach(function(g) {
            var gZones = (g.id === 999)
                ? (zonesData || []).filter(function(z) { return z.group_id === 999; })
                : allZones.filter(function(z) { return z.group_id === g.id; });
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
        // Preserve open accordion state across re-renders
        var openIds = {};
        c.querySelectorAll('.zone-card.open').forEach(function(el) {
            var zid = el.getAttribute('data-zone-id');
            if (zid) openIds[zid] = true;
        });
        var zones = getFilteredZonesV2();
        var groups = zoneGroupsCache || [];
        var groupNameById = {};
        groups.forEach(function(g) { groupNameById[g.id] = g.name; });

        if (!zones.length) {
            c.innerHTML = '<div style="text-align:center;padding:30px;color:#999;font-size:14px">🔍 Зоны не найдены</div>';
            updateZoneStats(zones);
            return;
        }

        var html = '';
        var lastGroupId = null;
        var showSections = currentGroupFilter === null && !zoneSearchQuery;

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
                    var _endMs = new Date(z.planned_end_time).getTime();
                    var _startMs = new Date(z.watering_start_time).getTime();
                    var _remain = Math.max(0, Math.floor((_endMs - Date.now()) / 1000));
                    var _total = Math.max(60, Math.floor((_endMs - _startMs) / 1000));
                    _timerText = formatSeconds(_remain);
                    var _pct = Math.min(100, Math.max(0, ((_total - _remain) / _total) * 100));
                    _pctText = Math.round(_pct) + '%';
                    _progWidth = _pct + '%';
                }
                runningHtml = '<div class="zc-running"><span class="zc-running-dot"></span><span>Осталось</span><span class="zc-running-timer" id="ztimer-' + z.id + '" data-remaining-seconds="' + (_remain || '') + '">' + _timerText + '</span><span class="zc-running-pct" id="zpct-' + z.id + '">' + _pctText + '</span></div>';
                runningHtml += '<div class="zc-progress"><div class="zc-progress-bar" id="zprog-' + z.id + '" style="width:' + _progWidth + '"></div></div>';
            }

            var emergency = !!(statusData && statusData.emergency_stop);
            var startAction = emergency ? "showNotification('Аварийная остановка активна','warning')" : "toggleZoneRun(" + z.id + ")";

            html += '<div class="zone-card ' + statusCls + '" id="zcard-' + z.id + '" data-zone-id="' + z.id + '">';
            html += '<div class="zone-card-main" onclick="toggleZoneCard(' + z.id + ')">';
            // Photo thumbnail if exists, otherwise icon (issue #6 + #11)
            if (z.photo_path) {
                var _ts = z._photoTs || '';
                // Issue #11: list shows the small thumb (?variant=thumb), lightbox opens the full main file.
                var _thumbUrl = '/api/zones/' + z.id + '/photo?variant=thumb' + (_ts ? '&ts=' + _ts : '');
                var _fullUrl = '/api/zones/' + z.id + '/photo' + (_ts ? '?ts=' + _ts : '');
                html += '<div class="zc-photo" onclick="event.stopPropagation();showPhotoModal(\'' + _fullUrl + '\')" title="Открыть фото">';
                // alt is escaped (XSS); src is server-controlled URL (no user input).
                // onerror falls back to hiding the img (parent gets default grey background).
                html += '<img src="' + _thumbUrl + '" alt="Фото зоны ' + escapeHtml(z.name || '') + '" onerror="this.style.display=\'none\'">';
                html += '</div>';
            } else {
                html += '<div class="zc-icon" style="background:' + t.bg + '">' + escapeHtml(z.icon || '🌿') + '</div>';
            }
            html += '<div class="zc-info"><div class="zc-name">#' + z.id + ' ' + escapeHtml(z.name || '') + '</div>';
            html += '<div class="zc-meta"><span>' + escapeHtml(t.label) + '</span><span style="color:#ddd">·</span><span class="zc-dur-badge" id="zbadge-' + z.id + '">' + z.duration + ' мин</span>';
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
            if (isAdmin) {
                html += '<button class="zc-btn-edit zc-btn-half" onclick="event.stopPropagation();openZoneSheet(' + z.id + ')">✏️ Редактировать</button>';
            }
            html += '<button class="zc-btn-history" onclick="event.stopPropagation();window.historyModal&&window.historyModal.openForZone(' + z.id + ')" data-audit-action="zone_history_open_click" data-audit-target="zone_' + z.id + '">📊 История</button>';
            html += '</div>';
            html += '<div class="zc-actions zc-actions--primary">';
            if (isRunning) {
                html += '<button class="zc-btn-stop" onclick="event.stopPropagation();' + startAction + '">⏹ Стоп</button>';
            } else {
                html += '<button class="zc-btn-run" onclick="event.stopPropagation();showRunPopup(' + z.id + ',' + z.duration + ')">▶ Запустить</button>';
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
        // Issue #15 — restore .selected class on re-render in select mode.
        if (groupSelectMode && groupSelectMode.selected.size > 0) {
            groupSelectMode.selected.forEach(function(zid) {
                var card = document.querySelector('[data-zone-id="' + zid + '"]');
                if (card) card.classList.add('selected');
            });
        }
        updateZoneStats(zones);

        // Init running timers
        zones.forEach(function(z) {
            if (z.state === 'on') initZoneTimer(z);
        });
    }

    function updateZoneStats(zones) {
        var all = (zonesData || []).filter(function(z) { return z.group_id !== 999; });
        var running = all.filter(function(z) { return z.state === 'on'; }).length;
        var groups = (zoneGroupsCache || []).filter(function(g) { return g.id !== 999; });
        var waterToday = getAuthoritativeWaterToday(statusData);
        var _flag = function(v){ try { if (v===true||v===1) return true; var s=String(v).trim().toLowerCase(); return s==='1'||s==='true'||s==='on'||s==='yes'; } catch(e){ return false; } };
        var hasFlowMeter = groups.some(function(g){ return _flag(g.use_water_meter); });

        var el;
        el = document.getElementById('statZonesTotal'); if (el) el.textContent = all.length;
        el = document.getElementById('statZonesActive'); if (el) el.textContent = running;
        el = document.getElementById('statZonesGroups'); if (el) el.textContent = groups.length;
        el = document.getElementById('statZonesWater');
        if (el) {
            el.textContent = waterToday.available ? Math.round(waterToday.liters) : '—';
            // Hide the whole tile (.zstat-item) when no flow meter is configured (issue #3).
            // flex:1 on remaining tiles makes them fill the bar automatically.
            var tile = el.closest('.zstat-item');
            if (tile) tile.style.display = hasFlowMeter ? '' : 'none';
        }
        // Also update old zones-count for backward compat
        el = document.getElementById('zones-count'); if (el) el.textContent = all.length;
    }

    function initZoneTimer(zone) {
        function applyTimer(remain) {
            var total;
            if (zone.planned_end_time && zone.watering_start_time) {
                var endMs = new Date(zone.planned_end_time).getTime();
                var startMs = new Date(zone.watering_start_time).getTime();
                total = Math.max(60, Math.floor((endMs - startMs) / 1000));
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
                var endMs = new Date(zone.planned_end_time).getTime();
                var remain = Math.max(0, Math.floor((endMs - Date.now()) / 1000));
                if (remain > 0) { applyTimer(remain); return; }
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

    // Accordion toggle — only one zone card may be open at a time (issue #5).
    // Scoped to #zoneList so cards in other lists are unaffected.
    function toggleZoneCard(id) {
        // Issue #15 — in select mode, tap on card body toggles selection,
        // not accordion expansion.
        if (groupSelectMode && toggleZoneSelected(id)) return;
        var card = document.getElementById('zcard-' + id);
        if (!card) return;
        var willOpen = !card.classList.contains('open');
        if (willOpen) {
            var list = document.getElementById('zoneList');
            if (list) {
                list.querySelectorAll('.zone-card.open').forEach(function(other) {
                    if (other !== card) other.classList.remove('open');
                });
            }
        }
        card.classList.toggle('open');
    }
    // Make accessible globally
    window.toggleZoneCard = toggleZoneCard;

    // Group selection
    function selectZoneGroup(groupId) {
        currentGroupFilter = groupId;
        renderGroupTabs();
        renderZoneCards();
    }
    window.selectZoneGroup = selectZoneGroup;

    // Search
    function toggleZoneSearch() {
        var wrap = document.getElementById('zoneSearchWrap');
        if (!wrap) return;
        var visible = wrap.style.display !== 'none';
        wrap.style.display = visible ? 'none' : 'block';
        if (!visible) document.getElementById('searchInput').focus();
        else { zoneSearchQuery = ''; document.getElementById('searchInput').value = ''; renderZoneCards(); }
    }
    window.toggleZoneSearch = toggleZoneSearch;

    function filterZonesBySearch() {
        zoneSearchQuery = (document.getElementById('searchInput') || {}).value || '';
        renderZoneCards();
    }
    window.filterZonesBySearch = filterZonesBySearch;

    // Run/stop zone
    function toggleZoneRun(id) {
        showLoading(((zonesData||[]).find(function(z){return z.id===id;})||{}).state==='on' ? 'Остановка...' : 'Запуск...');
        var z = (zonesData || []).find(function(z) { return z.id === id; });
        if (!z) { hideLoading(); return; }
        var wantOn = z.state !== 'on';
        var url = wantOn ? '/api/zones/' + id + '/mqtt/start' : '/api/zones/' + id + '/mqtt/stop';
        // Optimistic: set state + times BEFORE fetch for instant timer
        var optimisticStart = wantOn ? optimisticTimestamp() : null;
        var optimisticEnd = wantOn ? optimisticTimestamp(Date.now() + (z.duration||10) * 60 * 1000) : null;
        var mutationToken = rememberOptimisticZoneState(
            z, wantOn ? 'on' : 'off', optimisticStart, optimisticEnd
        );
        renderZoneCards();
        renderGroupTabs();
        fetch(url, { method: 'POST' }).then(function(r) { return r.json(); }).then(function(data) {
            if (data && data.success) {
                hideLoading();
                showZoneToast(wantOn ? '▶ Зона #' + id + ' запущена' : '⏹ Зона #' + id + ' остановлена', wantOn ? 'success' : '');
                reconcileOptimisticZoneState(id, mutationToken);
                // Light refresh status (groups) after 2 sec
                setTimeout(function() { loadStatusData(); }, 2000);
            } else {
                hideLoading();
                revertOptimisticZoneState(id, mutationToken);
                renderZoneCards();
                showZoneToast((data && data.message) || 'Ошибка', 'error');
            }
        }).catch(function() {
            hideLoading();
            revertOptimisticZoneState(id, mutationToken);
            renderZoneCards();
            showZoneToast('Ошибка сети', 'error');
        });
    }
    window.toggleZoneRun = toggleZoneRun;

    // Duration +/-
    var durDebounceTimers = {};
    var durWriteInFlight = {};
    var durWritePending = {};

    function cancelQueuedZoneDurationSave(id) {
        clearTimeout(durDebounceTimers[id]);
        durDebounceTimers[id] = null;
        durWritePending[id] = false;
    }

    function discardPendingZoneDuration(id) {
        delete pendingZoneDurations[id];
        zonesDataRevision += 1;
        cancelQueuedZoneDurationSave(id);
    }

    async function saveZoneDuration(id) {
        var z = (zonesData || []).find(function(zone) { return zone.id === id; });
        if (!z) return false;
        var pending = pendingZoneDurations[id];
        if (!pending) return true;
        if (!Number.isInteger(pending.expectedVersion) || pending.expectedVersion < 0) {
            discardPendingZoneDuration(id);
            await recoverFromZoneCasConflict({ error_code: 'EXPECTED_VERSION_REQUIRED' }, false);
            return false;
        }

        var requestedDuration = pending.duration;
        var durationRevision = pending.revision;
        var expectedVersion = pending.expectedVersion;
        try {
            var data = await api.put('/api/zones/' + id, {
                duration: requestedDuration,
                expected_version: expectedVersion,
            });
            if (isZoneCasConflict(data)) {
                discardPendingZoneDuration(id);
                await recoverFromZoneCasConflict(data, false);
                return false;
            }
            if (!data || typeof data !== 'object' || data.success === false) {
                showZoneToast((data && data.message) || 'Ошибка сохранения длительности', 'error');
                discardPendingZoneDuration(id);
                await loadZonesData();
                return false;
            }
            if (!Number.isInteger(data.version)) {
                showZoneToast('Сервер не вернул новую версию зоны. Загружены актуальные данные.', 'error');
                discardPendingZoneDuration(id);
                await loadZonesData();
                return false;
            }
            var current = (zonesData || []).find(function(zone) { return zone.id === id; });
            if (current && current.version === expectedVersion) current.version = data.version;
            if (pendingZoneDurations[id] && pendingZoneDurations[id].revision === durationRevision) {
                delete pendingZoneDurations[id];
            } else if (pendingZoneDurations[id]) {
                // A newer local click is causally based on this successful
                // write, so only that local successor may advance its token.
                pendingZoneDurations[id].expectedVersion = data.version;
            }
            zonesDataRevision += 1;
            return true;
        } catch (error) {
            showZoneToast('Ошибка сохранения длительности', 'error');
            discardPendingZoneDuration(id);
            await loadZonesData();
            return false;
        }
    }

    function queueZoneDurationSave(id) {
        if (durWriteInFlight[id]) {
            durWritePending[id] = true;
            return;
        }
        durWriteInFlight[id] = true;
        saveZoneDuration(id).then(function(saved) {
            if (!saved) durWritePending[id] = false;
        }).catch(function() {
            durWritePending[id] = false;
        }).finally(function() {
            durWriteInFlight[id] = false;
            if (durWritePending[id]) {
                durWritePending[id] = false;
                queueZoneDurationSave(id);
            }
        });
    }

    function changeZoneDur(id, delta) {
        var z = (zonesData || []).find(function(z) { return z.id === id; });
        if (!z) return;
        var pending = pendingZoneDurations[id];
        var currentDuration = pending ? pending.duration : (z.duration || 10);
        var nextDuration = Math.max(1, Math.min(120, currentDuration + delta));
        var durationRevision = ++zonesDataRevision;
        pendingZoneDurations[id] = {
            duration: nextDuration,
            revision: durationRevision,
            expectedVersion: pending ? pending.expectedVersion : z.version,
        };
        z.duration = nextDuration;
        var el = document.getElementById('zdur-' + id);
        if (el) el.textContent = z.duration;
        var badge = document.getElementById('zbadge-' + id);
        if (badge) badge.textContent = z.duration + ' мин';
        // Debounce API call
        clearTimeout(durDebounceTimers[id]);
        durDebounceTimers[id] = setTimeout(function() {
            queueZoneDurationSave(id);
        }, 500);
    }
    window.changeZoneDur = changeZoneDur;

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
        _runPopupSelectedZones = null;
        var title = gid ? '▶ ' + gName : '▶ Все группы';
        document.getElementById('runPopupTitle').textContent = title;
        runPopupDur = 15;
        // Issue #12: reset mode on each open.
        runPopupMode = 'min';
        runPopupPct = null;
        // Show "with defaults" button for group
        var defBtn = document.getElementById('runPopupDefaults');
        if (defBtn) defBtn.style.display = 'block';
        initDialTicks();
        updateDial();
        _refreshRunPopupModeUI();
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
                setTimeout(function() { Promise.all([loadStatusData(), loadZonesData()]); }, 1500);
            }).catch(function() { showZoneToast('Ошибка', 'error'); });
        } else {
            showLoading('Запуск всех групп...');
            runAllGroups(null).then(function(results) {
                reportGroupRunResults(results, 'с настройками зон');
            }).catch(function() {
                hideLoading();
                showZoneToast('Ошибка сети', 'error');
            });
        }
    }
    window.runGroupWithDefaults = runGroupWithDefaults;
    window.showGroupRunPopup = showGroupRunPopup;
    window.runSelectedGroup = runSelectedGroup;

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
        // Populate photo section (issue #6)
        refreshSheetPhotoPreview(z);
        document.getElementById('sheetOverlay').classList.add('show');
        document.getElementById('bottomSheet').classList.add('show');
    }
    window.openZoneSheet = openZoneSheet;

    // Refresh the photo preview area inside the bottom sheet for a given zone
    function refreshSheetPhotoPreview(z) {
        var preview = document.getElementById('sheetPhotoPreview');
        var rotateBtn = document.getElementById('sheetPhotoRotateBtn');
        var deleteBtn = document.getElementById('sheetPhotoDeleteBtn');
        var uploadBtn = document.getElementById('sheetPhotoUploadBtn');
        if (!preview) return;
        if (z && z.photo_path) {
            var ts = z._photoTs || '';
            var url = '/api/zones/' + z.id + '/photo' + (ts ? '?ts=' + ts : '');
            // Build via DOM (no string interpolation of arbitrary attributes — XSS-safe).
            preview.innerHTML = '';
            var img = document.createElement('img');
            img.src = url;
            // alt is a DOM property (no HTML parsing), but escape defensively to satisfy XSS guards.
            img.alt = 'Фото зоны ' + escapeHtml(z.name || '');
            img.onclick = function() { showPhotoModal(url); };
            preview.appendChild(img);
            preview.style.cursor = 'pointer';
            preview.onclick = function() { showPhotoModal(url); };
            if (rotateBtn) rotateBtn.style.display = '';
            if (deleteBtn) deleteBtn.style.display = '';
            if (uploadBtn) uploadBtn.textContent = '📷 Заменить';
        } else {
            preview.innerHTML = '<span class="sheet-photo-placeholder">Нет фото</span>';
            preview.onclick = null;
            preview.style.cursor = 'default';
            if (rotateBtn) rotateBtn.style.display = 'none';
            if (deleteBtn) deleteBtn.style.display = 'none';
            if (uploadBtn) uploadBtn.textContent = '📷 Загрузить';
        }
    }

    function closeZoneSheet() {
        document.getElementById('sheetOverlay').classList.remove('show');
        document.getElementById('bottomSheet').classList.remove('show');
        editingZoneId = null;
    }
    window.closeZoneSheet = closeZoneSheet;

    function saveZoneEdit() {
        if (!editingZoneId) return;
        var zoneId = editingZoneId;
        var zone = (zonesData || []).find(function(item) { return item.id === zoneId; });
        if (!zone || !Number.isInteger(zone.version) || zone.version < 0) {
            recoverFromZoneCasConflict({ error_code: 'EXPECTED_VERSION_REQUIRED' }, true);
            return;
        }
        var changes = {
            name: document.getElementById('editZoneName').value,
            duration: parseInt(document.getElementById('editZoneDuration').value) || 10,
            icon: document.getElementById('editZoneIcon').value,
            group_id: parseInt(document.getElementById('editZoneGroup').value) || 1,
        };
        var expectedVersion = zone.version;
        var payload = Object.assign({}, changes, { expected_version: expectedVersion });
        api.put('/api/zones/' + zoneId, payload).then(function(data) {
            if (isZoneCasConflict(data)) {
                recoverFromZoneCasConflict(data, true);
                return;
            }
            if (!data || typeof data !== 'object' || data.success === false) {
                showZoneToast((data && data.message) || 'Ошибка сохранения', 'error');
                return;
            }
            if (!Number.isInteger(data.version)) {
                closeZoneSheet();
                showZoneToast('Сервер не вернул новую версию зоны. Загружены актуальные данные.', 'error');
                loadZonesData();
                return;
            }
            var current = (zonesData || []).find(function(item) { return item.id === zoneId; });
            if (current && current.version === expectedVersion) {
                Object.assign(current, changes, { version: data.version });
            }
            closeZoneSheet();
            showZoneToast('✅ Зона сохранена', 'success');
            loadZonesData();
        }).catch(function() { showZoneToast('Ошибка сохранения', 'error'); });
    }
    window.saveZoneEdit = saveZoneEdit;

    // Issue #15 — "Запустить выбранные" mode state.
    // null = off; { selected: Set<int> } when on. Selection spans groups —
    // confirmRun groups by zone.group_id and fires one parallel request per gid.
    var groupSelectMode = null;
    var _runPopupSelectedZones = null;  // populated when popup confirms run-selected
    function enterRunSelectedMode() {
        groupSelectMode = { selected: new Set() };
        document.body.classList.add('mode-select-zones');
        _updateSelectedCounter();
    }
    function exitRunSelectedMode() {
        if (!groupSelectMode) return;
        // Clear visual selected class on cards before leaving the mode.
        var cards = document.querySelectorAll('.zone-card.selected');
        for (var i = 0; i < cards.length; i++) cards[i].classList.remove('selected');
        groupSelectMode = null;
        document.body.classList.remove('mode-select-zones');
        _updateSelectedCounter();
    }
    function toggleZoneSelected(zoneId) {
        if (!groupSelectMode) return false;
        var z = (zonesData || []).find(function(zz) { return zz.id === zoneId; });
        if (!z || !z.group_id || z.group_id === 999) return false;
        if (groupSelectMode.selected.has(zoneId)) {
            groupSelectMode.selected.delete(zoneId);
        } else {
            groupSelectMode.selected.add(zoneId);
        }
        var card = document.querySelector('[data-zone-id="' + zoneId + '"]');
        if (card) card.classList.toggle('selected', groupSelectMode.selected.has(zoneId));
        _updateSelectedCounter();
        return true;
    }
    function _updateSelectedCounter() {
        var n = groupSelectMode ? groupSelectMode.selected.size : 0;
        var btn = document.getElementById('zoneSelectNextBtn');
        if (btn) {
            btn.textContent = 'Далее (' + n + ')';
            btn.disabled = n === 0;
        }
    }
    function confirmRunSelectedNext() {
        if (!groupSelectMode || groupSelectMode.selected.size === 0) return;
        var selectedZones = Array.from(groupSelectMode.selected);
        // Open the existing run popup, but mark it as "selected-zones" via _runPopupSelectedZones.
        // confirmRun groups by zone.group_id and fires one parallel request per gid.
        runPopupGroupId = null;
        runPopupZoneId = null;
        _runPopupAllGroups = false;
        _runPopupSelectedZones = selectedZones;
        runPopupDur = 15;
        runPopupMode = 'min';
        runPopupPct = null;
        document.getElementById('runPopupTitle').textContent =
            '▶ Выбранные зоны (' + selectedZones.length + ')';
        // Hide "📋 С настройками зон" — defaults path is group-wide, ambiguous for subset.
        var defBtn = document.getElementById('runPopupDefaults');
        if (defBtn) defBtn.style.display = 'none';
        if (typeof initDialTicks === 'function') initDialTicks();
        if (typeof updateDial === 'function') updateDial();
        _refreshRunPopupModeUI();
        document.getElementById('runPopupOverlay').classList.add('show');
        document.getElementById('runPopup').classList.add('show');
        if (typeof initDialDrag === 'function') setTimeout(initDialDrag, 100);
    }
    window.enterRunSelectedMode = enterRunSelectedMode;
    window.exitRunSelectedMode = exitRunSelectedMode;
    window.toggleZoneSelected = toggleZoneSelected;
    window.confirmRunSelectedNext = confirmRunSelectedNext;

    // Run Duration Popup with Circular Dial
    var runPopupZoneId = null;
    var runPopupGroupId = null;
    var _runPopupAllGroups = false;
    var runPopupDur = 10;
    // Issue #12: percent-of-norm mode. null/min = legacy minutes, 'pct' with
    // a runPopupPct value = % mode. confirmRun() picks request body shape.
    var runPopupMode = 'min';
    var runPopupPct = null;
    var MAX_DUR = 120;
    var DIAL_R = 85;
    var DIAL_CIRC = 2 * Math.PI * DIAL_R;

    function _refreshRunPopupModeUI() {
        var pop = document.getElementById('runPopup');
        if (!pop) return;
        if (runPopupMode === 'pct') pop.classList.add('mode-pct');
        else pop.classList.remove('mode-pct');
        // Active state on the matching pct button (or none in min mode).
        var btns = document.querySelectorAll('#runPopupPctPresets button');
        for (var i = 0; i < btns.length; i++) {
            var p = parseInt(btns[i].getAttribute('data-pct'), 10);
            if (runPopupMode === 'pct' && p === runPopupPct) btns[i].classList.add('active');
            else btns[i].classList.remove('active');
        }
    }

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
        if (svg.__dialDragInitialized) return;
        svg.__dialDragInitialized = true;
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
            // Issue #12: dragging the dial = explicit minutes mode.
            runPopupMode = 'min';
            runPopupPct = null;
            _refreshRunPopupModeUI();
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
        _runPopupSelectedZones = null;
        runPopupDur = defaultDur || 10;
        // Issue #12: reset mode each time popup opens — pct state must not
        // persist across separate runs.
        runPopupMode = 'min';
        runPopupPct = null;
        var z = (zonesData || []).find(function(z){ return z.id === zoneId; });
        var title = z ? '▶ #' + z.id + ' ' + z.name : '▶ Запустить';
        document.getElementById('runPopupTitle').textContent = title;
        // Hide "with defaults" button for single zone
        var defBtn = document.getElementById('runPopupDefaults');
        if (defBtn) defBtn.style.display = 'none';
        initDialTicks();
        updateDial();
        _refreshRunPopupModeUI();
        document.getElementById('runPopupOverlay').classList.add('show');
        document.getElementById('runPopup').classList.add('show');
        setTimeout(initDialDrag, 100);
    }
    function closeRunPopup() {
        document.getElementById('runPopupOverlay').classList.remove('show');
        document.getElementById('runPopup').classList.remove('show');
        runPopupZoneId = null;
        _runPopupAllGroups = false;
        _runPopupSelectedZones = null;
    }
    function setRunDur(val) {
        runPopupDur = val;
        // Issue #12: minute preset click = explicit minutes mode.
        runPopupMode = 'min';
        runPopupPct = null;
        _refreshRunPopupModeUI();
        updateDial();
    }
    function setRunPct(p) {
        // Issue #12: pick percent mode; backend unfolds per-zone server-side.
        runPopupMode = 'pct';
        runPopupPct = p;
        _refreshRunPopupModeUI();
    }

    function runAllGroups(groupBody) {
        var allGroups = (zoneGroupsCache || []).filter(function(g) { return g.id !== 999; });
        return Promise.all(allGroups.map(function(group) {
            var options = { method: 'POST' };
            if (groupBody) {
                options.headers = {'Content-Type': 'application/json'};
                options.body = JSON.stringify(groupBody);
            }
            return fetch('/api/groups/' + group.id + '/start-from-first', options)
                .then(function(response) { return response.json(); })
                .then(function(data) {
                    return {
                        id: group.id,
                        ok: !!(data && data.success),
                        message: data && data.message,
                    };
                })
                .catch(function() {
                    return { id: group.id, ok: false, message: 'Ошибка сети' };
                });
        }));
    }

    function reportGroupRunResults(results, successLabel) {
        hideLoading();
        var okCount = results.filter(function(result) { return result.ok; }).length;
        if (results.length > 0 && okCount === results.length) {
            showZoneToast('▶ Все группы запущены' + (successLabel ? ': ' + successLabel : ''), 'success');
        } else if (okCount > 0) {
            showZoneToast('▶ Запущены ' + okCount + ' из ' + results.length + ' групп', 'warning');
        } else {
            var firstMessage = results[0] && results[0].message;
            showZoneToast(firstMessage || 'Не удалось запустить группы', 'error');
        }
        setTimeout(function() { Promise.all([loadStatusData(), loadZonesData()]); }, 1500);
    }

    function confirmRun() {
        // _runPopupAllGroups flag: true when "all groups" was selected (gid=null)
        if (!runPopupZoneId && !runPopupGroupId && !_runPopupAllGroups && !_runPopupSelectedZones) return;
        var dur = runPopupDur;
        // Issue #12: capture mode at confirm time (popup is closed below).
        var modePct = (runPopupMode === 'pct' && runPopupPct);
        var pct = runPopupPct;
        var savedZoneId = runPopupZoneId;
        var savedGroupId = runPopupGroupId;
        var savedAllGroups = _runPopupAllGroups;
        var savedSelectedZones = _runPopupSelectedZones;
        closeRunPopup();

        // Pick request-body shape once. Group + single-zone use different
        // field names for minutes mode (override_duration vs duration), but
        // share `duration_percent` for percent mode.
        var groupBody = modePct ? {duration_percent: pct} : {override_duration: dur};
        var zoneBody = modePct ? {duration_percent: pct} : {duration: dur};
        var label = modePct ? (pct + '%') : (dur + ' мин');

        // Issue #15 — ad-hoc selected zones path. Selection may span groups;
        // group by zone.group_id and fire one /run-selected per gid in parallel.
        // Each group runs its own sequential queue server-side.
        if (savedSelectedZones && savedSelectedZones.length > 0) {
            var n = savedSelectedZones.length;
            var byGroup = {};
            savedSelectedZones.forEach(function(zid) {
                var z = (zonesData || []).find(function(zz) { return zz.id === zid; });
                if (!z || !z.group_id || z.group_id === 999) return;
                if (!byGroup[z.group_id]) byGroup[z.group_id] = [];
                byGroup[z.group_id].push(zid);
            });
            var gids = Object.keys(byGroup);
            if (gids.length === 0) {
                showZoneToast('Нет валидных зон для запуска', 'error');
                return;
            }
            showLoading('Запуск ' + n + ' зон(ы) в ' + gids.length + ' групп(ах)...');
            Promise.all(gids.map(function(gid) {
                var zonesForGroup = byGroup[gid];
                var selBody = modePct
                    ? { zones: zonesForGroup, duration_percent: pct }
                    : { zones: zonesForGroup, duration: dur };
                return fetch('/api/groups/' + gid + '/run-selected', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(selBody)
                })
                .then(function(r) { return r.json(); })
                .then(function(data) { return { gid: gid, ok: !!(data && data.success), msg: data && data.message }; })
                .catch(function() { return { gid: gid, ok: false, msg: 'сеть' }; });
            })).then(function(results) {
                hideLoading();
                var okCount = results.filter(function(r) { return r.ok; }).length;
                if (okCount === results.length) {
                    showZoneToast('▶ Запущены ' + n + ' зон(ы) в ' + okCount + ' групп(ах) на ' + label, 'success');
                } else if (okCount > 0) {
                    showZoneToast('▶ Запущены ' + okCount + ' из ' + results.length + ' групп', 'warning');
                } else {
                    var firstMsg = results[0] && results[0].msg;
                    showZoneToast(firstMsg || 'Ошибка запуска', 'error');
                }
                if (typeof exitRunSelectedMode === 'function') exitRunSelectedMode();
                setTimeout(function() { Promise.all([loadStatusData(), loadZonesData()]); }, 1500);
            });
            return;
        }

        if (savedGroupId || savedAllGroups) {
            // Group run: pass override_duration to API (does NOT change base durations in DB)
            if (savedAllGroups && !savedGroupId) {
                // All groups: start each group with override
                showLoading('Запуск всех групп...');
                runAllGroups(groupBody).then(function(results) {
                    reportGroupRunResults(results, label);
                }).catch(function() {
                    hideLoading();
                    showZoneToast('Ошибка сети', 'error');
                });
                return;
            }
            var gid = savedGroupId;
            var groupZones = (zonesData || []).filter(function(z) { return z.group_id === gid && z.group_id !== 999; });
            // Optimistic: set local times for instant timer display.
            // For percent mode, use per-zone duration*pct (best-effort UX preview;
            // server is the authority and may emit warnings if base norm == 0).
            groupZones.forEach(function(z) {
                var pdur = dur;
                if (modePct) {
                    var base = parseInt(z.duration, 10) || 15;
                    pdur = Math.max(1, Math.min(240, Math.ceil(base * pct / 100)));
                }
                z.state = 'on';
                z.watering_start_time = optimisticTimestamp();
                z.planned_end_time = optimisticTimestamp(Date.now() + pdur * 60 * 1000);
            });
            renderZoneCards();
            renderGroupTabs();
            fetch('/api/groups/' + gid + '/start-from-first', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(groupBody)
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                showZoneToast(data && data.success ? '▶ Группа запущена' : 'Ошибка', data && data.success ? 'success' : 'error');
                // Issue #12 C1: surface server-side warnings on the group
                // path (mirrors single-zone handler below). norm_not_set =>
                // some zone in the group had duration<=0, server fell back
                // to 15 min. clipped_max => some zone × pct exceeded 240.
                if (data && data.success && data.warnings && data.warnings.length) {
                    var msgs = data.warnings.map(function(w) {
                        if (w === 'norm_not_set') return 'норма зоны не задана — использую 15 мин';
                        if (w === 'clipped_max') return 'обрезано до 240 мин';
                        if (w === 'clipped_min') return 'округлено до 1 мин';
                        return w;
                    });
                    setTimeout(function() { showZoneToast('⚠ ' + msgs.join('; '), 'error'); }, 600);
                }
                if (!data || !data.success) loadZonesData();
                setTimeout(function() { Promise.all([loadStatusData(), loadZonesData()]); }, 1500);
            }).catch(function() {
                hideLoading();
                showZoneToast('Ошибка сети', 'error');
                loadZonesData();
            });
            return;
        }

        // Single zone run — duration override (one-time, doesn't change base)
        var id = savedZoneId;
        var z = (zonesData || []).find(function(z){ return z.id === id; });
        var wasRunning = z && z.state === 'on';

        // If already running — stop first, then restart
        var zName = (z && z.name) ? ' ' + z.name : '';
        showLoading('Запуск зоны #' + id + zName + '...');
        var mutationToken = null;
        var startFn = function() {
            // Optimistic: set state + times BEFORE fetch for instant timer.
            // % mode: pre-compute the same way the server will (best-effort).
            var optDur = dur;
            if (modePct && z) {
                var base = parseInt(z.duration, 10) || 15;
                optDur = Math.max(1, Math.min(240, Math.ceil(base * pct / 100)));
            }
            if (z) {
                mutationToken = rememberOptimisticZoneState(
                    z,
                    'on',
                    optimisticTimestamp(),
                    optimisticTimestamp(Date.now() + optDur * 60 * 1000)
                );
            }
            renderZoneCards();
            renderGroupTabs();
            fetch('/api/zones/' + id + '/mqtt/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(zoneBody) })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data && data.success) {
                    hideLoading();
                    showZoneToast('▶ #' + id + zName + ' запущена: ' + label, 'success');
                    reconcileOptimisticZoneState(id, mutationToken);
                    // Issue #12: surface server-side warnings as toast text.
                    if (data.warnings && data.warnings.length) {
                        var msgs = data.warnings.map(function(w) {
                            if (w === 'norm_not_set') return 'норма зоны не задана — использую 15 мин';
                            if (w === 'clipped_max') return 'обрезано до 240 мин';
                            if (w === 'clipped_min') return 'округлено до 1 мин';
                            return w;
                        });
                        setTimeout(function() { showZoneToast('⚠ ' + msgs.join('; '), 'error'); }, 600);
                    }
                    // Refresh timer (server times may differ slightly)
                    initZoneTimer(z);
                    setTimeout(function() { loadStatusData(); }, 2000);
                } else {
                    revertOptimisticZoneState(id, mutationToken);
                    renderZoneCards();
                    hideLoading();
                    showZoneToast((data && data.message) || 'Ошибка', 'error');
                }
            }).catch(function() {
                revertOptimisticZoneState(id, mutationToken);
                renderZoneCards();
                hideLoading();
                showZoneToast('Ошибка сети', 'error');
            });
        };
        
        if (wasRunning) {
            fetch('/api/zones/' + id + '/mqtt/stop', { method: 'POST' })
            .then(function(response) { return response.json(); })
            .then(function(data) {
                if (!data || !data.success) throw new Error((data && data.message) || 'stop failed');
                if (z) {
                    z.state = 'off';
                    z.watering_start_time = null;
                    z.planned_end_time = null;
                }
            })
            .then(function() { return new Promise(function(r) { setTimeout(r, 500); }); })
            .then(startFn)
            .catch(function() {
                hideLoading();
                showZoneToast('Не удалось перезапустить зону', 'error');
                loadZonesData();
            });
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
                showZoneToast(
                    data && data.success ? '▶ Группа запущена с настройками зон' : ((data && data.message) || 'Ошибка'),
                    data && data.success ? 'success' : 'error'
                );
                setTimeout(function() { Promise.all([loadStatusData(), loadZonesData()]); }, 1500);
            }).catch(function() {
                hideLoading();
                showZoneToast('Ошибка сети', 'error');
            });
        } else {
            runAllGroups(null).then(function(results) {
                reportGroupRunResults(results, 'с настройками зон');
            }).catch(function() {
                hideLoading();
                showZoneToast('Ошибка сети', 'error');
            });
        }
    }
    window.confirmRunWithDefaults = confirmRunWithDefaults;
    window.showRunPopup = showRunPopup;
    window.closeRunPopup = closeRunPopup;
    window.setRunDur = setRunDur;
    window.setRunPct = setRunPct;
    window.confirmRun = confirmRun;

    // Loading overlay
    function showLoading(text) {
        var el = document.getElementById('loadingOverlay');
        var txt = document.getElementById('loadingText');
        if (txt) txt.textContent = text || 'Загрузка...';
        if (el) el.classList.add('show');
        clearTimeout(_loadingTimer);
        _loadingTimer = setTimeout(hideLoading, 15000); // safety: auto-hide after 15s
    }
    function hideLoading() {
        var el = document.getElementById('loadingOverlay');
        if (el) el.classList.remove('show');
    }
    var _loadingTimer = null;
    window.showLoading = showLoading;
    window.hideLoading = hideLoading;

    // Toast
    function showZoneToast(msg, type) {
        var t = document.getElementById('zoneToast');
        if (!t) return;
        t.textContent = msg;
        t.className = 'zone-toast show' + (type ? ' ' + type : '');
        clearTimeout(t._timer);
        t._timer = setTimeout(function() { t.className = 'zone-toast'; }, 2500);
    }

    // --- Sidebar Toggle ---
    (function() {
        var btn = document.getElementById('sidebar-toggle');
        if (!btn) return;
        var layout = document.querySelector('.desktop-layout');
        if (!layout) return;
        // Restore state
        if (localStorage.getItem('sidebar-collapsed') === 'true') {
            layout.classList.add('sidebar-collapsed');
        }
        btn.addEventListener('click', function() {
            layout.classList.toggle('sidebar-collapsed');
            localStorage.setItem('sidebar-collapsed', layout.classList.contains('sidebar-collapsed'));
        });
    })();
