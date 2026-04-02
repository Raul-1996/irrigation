/**
 * zones-sensors.js — Sensor toggles, settings modals, autosave, rain/env config
 * Depends on: zones-core.js, zones-groups.js
 */

// ===== Sensor toggle handlers =====
async function toggleGroupUseMaster(groupId, enabled) {
    try {
        const topicEl = document.getElementById(`mv-topic-${groupId}`);
        const modeEl = document.getElementById(`mv-mode-${groupId}`);
        const serverEl = document.getElementById(`mv-server-${groupId}`);
        const topic = (topicEl && topicEl.value ? topicEl.value.trim() : '');
        if (topicEl) topicEl.style.border = '';
        if (enabled) {
            const serverOk = !!(serverEl && String(serverEl.value||'').trim());
            const topicOk = !!topic;
            if (!serverOk || !topicOk) {
                if (topicEl && !topicOk) topicEl.style.border = '2px solid #f44336';
                const cb = document.querySelector(`[data-group-id="${groupId}"] .group-use-mv`);
                if (cb) cb.checked = false;
                showNotification('Укажите MQTT сервер и MQTT-топик для мастер-клапана', 'warning');
                return;
            }
        }
        const payload = { use_master_valve: !!enabled };
        if (topic) payload.master_mqtt_topic = topic;
        if (modeEl && modeEl.value) payload.master_mode = modeEl.value;
        if (serverEl && serverEl.value) payload.master_mqtt_server_id = parseInt(serverEl.value);
        const ok = await api.put(`/api/groups/${groupId}`, payload);
        if (!ok) {
            showNotification('Не удалось сохранить настройки мастер-клапана', 'error');
        } else {
            const gi = groupsData.findIndex(g=>g.id===groupId);
            if (gi>=0) {
                groupsData[gi].use_master_valve = !!enabled;
                if (topic) groupsData[gi].master_mqtt_topic = topic;
                if (modeEl && modeEl.value) groupsData[gi].master_mode = modeEl.value;
                if (serverEl && serverEl.value) groupsData[gi].master_mqtt_server_id = parseInt(serverEl.value);
            }
            showNotification('Настройки мастер-клапана сохранены', 'success');
        }
    } catch (e) {
        showNotification('Ошибка сохранения настроек мастер-клапана', 'error');
    }
}

async function toggleGroupUsePressure(groupId, enabled){
    try {
        if (enabled) {
            const serverEl = document.getElementById(`pressure-server-${groupId}`);
            const topicEl = document.getElementById(`pressure-topic-${groupId}`);
            const serverOk = !!(serverEl && String(serverEl.value||'').trim());
            const topic = (topicEl && topicEl.value ? topicEl.value.trim() : '');
            if (!serverOk || !topic) {
                if (topicEl && !topic) topicEl.style.border = '2px solid #f44336';
                const cb = document.querySelector(`[data-group-id="${groupId}"] .group-use-pressure`);
                if (cb) cb.checked = false;
                showNotification('Укажите MQTT сервер и MQTT-топик для датчика давления', 'warning');
                return;
            }
        }
        const ok = await api.put(`/api/groups/${groupId}`, { use_pressure_sensor: !!enabled });
        if (ok){
            const gi = groupsData.findIndex(g=>g.id===groupId);
            if (gi>=0) groupsData[gi].use_pressure_sensor = !!enabled;
            showNotification('Настройка датчика давления сохранена', 'success');
        }
    } catch(e){ showNotification('Ошибка сохранения настройки давления', 'error'); }
}

async function toggleGroupUseWater(groupId, enabled){
    try {
        if (enabled) {
            const serverEl = document.getElementById(`water-server-${groupId}`);
            const topicEl = document.getElementById(`water-topic-${groupId}`);
            const serverOk = !!(serverEl && String(serverEl.value||'').trim());
            const topic = (topicEl && topicEl.value ? topicEl.value.trim() : '');
            if (!serverOk || !topic) {
                if (topicEl && !topic) topicEl.style.border = '2px solid #f44336';
                const cb = document.querySelector(`[data-group-id="${groupId}"] .group-use-water`);
                if (cb) cb.checked = false;
                showNotification('Укажите MQTT сервер и MQTT-топик для счётчика воды', 'warning');
                return;
            }
        }
        const ok = await api.put(`/api/groups/${groupId}`, { use_water_meter: !!enabled });
        if (ok){
            const gi = groupsData.findIndex(g=>g.id===groupId);
            if (gi>=0) groupsData[gi].use_water_meter = !!enabled;
            showNotification('Настройка счётчика воды сохранена', 'success');
        }
    } catch(e){ showNotification('Ошибка сохранения настройки счётчика воды', 'error'); }
}

async function toggleGroupUseRain(groupId, enabled) {
    try {
        const ok = await api.put(`/api/groups/${groupId}`, { name: groupsData.find(g=>g.id===groupId).name, use_rain_sensor: !!enabled });
        if (ok) {
            const gi = groupsData.findIndex(g=>g.id===groupId);
            if (gi>=0) groupsData[gi].use_rain_sensor = !!enabled;
            showNotification('Настройка датчика дождя для группы сохранена', 'success');
        }
    } catch (e) {
        showNotification('Ошибка сохранения настройки группы', 'error');
    }
}

// ===== Settings modals open/close =====
function openMasterSettings(groupId){ openModalById(`modal-master-${groupId}`); }
function closeMasterSettings(groupId){ closeModalById(`modal-master-${groupId}`); }
function openPressureSettings(groupId){ openModalById(`modal-pressure-${groupId}`); }
function closePressureSettings(groupId){ closeModalById(`modal-pressure-${groupId}`); }
function openWaterSettings(groupId){
    openModalById(`modal-water-${groupId}`);
    try { waterStartLive(groupId); } catch(e){}
}
function closeWaterSettings(groupId){
    try { waterStopLive(groupId); } catch(e){}
    try { waterFlushSave(groupId); } catch(e){}
    closeModalById(`modal-water-${groupId}`);
}

// ===== Master valve topic/mode save =====
async function saveGroupMasterTopic(groupId) {
    try {
        const topicEl = document.getElementById(`mv-topic-${groupId}`);
        const modeEl = document.getElementById(`mv-mode-${groupId}`);
        const topic = (topicEl && topicEl.value ? topicEl.value.trim() : '');
        if (topicEl) topicEl.style.border = '';
        const payload = { master_mqtt_topic: topic };
        if (modeEl && modeEl.value) payload.master_mode = modeEl.value;
        const ok = await api.put(`/api/groups/${groupId}`, payload);
        if (!ok) {
            showNotification('Не удалось сохранить топик мастер-клапана', 'error');
        } else {
            const gi = groupsData.findIndex(g=>g.id===groupId);
            if (gi>=0) { groupsData[gi].master_mqtt_topic = topic; }
            showNotification('Топик мастер-клапана сохранён', 'success');
        }
    } catch (e) {
        showNotification('Ошибка сохранения топика мастер-клапана', 'error');
    }
}

async function saveGroupMasterMode(groupId) {
    try {
        const modeEl = document.getElementById(`mv-mode-${groupId}`);
        const topicEl = document.getElementById(`mv-topic-${groupId}`);
        const mode = (modeEl && modeEl.value) ? modeEl.value : 'NC';
        const topic = (topicEl && topicEl.value ? topicEl.value.trim() : '');
        if (topicEl) topicEl.style.border = '';
        const payload = { master_mode: mode };
        if (topic) payload.master_mqtt_topic = topic;
        const ok = await api.put(`/api/groups/${groupId}`, payload);
        if (!ok) {
            showNotification('Не удалось сохранить режим мастер-клапана', 'error');
        } else {
            const gi = groupsData.findIndex(g=>g.id===groupId);
            if (gi>=0) { groupsData[gi].master_mode = mode; }
            showNotification('Режим мастер-клапана сохранён', 'success');
        }
    } catch (e) {
        showNotification('Ошибка сохранения режима мастер-клапана', 'error');
    }
}

// ===== Autosave badge (debounced per group) =====
var __saveTimers = {};
function scheduleAutoSave(groupId){
    const badge = document.getElementById(`save-badge-${groupId}`);
    if (badge){ badge.textContent = 'Сохранение…'; badge.className = 'save-badge saving'; }
    if (__saveTimers[groupId]) clearTimeout(__saveTimers[groupId]);
    __saveTimers[groupId] = setTimeout(async ()=>{
        try{
            const mvServerSel = document.getElementById(`mv-server-${groupId}`);
            const mvServer = mvServerSel ? parseInt(mvServerSel.value) : null;
            const mvTopic = (document.getElementById(`mv-topic-${groupId}`)||{}).value || '';
            const mvModeSel = document.getElementById(`mv-mode-${groupId}`);
            const mvMode = mvModeSel ? mvModeSel.value : 'NC';

            const prServerSel = document.getElementById(`pressure-server-${groupId}`);
            const prServer = prServerSel ? parseInt(prServerSel.value) : null;
            const prTopic = (document.getElementById(`pressure-topic-${groupId}`)||{}).value || '';
            const prUnitSel = document.getElementById(`pressure-unit-${groupId}`);
            const prUnit = prUnitSel ? prUnitSel.value : 'bar';

            const wServerSel = document.getElementById(`water-server-${groupId}`);
            const wServer = wServerSel ? parseInt(wServerSel.value) : null;
            const wTopic = (document.getElementById(`water-topic-${groupId}`)||{}).value || '';
            const wPulseSel = document.getElementById(`water-pulse-${groupId}`);
            const wPulse = wPulseSel ? wPulseSel.value : '1l';

            const rainServerSel = document.getElementById(`rain-server-${groupId}`);
            const rainServer = rainServerSel ? parseInt(rainServerSel.value) : null;
            const rainTopic = (document.getElementById(`rain-topic-${groupId}`)||{}).value || '';

            const tempServerSel = document.getElementById(`temp-server-${groupId}`);
            const tempServer = tempServerSel ? parseInt(tempServerSel.value) : null;
            const tempTopic = (document.getElementById(`temp-topic-${groupId}`)||{}).value || '';

            const humServerSel = document.getElementById(`hum-server-${groupId}`);
            const humServer = humServerSel ? parseInt(humServerSel.value) : null;
            const humTopic = (document.getElementById(`hum-topic-${groupId}`)||{}).value || '';

            const payload = {
                master_mqtt_topic: mvTopic,
                master_mode: mvMode,
                master_mqtt_server_id: isNaN(mvServer)? null : mvServer,
                pressure_mqtt_topic: prTopic,
                pressure_unit: prUnit,
                pressure_mqtt_server_id: isNaN(prServer)? null : prServer,
                water_mqtt_topic: wTopic,
                water_pulse_size: wPulse,
                water_mqtt_server_id: isNaN(wServer)? null : wServer,
                rain_mqtt_topic: rainTopic,
                rain_mqtt_server_id: isNaN(rainServer)? null : rainServer,
                temp_mqtt_topic: tempTopic,
                temp_mqtt_server_id: isNaN(tempServer)? null : tempServer,
                hum_mqtt_topic: humTopic,
                hum_mqtt_server_id: isNaN(humServer)? null : humServer
            };
            const ok = await api.put(`/api/groups/${groupId}`, payload);
            if (ok){ if (badge){ badge.textContent='Сохранено'; badge.className='save-badge saved'; setTimeout(()=>{ if (badge) { badge.textContent=''; badge.className='save-badge'; } }, 1200); } }
            else { if (badge){ badge.textContent='Ошибка'; badge.className='save-badge error'; } }
        }catch(e){ if (badge){ badge.textContent='Ошибка'; badge.className='save-badge error'; } }
    }, 450);
}

// ===== Rain UI =====
function initRainUi() {
    try {
        const cfg = window.rainConfig || {enabled:false, type:'NO', topic:'', server_id:null};
        document.getElementById('rain-enabled').checked = !!cfg.enabled;
        document.getElementById('rain-type').value = (cfg.type==='NC'?'NC':'NO');
        document.getElementById('rain-topic').value = cfg.topic || '';
        const rs = document.getElementById('rain-server');
        if (rs && cfg.server_id) rs.value = String(cfg.server_id);
        try{ updateGlobalToggleTitles(); }catch(e){}
    } catch {}
}

async function saveRainConfig() {
    try {
        const enabled = document.getElementById('rain-enabled').checked;
        const type = document.getElementById('rain-type').value;
        const topic = document.getElementById('rain-topic').value;
        const rs = document.getElementById('rain-server');
        const server_id = rs && rs.value ? parseInt(rs.value) : null;
        const rainTopicInput = document.getElementById('rain-topic');
        rainTopicInput.style.border = '';
        if (enabled && (!server_id || !String(topic).trim())) {
            rainTopicInput.style.border = '2px solid #f44336';
            showNotification('Укажите MQTT сервер и MQTT-топик для датчика дождя', 'error');
            const tgl = document.getElementById('rain-enabled'); if (tgl) tgl.checked = false;
            try{ updateGlobalToggleTitles(); }catch(e){}
            return;
        }
        const resp = await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled, type, topic, server_id})});
        const data = await resp.json();
        if (data && data.success) {
            showNotification('Конфигурация датчика дождя сохранена', 'success');
            try{ window.rainConfig = window.rainConfig || {}; window.rainConfig.enabled = enabled; renderGroupsGrid(); }catch(e){}
            try{ updateGlobalToggleTitles(); }catch(e){}
        } else {
            showNotification('Не удалось сохранить конфигурацию', 'error');
        }
    } catch (e) {
        showNotification('Ошибка сохранения конфигурации', 'error');
    }
}

function openGlobalRainSettings(){ openModalById('modal-global-rain'); }
function closeGlobalRainSettings(){ closeModalById('modal-global-rain'); }
var __rainTimer = null;
function scheduleGlobalRainSave(){ if (__rainTimer) clearTimeout(__rainTimer); __rainTimer = setTimeout(saveRainConfig, 400); }

// ===== Env UI =====
function initEnvUi() {
    try {
        const cfg = window.envConfig || { temp:{enabled:false}, hum:{enabled:false} };
        document.getElementById('env-temp-enabled').checked = !!(cfg.temp && cfg.temp.enabled);
        document.getElementById('env-temp-topic').value = (cfg.temp && cfg.temp.topic) || '';
        if (cfg.temp && cfg.temp.server_id) document.getElementById('env-temp-server').value = String(cfg.temp.server_id);
        document.getElementById('env-hum-enabled').checked = !!(cfg.hum && cfg.hum.enabled);
        document.getElementById('env-hum-topic').value = (cfg.hum && cfg.hum.topic) || '';
        if (cfg.hum && cfg.hum.server_id) document.getElementById('env-hum-server').value = String(cfg.hum.server_id);
        try{ updateGlobalToggleTitles(); }catch(e){}
    } catch {}
}

async function saveEnvConfig() {
    try {
        const temp = {
            enabled: document.getElementById('env-temp-enabled').checked,
            server_id: (document.getElementById('env-temp-server').value || '') ? parseInt(document.getElementById('env-temp-server').value) : null,
            topic: document.getElementById('env-temp-topic').value,
        };
        const hum = {
            enabled: document.getElementById('env-hum-enabled').checked,
            server_id: (document.getElementById('env-hum-server').value || '') ? parseInt(document.getElementById('env-hum-server').value) : null,
            topic: document.getElementById('env-hum-topic').value,
        };
        const tempTopicInput = document.getElementById('env-temp-topic');
        const humTopicInput = document.getElementById('env-hum-topic');
        tempTopicInput.style.border = '';
        humTopicInput.style.border = '';
        if (temp.enabled && (!temp.server_id || !String(temp.topic).trim())) {
            tempTopicInput.style.border = '2px solid #f44336';
            showNotification('Укажите MQTT сервер и MQTT-топик для датчика температуры', 'error');
            const tgl = document.getElementById('env-temp-enabled'); if (tgl) tgl.checked = false;
            try{ updateGlobalToggleTitles(); }catch(e){}
            return;
        }
        if (hum.enabled && (!hum.server_id || !String(hum.topic).trim())) {
            humTopicInput.style.border = '2px solid #f44336';
            showNotification('Укажите MQTT сервер и MQTT-топик для датчика влажности', 'error');
            const tgl = document.getElementById('env-hum-enabled'); if (tgl) tgl.checked = false;
            try{ updateGlobalToggleTitles(); }catch(e){}
            return;
        }
        const resp = await fetch('/api/env', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ temp, hum })});
        const data = await resp.json();
        if (data && data.success) {
            showNotification('Настройки датчиков среды сохранены', 'success');
            try{ window.envConfig = data.config || window.envConfig || {}; updateGlobalToggleTitles(); }catch(e){}
        } else {
            if (data && data.errors) {
                if (data.errors.temp_topic) {
                    tempTopicInput.style.border = '2px solid #f44336';
                }
                if (data.errors.hum_topic) {
                    humTopicInput.style.border = '2px solid #f44336';
                }
                const msg = [data.errors.temp_topic, data.errors.hum_topic].filter(Boolean).join('. ');
                showNotification(msg || 'Не удалось сохранить настройки датчиков', 'error');
            } else {
                showNotification('Не удалось сохранить настройки датчиков', 'error');
            }
        }
    } catch (e) {
        showNotification('Ошибка сохранения настроек датчиков', 'error');
    }
}

function openGlobalEnvSettings(){ openModalById('modal-global-env'); }
function closeGlobalEnvSettings(){ closeModalById('modal-global-env'); }
var __envTimer = null;
function scheduleGlobalEnvSave(){ if (__envTimer) clearTimeout(__envTimer); __envTimer = setTimeout(saveEnvConfig, 400); }

// ===== Global toggle titles =====
function updateGlobalToggleTitles(){
    try{
        const rainEnabled = document.getElementById('rain-enabled');
        const rs = document.getElementById('rain-server');
        const rt = document.getElementById('rain-topic');
        const hasRainConfig = !!(rs && rs.value) && !!(rt && String(rt.value).trim());
        if (rainEnabled){
            rainEnabled.title = hasRainConfig ? '' : 'Укажите MQTT сервер и MQTT топик для датчика дождя';
        }
        const tempTgl = document.getElementById('env-temp-enabled');
        const ts = document.getElementById('env-temp-server');
        const tt = document.getElementById('env-temp-topic');
        const hasTempCfg = !!(ts && ts.value) && !!(tt && String(tt.value).trim());
        if (tempTgl){ tempTgl.title = hasTempCfg ? '' : 'Укажите MQTT сервер и MQTT топик для датчика температуры'; }
        const humTgl = document.getElementById('env-hum-enabled');
        const hs = document.getElementById('env-hum-server');
        const ht = document.getElementById('env-hum-topic');
        const hasHumCfg = !!(hs && hs.value) && !!(ht && String(ht.value).trim());
        if (humTgl){ humTgl.title = hasHumCfg ? '' : 'Укажите MQTT сервер и MQTT топик для датчика влажности'; }
    }catch(e){}
}
