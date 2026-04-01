    let zonesData = [];
    let groupsData = [];
    let modifiedZones = new Set();
    let modifiedGroups = new Set(); // Новый набор для отслеживания модифицированных групп
    let editingGroupId = null;
    let sortColumn = -1;
    let sortDirection = 'asc';
    
    // Загрузка данных
    async function loadData() {
        try {
            const [zonesRes, groupsRes, mqttRes, rainRes, envRes] = await Promise.all([
                api.get('/api/zones'),
                api.get('/api/groups'),
                api.get('/api/mqtt/servers'),
                api.get('/api/rain'),
                api.get('/api/env')
            ]);
            zonesData = zonesRes;
            groupsData = groupsRes;
            window.mqttServers = (mqttRes && mqttRes.servers) ? mqttRes.servers : [];
            window.rainConfig = (rainRes && rainRes.config) ? rainRes.config : {enabled:false, type:'NO', topic:'', server_id:null};
            window.envConfig = (envRes && envRes.config) ? envRes.config : { temp:{enabled:false, topic:'', server_id:null}, hum:{enabled:false, topic:'', server_id:null} };
            
            renderZonesTable();
            renderGroupsGrid();
            try { (groupsData||[]).filter(g=>g.id!==999).forEach(g=>waterInitForGroup(g)); } catch(e){}
            loadGroupSelectors();
            updateZonesCount();
            await loadEarlyOff();
            initRainUi();
            initEnvUi();
        } catch (error) {
            console.error('Ошибка загрузки данных:', error);
            showNotification('Ошибка загрузки данных', 'error');
        }
    }
    
    // Рендеринг таблицы зон
    function renderZonesTable() {
        const tbody = document.getElementById('zones-table-body');
        tbody.innerHTML = '';
        
        zonesData.forEach(zone => {
            const row = document.createElement('tr');
            row.className = 'zone-row';
            row.dataset.zoneId = zone.id;
            
            row.innerHTML = `
                <td><input type="checkbox" class="zone-checkbox" value="${zone.id}" onchange="updateSelectedCount()"></td>
                <td style="color: #333 !important;">
                    <span class="zone-status-indicator ${zone.state === 'on' ? 'active' : 'inactive'}"></span>
                    ${zone.id}
                </td>
                <td>
                    <div class="icon-dropdown">
                        <span class="zone-icon" onclick="toggleIconDropdown(${zone.id})">${zone.icon}</span>
                        <div class="icon-dropdown-content" id="icon-dropdown-${zone.id}">
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌿')">🌿 Трава</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌳')">🌳 Дерево</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌺')">🌺 Цветок</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌻')">🌻 Подсолнух</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌹')">🌹 Роза</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌸')">🌸 Сакура</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌼')">🌼 Ромашка</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌷')">🌷 Тюльпан</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌱')">🌱 Росток</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌲')">🌲 Ель</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌴')">🌴 Пальма</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌵')">🌵 Кактус</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🍀')">🍀 Клевер</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌾')">🌾 Пшеница</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🌽')">🌽 Кукуруза</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🥕')">🥕 Морковь</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🍅')">🍅 Помидор</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🥬')">🥬 Салат</div>
                            <div class="icon-option" onclick="selectIcon(${zone.id}, '🧱')">🧱 Кирпич</div>
                        </div>
                    </div>
                </td>
                <td>
                    <input type="text" class="zone-name" value="${escapeHtml(zone.name)}" 
                           onchange="updateZone(${zone.id}, 'name', this.value)">
                </td>
                <td>
                    <input type="number" class="zone-duration" value="${zone.duration}" 
                           min="1" max="240" onchange="(function(inp){ let v=parseInt(inp.value||'0'); if(isNaN(v)||v<1)v=1; if(v>240)v=240; inp.value=v; updateZone(${zone.id}, 'duration', v); })(this)">
                </td>
                <td>
                    <select class="zone-group" onchange="updateZone(${zone.id}, 'group_id', this.value)">
                        ${groupsData.map(group => 
                            (group.id === 999 ? `<option value="999" ${zone.group_id == 999 ? 'selected' : ''}>БЕЗ ПОЛИВА</option>` :
                            `<option value="${group.id}" ${zone.group_id == group.id ? 'selected' : ''}>${escapeHtml(group.name)}</option>`)
                        ).join('')}
                    </select>
                </td>
                <td>
                    <input type="text" class="zone-topic" value="${escapeHtml(zone.topic || '')}" 
                           placeholder="zone/1" onchange="updateZone(${zone.id}, 'topic', this.value)">
                </td>
                <td>
                    <select class="zone-mqtt" onchange="updateZone(${zone.id}, 'mqtt_server_id', this.value)">
                        ${window.mqttServers.map(s => `<option value="${s.id}" ${String(zone.mqtt_server_id||'')===String(s.id)?'selected':''}>${escapeHtml(s.name)}</option>`).join('')}
                    </select>
                </td>
                <td>
                    <div class="zone-photo">
                        ${zone.photo_path ? 
                            `<img src="/api/zones/${zone.id}/photo" alt="Фото зоны ${zone.id}" onclick="showPhotoModal('/api/zones/${zone.id}/photo')">` :
                            `<div class="no-photo" onclick="uploadPhoto(${zone.id})">📷</div>`
                        }
                        ${zone.photo_path ? 
                            `<div style="display:flex; gap:.3rem; margin-top:.2rem;">
                                <button class="photo-delete-btn" onclick="deletePhoto(${zone.id})">Удалить</button>
                                <button class="photo-upload-btn" onclick="rotatePhoto(${zone.id}, 90)">⟳ 90°</button>
                                <button class="photo-upload-btn" onclick="rotatePhoto(${zone.id}, -90)">⟲ -90°</button>
                            </div>` :
                            `<button class="photo-upload-btn" onclick="uploadPhoto(${zone.id})">Загрузить</button>`
                        }
                    </div>
                </td>
                <td>
                    <div class="zone-actions">
                        <button class="start-btn" onclick="toggleZone(${zone.id})">${zone.state === 'on' ? '⏹️' : '▶️'}</button>
                        <button class="delete-btn" onclick="deleteZone(${zone.id})">🗑️</button>
                    </div>
                </td>
            `;
            
            tbody.appendChild(row);
        });
        
        updateSelectedCount();
    }
    
    // Рендеринг сетки групп
    function renderGroupsGrid() {
        const container = document.getElementById('groups-grid');
        container.innerHTML = '';
        
        groupsData
            .filter(group => group.id !== 999)
            .forEach(group => {
            const card = document.createElement('div');
            card.className = 'group-card';
            card.dataset.groupId = group.id;
            card.style.position = 'relative';
            card.innerHTML = `
                <button class="delete-btn" title="Удалить группу" onclick="deleteGroup(${group.id})">✖</button>
                <div class="group-head-grid">
                    <div>
                        <label style="display:block; color:#555; font-weight:500; margin-bottom:4px;">Имя группы</label>
                    <input type="text" class="group-name" value="${escapeHtml(group.name)}" 
                           oninput="autoSaveGroupName(${group.id}, this.value)"
                           placeholder="Название группы"
                               style="width:100%; height:34px;">
                </div>
                    <div>
                        <label style="display:block; color:#555; font-weight:500; margin-bottom:4px;">Количество зон</label>
                        <input type="text" value="${group.zone_count || 0}" readonly aria-readonly style="width:100%; height:34px; background:#f1f3f5; color:#333; border:1px solid #e0e0e0; border-radius:4px; padding: 0 .5rem;">
                    </div>
                    <div class="toggle-grid">
                        <div class="toggle-row"><input type="checkbox" class="switch group-use-rain ${(!window.rainConfig || !window.rainConfig.enabled)?'blocked-off':''}" ${(window.rainConfig && window.rainConfig.enabled && group.use_rain_sensor)?'checked':''} ${(!window.rainConfig || !window.rainConfig.enabled)?'data-blocked="1"':''} title="${(!window.rainConfig || !window.rainConfig.enabled)?'Глобальный датчик дождя выключен':'Использовать датчик дождя'}" onchange="toggleGroupUseRain(${group.id}, this.checked)" onclick="if(this.getAttribute('data-blocked')==='1'){ event.preventDefault(); showNotification('Нельзя включить датчик дождя для группы: глобальный датчик дождя выключен', 'warning'); }"><label style="margin-left:6px; color:#222;">Использовать датчик дождя</label></div>
                        <div class="toggle-row"><input type="checkbox" class="switch group-use-mv" ${group.use_master_valve ? 'checked' : ''} onchange="toggleGroupUseMaster(${group.id}, this.checked)"><label style="margin-left:6px; color:#222;">Использовать мастер клапан</label></div>
                        <div class="toggle-row"><input type="checkbox" class="switch group-use-pressure" ${group.use_pressure_sensor ? 'checked' : ''} onchange="toggleGroupUsePressure(${group.id}, this.checked)"><label style="margin-left:6px; color:#222;">Использовать датчик давления</label></div>
                        <div class="toggle-row"><input type="checkbox" class="switch group-use-water" ${group.use_water_meter ? 'checked' : ''} onchange="toggleGroupUseWater(${group.id}, this.checked)"><label style="margin-left:6px; color:#222;">Использовать счётчик воды</label></div>
                    </div>
                </div>
                <div style="padding: 0 10px 10px 10px; display:flex; flex-wrap:wrap; gap:8px;">
                    <button class="group-edit-btn" onclick="openMasterSettings(${group.id})">Настройки мастер клапана</button>
                    <button class="group-edit-btn" onclick="openPressureSettings(${group.id})">Настройки датчика давления</button>
                    <button class="group-edit-btn" onclick="openWaterSettings(${group.id})">Настройки счётчика воды</button>
                    
                    <span id="save-badge-${group.id}" class="save-badge" style="margin-left:auto;"></span>
                </div>

                <div id="modal-master-${group.id}" class="modal" onclick="if(event.target===this) closeMasterSettings(${group.id})">
                    <div class="modal-content" role="dialog" aria-modal="true">
                        <div class="modal-header">
                            <div class="modal-title">Настройки мастер клапана</div>
                            <span role="button" tabindex="0" aria-label="Закрыть" class="close" onclick="closeMasterSettings(${group.id})">&times;</span>
                        </div>
                        <div class="modal-form ${group.use_master_valve ? '' : 'dim'}" aria-hidden="false">
                            <div class="form-group">
                                <label>MQTT сервер</label>
                                <select id="mv-server-${group.id}" onchange="scheduleAutoSave(${group.id})">
                                    ${(window.mqttServers||[]).map(s=>`<option value="${s.id}" ${String(s.id)===String(group.master_mqtt_server_id||'')?'selected':''}>${s.name}</option>`).join('')}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Топик MQTT мастер-клапана</label>
                                <input type="text" id="mv-topic-${group.id}" value="${(group.master_mqtt_topic||'').replaceAll('"','&quot;')}" placeholder="/devices/wb-mr6c_101/controls/K1" oninput="scheduleAutoSave(${group.id})" onblur="saveGroupMasterTopic(${group.id})">
                            </div>
                            <div class="form-group">
                                <label>Режим</label>
                                <select id="mv-mode-${group.id}" onchange="saveGroupMasterMode(${group.id})">
                                    <option value="NC" ${((group.master_mode||'NC')==='NC')?'selected':''}>NC (нормально закрыт)</option>
                                    <option value="NO" ${((group.master_mode||'NC')==='NO')?'selected':''}>NO (нормально открыт)</option>
                                </select>
                            </div>
                        </div>
                        <div class="modal-actions">
                            <button type="button" class="btn-secondary" onclick="closeMasterSettings(${group.id})">Закрыть</button>
                        </div>
                    </div>
                </div>

                <div id="modal-pressure-${group.id}" class="modal" onclick="if(event.target===this) closePressureSettings(${group.id})">
                    <div class="modal-content" role="dialog" aria-modal="true">
                        <div class="modal-header">
                            <div class="modal-title">Настройки датчика давления</div>
                            <span role="button" tabindex="0" aria-label="Закрыть" class="close" onclick="closePressureSettings(${group.id})">&times;</span>
                        </div>
                        <div class="modal-form ${group.use_pressure_sensor ? '' : 'dim'}">
                            <div class="form-group">
                                <label>MQTT сервер</label>
                                <select id="pressure-server-${group.id}" onchange="scheduleAutoSave(${group.id})">
                                    ${(window.mqttServers||[]).map(s=>`<option value="${s.id}" ${String(s.id)===String(group.pressure_mqtt_server_id||'')?'selected':''}>${s.name}</option>`).join('')}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Топик MQTT датчика давления</label>
                                <input type="text" id="pressure-topic-${group.id}" value="${(group.pressure_mqtt_topic||'').replaceAll('"','&quot;')}" placeholder="/devices/wb-ms_10/controls/pressure" oninput="scheduleAutoSave(${group.id})">
                            </div>
                            <div class="form-group">
                                <label>Единицы измерения</label>
                                <select id="pressure-unit-${group.id}" onchange="scheduleAutoSave(${group.id})">
                                    <option value="bar" ${(group.pressure_unit||'bar')==='bar'?'selected':''}>Бар</option>
                                    <option value="kpa" ${(group.pressure_unit||'bar')==='kpa'?'selected':''}>кПа</option>
                                    <option value="psi" ${(group.pressure_unit||'bar')==='psi'?'selected':''}>PSI</option>
                                </select>
                            </div>
                        </div>
                        <div class="modal-actions">
                            <button type="button" class="btn-secondary" onclick="closePressureSettings(${group.id})">Закрыть</button>
                        </div>
                    </div>
                </div>

                <div id="modal-water-${group.id}" class="modal" onclick="if(event.target===this) closeWaterSettings(${group.id})">
                    <div class="modal-content" role="dialog" aria-modal="true">
                        <div class="modal-header">
                            <div class="modal-title">Настройки счётчика воды</div>
                            <span role="button" tabindex="0" aria-label="Закрыть" class="close" onclick="closeWaterSettings(${group.id})">&times;</span>
                        </div>
                        <div class="modal-form ${group.use_water_meter ? '' : 'dim'}">
                            <div class="form-group">
                                <label>MQTT сервер</label>
                                <select id="water-server-${group.id}" onchange="scheduleAutoSave(${group.id})">
                                    ${(window.mqttServers||[]).map(s=>`<option value="${s.id}" ${String(s.id)===String(group.water_mqtt_server_id||'')?'selected':''}>${s.name}</option>`).join('')}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Топик MQTT счётчика воды</label>
                                <input type="text" id="water-topic-${group.id}" value="${(group.water_mqtt_topic||'').replaceAll('"','&quot;')}" placeholder="/devices/wb-water/controls/meter" oninput="scheduleAutoSave(${group.id})">
                            </div>
                            <div class="form-group">
                                <label>Величина импульса</label>
                                <select id="water-pulse-${group.id}" onchange="scheduleAutoSave(${group.id})">
                                    <option value="1l" ${(group.water_pulse_size||'1l')==='1l'?'selected':''}>1 л</option>
                                    <option value="10l" ${(group.water_pulse_size||'1l')==='10l'?'selected':''}>10 л</option>
                                    <option value="100l" ${(group.water_pulse_size||'1l')==='100l'?'selected':''}>100 л</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Импульсы WB-MWAC (текущее)</label>
                                <input type="text" id="water-pulses-${group.id}" value="" readonly aria-readonly style="background:#f1f3f5">
                            </div>
                            <div class="form-group">
                                <label>Показание счётчика (м³)</label>
                                <div class="water-digits" id="water-digits-${group.id}" aria-label="Показание счётчика">
                                    ${[0,1,2,3,4,5,6,7].map(i=>`
                                    <div class="digit-col" data-i="${i}">
                                        <button type="button" class="btn-small" onclick="waterIncDigit(${group.id}, ${i}, 1)">+</button>
                                        <div class="digit" id="water-digit-${group.id}-${i}">0</div>
                                        <button type="button" class="btn-small" onclick="waterIncDigit(${group.id}, ${i}, -1)">−</button>
                                    </div>`).join('')}
                                </div>
                                
                            </div>
                        </div>
                        <div class="modal-actions">
                            <div id="water-actions-${group.id}" style="display:none; gap:8px;">
                                <button type="button" class="btn-secondary" onclick="waterCancel(${group.id})">Отменить</button>
                                <button type="button" class="btn-secondary" onclick="waterProbeOnce(${group.id})">Получить данные по MQTT</button>
                            </div>
                            <button type="button" class="btn-secondary" onclick="closeWaterSettings(${group.id})">Закрыть</button>
                        </div>
                    </div>
                </div>


            `;
            container.appendChild(card);
        });
    }

    function markGroupModified(groupId) {
        modifiedGroups.add(groupId);
    }

    function updateSaveAllButton() {}
    
    // Загрузка селекторов групп
    function loadGroupSelectors() {
        const selectors = ['bulkGroup', 'zoneGroup'];
        selectors.forEach(selectorId => {
            const selector = document.getElementById(selectorId);
            if (selector) {
                selector.innerHTML = groupsData.map(group => {
                    // В массовых действиях тоже показываем группу 999 (БЕЗ ПОЛИВА)
                    return `<option value="${group.id}">${group.id===999?'БЕЗ ПОЛИВА':escapeHtml(group.name)}</option>`;
                }).join('');
            }
        });
        // MQTT servers for bulk
        const bulkMqtt = document.getElementById('bulkMqtt');
        if (bulkMqtt) {
            bulkMqtt.innerHTML = (window.mqttServers||[]).map(s=>`<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
        }
        // Rain server selector
        const rs = document.getElementById('rain-server');
        if (rs) {
            rs.innerHTML = (window.mqttServers||[]).map(s=>`<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
        }
        // Env servers
        const ets = document.getElementById('env-temp-server');
        if (ets) ets.innerHTML = (window.mqttServers||[]).map(s=>`<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
        const ehs = document.getElementById('env-hum-server');
        if (ehs) ehs.innerHTML = (window.mqttServers||[]).map(s=>`<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
    }
    
    // Обновление счетчика зон
    function updateZonesCount() {
        document.getElementById('zones-count').textContent = zonesData.length;
    }

    // Автосохранение имени группы
    let _saveTimers = {};
    function autoSaveGroupName(groupId, value) {
        markGroupModified(groupId);
        if (_saveTimers[groupId]) clearTimeout(_saveTimers[groupId]);
        _saveTimers[groupId] = setTimeout(async () => {
            try {
                const ok = await api.put(`/api/groups/${groupId}`, { name: value });
                if (ok && ok.success) {
                    const gi = groupsData.findIndex(g=>g.id===groupId);
                    if (gi>=0) groupsData[gi].name = value;
                } else {
                    showNotification('Не удалось сохранить имя группы', 'error');
                }
            } catch (e) {
                showNotification('Ошибка сохранения группы', 'error');
            }
        }, 400);
    }

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
            // Клиентская валидация
            const rainTopicInput = document.getElementById('rain-topic');
            rainTopicInput.style.border = '';
            if (enabled && (!server_id || !String(topic).trim())) {
                rainTopicInput.style.border = '2px solid #f44336';
                showNotification('Укажите MQTT сервер и MQTT-топик для датчика дождя', 'error');
                // Откат тумблера
                const tgl = document.getElementById('rain-enabled'); if (tgl) tgl.checked = false;
                try{ updateGlobalToggleTitles(); }catch(e){}
                return;
            }
            const resp = await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled, type, topic, server_id})});
            const data = await resp.json();
            if (data && data.success) {
                showNotification('Конфигурация датчика дождя сохранена', 'success');
                // Если глобально выключили — запретим переключатели у групп
                try{ window.rainConfig = window.rainConfig || {}; window.rainConfig.enabled = enabled; renderGroupsGrid(); }catch(e){}
                try{ updateGlobalToggleTitles(); }catch(e){}
            } else {
                showNotification('Не удалось сохранить конфигурацию', 'error');
            }
        } catch (e) {
            showNotification('Ошибка сохранения конфигурации', 'error');
        }
    }

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

    // === Master Valve handlers (UI only; backend to be added) ===
    async function toggleGroupUseMaster(groupId, enabled) {
        try {
            const topicEl = document.getElementById(`mv-topic-${groupId}`);
            const modeEl = document.getElementById(`mv-mode-${groupId}`);
            const serverEl = document.getElementById(`mv-server-${groupId}`);
            const topic = (topicEl && topicEl.value ? topicEl.value.trim() : '');
            if (topicEl) topicEl.style.border = '';
            // Валидация при включении: требуем MQTT сервер и MQTT топик
            if (enabled) {
                const serverOk = !!(serverEl && String(serverEl.value||'').trim());
                const topicOk = !!topic;
                if (!serverOk || !topicOk) {
                    if (topicEl && !topicOk) topicEl.style.border = '2px solid #f44336';
                    // Откат чекбокса
                    const cb = document.querySelector(`[data-group-id="${groupId}"] .group-use-mv`);
                    if (cb) cb.checked = false;
                    showNotification('Укажите MQTT сервер и MQTT-топик для мастер-клапана', 'warning');
                    return;
                }
            }
            // Save immediately (will be no-op until backend supports fields)
            const payload = { use_master_valve: !!enabled };
            if (topic) payload.master_mqtt_topic = topic;
            if (modeEl && modeEl.value) payload.master_mode = modeEl.value;
            if (serverEl && serverEl.value) payload.master_mqtt_server_id = parseInt(serverEl.value);
            const ok = await api.put(`/api/groups/${groupId}`, payload);
            if (!ok) {
                showNotification('Не удалось сохранить настройки мастер-клапана', 'error');
            } else {
                // update local cache
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

    // === Pressure & Water toggles (UI only; backend to be added) ===
    async function toggleGroupUsePressure(groupId, enabled){
        try {
            // Валидация при включении
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
            // Валидация при включении
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

    // === Settings modals open/close ===
    // === Unified modal open/close and sizing (consistent across devices) ===
    function _calcViewport(){
        const vv = (window.visualViewport ? { w: window.visualViewport.width, h: window.visualViewport.height } : null);
        return {
            w: Math.max(320, Math.min(window.innerWidth || 0, screen.width || Infinity, vv ? vv.w : Infinity)),
            h: Math.max(320, Math.min(window.innerHeight || 0, screen.height || Infinity, vv ? vv.h : Infinity))
        };
    }
    function _sizeModalContent(modal){
        try{
            const c = modal.querySelector('.modal-content'); if (!c) return;
            const vp = _calcViewport();
            const maxW = Math.min(520, vp.w - 32);
            const widthPx = Math.max(280, maxW);
            const maxH = Math.min(Math.round(vp.h*0.92), vp.h - 32);
            c.style.margin = '0';
            c.style.width = widthPx + 'px';
            c.style.maxWidth = widthPx + 'px';
            c.style.maxHeight = maxH + 'px';
        }catch(e){}
    }
    function openModalById(id){
        const m = document.getElementById(id); if (!m) return;
        // Move modal to body to avoid transformed ancestors affecting fixed positioning (iOS Safari bug)
        try { if (m.parentElement && m.parentElement !== document.body) { document.body.appendChild(m); } } catch(e){}
        m.style.display = 'flex';
        _sizeModalContent(m);
        // CSS handles viewport centering; no JS overrides to avoid off-screen drift
    }
    function closeModalById(id){ const m = document.getElementById(id); if (!m) return; m.style.display='none'; }
    function recenterOpenModals(){
        try{
            document.querySelectorAll('.modal').forEach(m=>{
                if (m instanceof HTMLElement && getComputedStyle(m).display !== 'none') _sizeModalContent(m);
                // Positioning handled by CSS (50vw/50dvh)
            });
        }catch(e){}
    }
    window.addEventListener('resize', recenterOpenModals);
    window.addEventListener('orientationchange', recenterOpenModals);
    if (window.visualViewport){ try{ window.visualViewport.addEventListener('resize', recenterOpenModals); }catch(e){} }

    // === Global Rain modal helpers + autosave ===
    function openGlobalRainSettings(){ openModalById('modal-global-rain'); }
    function closeGlobalRainSettings(){ closeModalById('modal-global-rain'); }
    let __rainTimer = null;
    function scheduleGlobalRainSave(){ if (__rainTimer) clearTimeout(__rainTimer); __rainTimer = setTimeout(saveRainConfig, 400); }

    // === Global Env modal helpers + autosave ===
    function openGlobalEnvSettings(){ openModalById('modal-global-env'); }
    function closeGlobalEnvSettings(){ closeModalById('modal-global-env'); }
    let __envTimer = null;
    function scheduleGlobalEnvSave(){ if (__envTimer) clearTimeout(__envTimer); __envTimer = setTimeout(saveEnvConfig, 400); }

    // Settings modals using unified helpers
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

    // Safety: recenter on resize/orientationchange
    function recenterOpenModals(){
        try{
            const opened = document.querySelectorAll('.modal');
            opened.forEach(el=>{
                if ((el instanceof HTMLElement) && getComputedStyle(el).display !== 'none'){
                    // force reflow to ensure flex centering recalculates
                    el.style.alignItems = 'center';
                }
            });
        }catch(e){}
    }
    window.addEventListener('resize', recenterOpenModals);
    window.addEventListener('orientationchange', recenterOpenModals);

    // === Autosave badge (debounced per group) ===
    const __saveTimers = {};
    function scheduleAutoSave(groupId){
        const badge = document.getElementById(`save-badge-${groupId}`);
        if (badge){ badge.textContent = 'Сохранение…'; badge.className = 'save-badge saving'; }
        if (__saveTimers[groupId]) clearTimeout(__saveTimers[groupId]);
        __saveTimers[groupId] = setTimeout(async ()=>{
            try{
                // collect payload from current DOM (all sensors)
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

    // ===== Water meter helpers =====
    const __waterState = {}; // { [groupId]: { editing: false, baseValueM3: 0, basePulses: 0, currentPulses: 0, pulseSize: '1l' } }
    function waterMarkDirty(groupId){
        __waterState[groupId] = __waterState[groupId] || {};
        __waterState[groupId].editing = true;
        const actions = document.getElementById(`water-actions-${groupId}`);
        if (actions) actions.style.display = 'flex';
    }
    function waterDigitsToValue(groupId){
        let val = 0;
        for (let i=0;i<8;i++){
            const el = document.getElementById(`water-digit-${groupId}-${i}`);
            const d = el ? parseInt(el.textContent||'0')||0 : 0;
            if (i<5){ // integer part positions 0..4 (hundred-thousands..ones)
                const pow = 4 - i; // i=0 -> 10^4, i=4 -> 10^0
                val += d * Math.pow(10, pow);
            } else {
                const fracPos = i - 4; // i=5 -> 1st decimal
                val += d * Math.pow(10, -fracPos);
            }
        }
        return Number(val.toFixed(3));
    }
    function waterSetDigits(groupId, valueM3){
        const v = Math.max(0, Math.min(99999.999, Number(valueM3)||0));
        const intPart = Math.floor(v);
        const fracPart = Math.round((v - intPart) * 1000); // 0..999
        const digits = [
            Math.floor(intPart / 10000) % 10,
            Math.floor(intPart / 1000) % 10,
            Math.floor(intPart / 100) % 10,
            Math.floor(intPart / 10) % 10,
            intPart % 10,
            Math.floor(fracPart / 100) % 10,
            Math.floor(fracPart / 10) % 10,
            fracPart % 10
        ];
        for (let i=0;i<8;i++){
            const el = document.getElementById(`water-digit-${groupId}-${i}`);
            if (el) el.textContent = String(digits[i]);
        }
    }
    function waterIncDigit(groupId, idx, delta){
        const el = document.getElementById(`water-digit-${groupId}-${idx}`);
        if (!el) return;
        let d = parseInt(el.textContent||'0')||0;
        d = (d + delta + 10) % 10;
        el.textContent = String(d);
        waterMarkDirty(groupId);
    }
    function waterCurrentFromPulses(groupId){
        const st = __waterState[groupId] || {};
        const pulseSize = (document.getElementById(`water-pulse-${groupId}`)||{}).value || st.pulseSize || '1l';
        const litersPerPulse = pulseSize==='100l' ? 100 : (pulseSize==='10l' ? 10 : 1);
        const baseM3 = Number(st.baseValueM3||0);
        const deltaPulses = Number(st.currentPulses||0) - Number(st.basePulses||0);
        const deltaM3 = (deltaPulses * litersPerPulse) / 1000.0;
        return Math.max(0, baseM3 + deltaM3);
    }
    async function waterProbeOnce(groupId){
        try{
            const serverEl = document.getElementById(`water-server-${groupId}`);
            const topicEl = document.getElementById(`water-topic-${groupId}`);
            const sid = serverEl && serverEl.value ? parseInt(serverEl.value) : null;
            const topic = topicEl && topicEl.value ? topicEl.value.trim() : '';
            if (!sid || !topic){ showNotification('Укажите MQTT сервер и MQTT-топик счётчика воды', 'warning'); return; }
            const res = await fetch(`/api/mqtt/${sid}/probe`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ filter: topic, duration: 1.5 })});
            const data = await res.json();
            const item = (data && data.items || []).find(it=> it.topic===topic);
            if (!item){ showNotification(`Нет данных по топику: ${topic}`, 'warning'); return; }
            const pulses = parseInt((item.payload||'').replace(/[^0-9-]/g,''))||0;
            const pulsesEl = document.getElementById(`water-pulses-${groupId}`);
            if (pulsesEl) pulsesEl.value = String(pulses);
            __waterState[groupId] = __waterState[groupId] || {};
            __waterState[groupId].currentPulses = pulses;
            if (!__waterState[groupId].editing){
                const val = waterCurrentFromPulses(groupId);
                waterSetDigits(groupId, val);
            }
        }catch(e){ showNotification('Ошибка запроса MQTT', 'error'); }
    }
    function waterCancel(groupId){
        const st = __waterState[groupId] || {};
        __waterState[groupId].editing = false;
        const actions = document.getElementById(`water-actions-${groupId}`);
        if (actions) actions.style.display = 'none';
        waterSetDigits(groupId, Number(st.baseValueM3||0));
    }
    async function waterAutoSave(groupId){
        try{
            const newVal = waterDigitsToValue(groupId);
            const pulsesEl = document.getElementById(`water-pulses-${groupId}`);
            const curP = pulsesEl ? parseInt(pulsesEl.value||'0')||0 : (__waterState[groupId]?.currentPulses||0);
            const payload = { water_base_value_m3: newVal, water_base_pulses: curP };
            const ok = await api.put(`/api/groups/${groupId}`, payload);
            if (!ok){ return; }
            __waterState[groupId] = __waterState[groupId] || {};
            __waterState[groupId].baseValueM3 = newVal;
            __waterState[groupId].basePulses = curP;
            __waterState[groupId].editing = false;
        }catch(e){}
    }
    const __waterSaveTimers = {};
    function scheduleWaterAutoSave(groupId){
        if (__waterSaveTimers[groupId]) clearTimeout(__waterSaveTimers[groupId]);
        __waterSaveTimers[groupId] = setTimeout(()=>{ waterAutoSave(groupId); }, 600);
    }
    async function waterFlushSave(groupId){
        try{
            const wServerSel = document.getElementById(`water-server-${groupId}`);
            const wServer = wServerSel ? parseInt(wServerSel.value) : null;
            const wTopic = (document.getElementById(`water-topic-${groupId}`)||{}).value || '';
            const wPulseSel = document.getElementById(`water-pulse-${groupId}`);
            const wPulse = wPulseSel ? wPulseSel.value : '1l';
            const payload = {
                water_mqtt_server_id: isNaN(wServer)? null : wServer,
                water_mqtt_topic: wTopic,
                water_pulse_size: wPulse
            };
            await api.put(`/api/groups/${groupId}`, payload);
        }catch(e){ /* no-op */ }
    }
    function waterInitForGroup(group){
        __waterState[group.id] = __waterState[group.id] || {};
        __waterState[group.id].pulseSize = group.water_pulse_size || '1l';
        __waterState[group.id].baseValueM3 = Number(group.water_base_value_m3||0);
        __waterState[group.id].basePulses = parseInt(group.water_base_pulses||0)||0;
        __waterState[group.id].currentPulses = __waterState[group.id].basePulses;
        __waterState[group.id].editing = false;
        // set digits to base on render
        setTimeout(()=>{ try{ waterSetDigits(group.id, __waterState[group.id].baseValueM3); }catch(e){} }, 0);
    }
    const __waterLiveTimers = {};
    function waterStartLive(groupId){
        waterStopLive(groupId);
        const fn = async ()=>{
            if (__waterState[groupId]?.editing) return; // don't live-update while editing
            await waterProbeOnce(groupId);
            if (!__waterState[groupId]?.editing){
                const val = waterCurrentFromPulses(groupId);
                waterSetDigits(groupId, val);
            }
        };
        __waterLiveTimers[groupId] = setInterval(fn, 3000);
        fn();
    }
    function waterStopLive(groupId){
        if (__waterLiveTimers[groupId]){ clearInterval(__waterLiveTimers[groupId]); delete __waterLiveTimers[groupId]; }
    }

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
            // Клиентская валидация
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
                // Подсветим оба поля, если есть ошибки
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

    // === Titles and availability hints for global toggles ===
    function updateGlobalToggleTitles(){
        try{
            // Rain
            const rainEnabled = document.getElementById('rain-enabled');
            const rs = document.getElementById('rain-server');
            const rt = document.getElementById('rain-topic');
            const hasRainConfig = !!(rs && rs.value) && !!(rt && String(rt.value).trim());
            if (rainEnabled){
                rainEnabled.title = hasRainConfig ? '' : 'Укажите MQTT сервер и MQTT топик для датчика дождя';
            }
            // Group rain checkboxes already disabled when global off via renderGroupsGrid()
            // Env temp
            const tempTgl = document.getElementById('env-temp-enabled');
            const ts = document.getElementById('env-temp-server');
            const tt = document.getElementById('env-temp-topic');
            const hasTempCfg = !!(ts && ts.value) && !!(tt && String(tt.value).trim());
            if (tempTgl){ tempTgl.title = hasTempCfg ? '' : 'Укажите MQTT сервер и MQTT топик для датчика температуры'; }
            // Env hum
            const humTgl = document.getElementById('env-hum-enabled');
            const hs = document.getElementById('env-hum-server');
            const ht = document.getElementById('env-hum-topic');
            const hasHumCfg = !!(hs && hs.value) && !!(ht && String(ht.value).trim());
            if (humTgl){ humTgl.title = hasHumCfg ? '' : 'Укажите MQTT сервер и MQTT топик для датчика влажности'; }
        }catch(e){}
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
    
    // Обновление зоны (отметка как измененной)
    function updateZone(zoneId, field, value) {
        const row = document.querySelector(`tr[data-zone-id="${zoneId}"]`);
        if (row) {
            row.classList.add('modified');
            modifiedZones.add(zoneId);
            
            // Автосохранение через debounce
            scheduleZoneAutoSave(zoneId);
        }
    }
    
    // Сохранение зоны
    async function saveZone(zoneId) {
        try {
            const zone = zonesData.find(z => z.id === zoneId);
            if (!zone) return;
            
            const row = document.querySelector(`tr[data-zone-id="${zoneId}"]`);
            const nameInput = row.querySelector('.zone-name');
            const durationInput = row.querySelector('.zone-duration');
            const groupSelect = row.querySelector('.zone-group');
            const topicInput = row.querySelector('.zone-topic');
            const mqttSelect = row.querySelector('.zone-mqtt');
            
            const updatedZone = {
                ...zone,
                name: nameInput.value,
                duration: parseInt(durationInput.value),
                group_id: parseInt(groupSelect.value)
            };
            if (topicInput) {
                updatedZone.topic = topicInput.value;
            }
            if (mqttSelect) {
                const val = mqttSelect.value;
                updatedZone.mqtt_server_id = val === '' ? null : parseInt(val);
            }

            // Проверка конфликтов (как было)
            try {
                const r = await fetch('/api/zones/check-duration-conflicts-bulk', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ changes: [{ zone_id: zoneId, new_duration: updatedZone.duration }] })
                });
                const result = await r.json();
                const zres = result && result.results && result.results[String(zoneId)];
                if (zres && zres.has_conflicts) {
                    showDurationConflictModal(zres.conflicts);
                    showNotification('Обнаружены конфликты программ. Изменение не сохранено.', 'warning');
                    return;
                }
            } catch (err) {}
            
            const success = await api.put(`/api/zones/${zoneId}`, updatedZone);
            if (success) {
                const zoneIndex = zonesData.findIndex(z => z.id === zoneId);
                if (zoneIndex !== -1) {
                    zonesData[zoneIndex] = { ...zonesData[zoneIndex], ...updatedZone };
                }
                row.classList.remove('modified');
                modifiedZones.delete(zoneId);
                showNotification('Зона сохранена', 'success');
                renderGroupsGrid();
            }
        } catch (error) {
            showNotification('Ошибка автосохранения зоны', 'error');
        }
    }
    
    // Удаление зоны
    async function deleteZone(zoneId) {
        if (!confirm(`Удалить зону ${zoneId}?`)) return;
        
        try {
            const success = await api.delete(`/api/zones/${zoneId}`);
            if (success) {
                // Удаляем зону из локального массива
                zonesData = zonesData.filter(z => z.id !== zoneId);
                modifiedZones.delete(zoneId);
                
                showNotification('Зона удалена', 'success');
                
                // Обновляем отображение
                renderZonesTable();
                renderGroupsGrid();
                updateZonesCount();
            }
        } catch (error) {
            console.error('Ошибка удаления зоны:', error);
            showNotification('Ошибка удаления зоны', 'error');
        }
    }

    // Показать модалку конфликтов длительности зоны
    function showDurationConflictModal(conflicts) {
        const modal = document.getElementById('conflictModal');
        const content = document.getElementById('conflictContent');
        if (!Array.isArray(conflicts) || conflicts.length === 0) {
            content.innerHTML = '<p>Конфликты не обнаружены.</p>';
        } else {
            const items = conflicts.map(c => {
                const start = toTimeString(c.overlap_start);
                const end = toTimeString(c.overlap_end);
                const groups = (c.common_groups || []).join(', ');
                const zones = (c.common_zones || []).join(', ');
                return `<li style="margin-bottom: .5rem;">
                    <div><strong>${escapeHtml(c.checked_program_name)}</strong> (${c.checked_program_time}) ↔ <strong>${escapeHtml(c.other_program_name)}</strong> (${c.other_program_time})</div>
                    <div>Пересечение: ${start} - ${end}</div>
                    ${zones ? `<div>Зоны: ${zones}</div>` : ''}
                    ${groups ? `<div>Группы: ${groups}</div>` : ''}
                </li>`;
            }).join('');
            content.innerHTML = `<p>Изменение длительности приведет к пересечению программ. Изменение не будет сохранено. Измените время зоны или расписание программ.</p><ul>${items}</ul>`;
        }
        modal.style.display = 'block';
    }

    function closeConflictModal() {
        const modal = document.getElementById('conflictModal');
        modal.style.display = 'none';
    }

    function toTimeString(totalMinutes) {
        const h = Math.floor(totalMinutes / 60) % 24;
        const m = totalMinutes % 60;
        return `${(''+h).padStart(2,'0')}:${(''+m).padStart(2,'0')}`;
    }
    
    // Запуск зоны
    async function startZone(zoneId) {
        try {
            // Проверяем, не запущена ли уже зона в той же группе
            const currentZone = zonesData.find(z => z.id === zoneId);
            if (!currentZone) {
                showNotification('Зона не найдена', 'error');
                return;
            }
            
            const activeZoneInGroup = zonesData.find(z => 
                z.group_id === currentZone.group_id && 
                z.id !== zoneId && 
                z.state === 'on'
            );
            
            if (activeZoneInGroup) {
                showNotification(`В группе уже запущена зона ${activeZoneInGroup.id}. Остановите её перед запуском новой зоны.`, 'warning');
                return;
            }
            
            // MQTT: публикуем '1' и ждём подтверждения через zones-sse
            const response = await api.post(`/api/zones/${zoneId}/mqtt/start`);
            
            if (response.success) {
                showNotification(`Зона ${zoneId} запущена`, 'success');
                
                // Обновляем статус зоны в локальном массиве
                const zoneIndex = zonesData.findIndex(z => z.id === zoneId);
                if (zoneIndex !== -1) {
                    zonesData[zoneIndex].state = 'on';
                }
                
                // Обновляем отображение
                renderZonesTable();
                
                // Обновляем статус на странице статуса
                if (window.location.pathname === '/') {
                    loadStatusData();
                }
            } else {
                showNotification(response.message, 'error');
            }
        } catch (error) {
            console.error('Ошибка запуска зоны:', error);
            showNotification('Ошибка запуска зоны', 'error');
        }
    }
    
    // Остановка зоны
    async function stopZone(zoneId) {
        try {
            const response = await api.post(`/api/zones/${zoneId}/mqtt/stop`);
            
            if (response.success) {
                showNotification(`Зона ${zoneId} остановлена`, 'success');
                
                // Обновляем статус зоны в локальном массиве
                const zoneIndex = zonesData.findIndex(z => z.id === zoneId);
                if (zoneIndex !== -1) {
                    zonesData[zoneIndex].state = 'off';
                }
                
                // Обновляем отображение
                renderZonesTable();
                
                // Обновляем статус на странице статуса
                if (window.location.pathname === '/') {
                    loadStatusData();
                }
            } else {
                showNotification(response.message, 'error');
            }
        } catch (error) {
            console.error('Ошибка остановки зоны:', error);
            showNotification('Ошибка остановки зоны', 'error');
        }
    }
    
    // Переключатель пуск/стоп одной кнопкой (как на странице статус)
    async function toggleZone(zoneId) {
        try {
            const zone = zonesData.find(z => z.id === zoneId);
            if (!zone) {
                showNotification('Зона не найдена', 'error');
                return;
            }
            if (zone.state === 'on') {
                await stopZone(zoneId);
            } else {
                await startZone(zoneId);
            }
        } catch (e) {
            showNotification('Ошибка переключения зоны', 'error');
        }
    }
    
    // Изменение иконки зоны
    function changeZoneIcon(zoneId) {
        const icons = ['🌿', '🌳', '🌺', '🌻', '🌹', '🌸', '🌼', '🌷', '🌱', '🌲', '🌴', '🌵', '🍀', '🌾', '🌽', '🥕', '🍅', '🥬', '🧱'];
        const currentIcon = zonesData.find(z => z.id === zoneId)?.icon || '🌿';
        const currentIndex = icons.indexOf(currentIcon);
        const nextIndex = (currentIndex + 1) % icons.length;
        const newIcon = icons[nextIndex];
        
        updateZone(zoneId, 'icon', newIcon);
        const iconElement = document.querySelector(`tr[data-zone-id="${zoneId}"] .zone-icon`);
        if (iconElement) {
            iconElement.textContent = newIcon;
        }
    }
    
    // Массовые действия
    function updateBulkForm() {
        const action = document.getElementById('bulkAction').value;
        document.getElementById('bulkGroupSelect').style.display = action === 'group' ? 'block' : 'none';
        document.getElementById('bulkIconSelect').style.display = action === 'icon' ? 'block' : 'none';
        document.getElementById('bulkDurationSelect').style.display = action === 'duration' ? 'block' : 'none';
        const mqttSel = document.getElementById('bulkMqttSelect'); if (mqttSel) mqttSel.style.display = action === 'mqtt' ? 'block' : 'none';
    }
    
    async function applyBulkAction() {
        const action = document.getElementById('bulkAction').value;
        if (!action) {
            showNotification('Выберите действие', 'warning');
            return;
        }
        
        const selectedZones = Array.from(document.querySelectorAll('.zone-checkbox:checked')).map(cb => parseInt(cb.value));
        if (selectedZones.length === 0) {
            showNotification('Выберите зоны для изменения', 'warning');
            return;
        }
        
        try {
            let value = null;
            switch (action) {
                case 'group':
                    value = parseInt(document.getElementById('bulkGroup').value);
                    break;
                case 'icon':
                    value = document.getElementById('bulkIcon').value;
                    break;
                case 'duration':
                    value = parseInt(document.getElementById('bulkDuration').value);
                    break;
                case 'mqtt':
                    value = parseInt(document.getElementById('bulkMqtt').value);
                    break;
            }
            
            // Действия, требующие специальных API
            if (action === 'delete' || action === 'delphoto') {
                for (const zoneId of selectedZones) {
                    if (action === 'delete') {
                        await api.delete(`/api/zones/${zoneId}`);
                    } else {
                        await api.delete(`/api/zones/${zoneId}/photo`);
                    }
                }
                showNotification(`Изменения применены к ${selectedZones.length} зонам`, 'success');
                await loadData();
                return;
            }

            // Массовое изменение длительности: сначала одна bulk-проверка конфликтов,
            // затем одна транзакционная запись только для зон без конфликтов
            if (action === 'duration') {
                const payload = { changes: selectedZones.map(zid => ({ zone_id: zid, new_duration: parseInt(value) })) };
                let check = null;
                try {
                    const r = await fetch('/api/zones/check-duration-conflicts-bulk', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                    check = await r.json();
                } catch(e) { check = null; }
                const results = (check && check.results) || {};
                const conflicted = [];
                const okIds = [];
                for (const zid of selectedZones) {
                    const zr = results[String(zid)];
                    if (zr && zr.has_conflicts) conflicted.push(zid); else okIds.push(zid);
                }
                if (conflicted.length > 0) {
                    showNotification(`Конфликты у зон: ${conflicted.join(', ')} — они пропущены`, 'warning');
                }
                if (okIds.length > 0) {
                    const zonesPayload = okIds.map(id => ({ id, duration: parseInt(value) }));
                    const resp = await fetch('/api/zones/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ zones: zonesPayload }) });
                    const j = await resp.json();
                    if (!j || !j.success) {
                        showNotification('Ошибка применения изменений', 'error');
                    } else {
                        showNotification(`Обновлено ${j.updated}, создано ${j.created}, ошибок ${j.failed}`, 'success');
                    }
                }
                await loadData();
                return;
            }

            // Остальные массовые операции оформляем одним импортом
            let zonesPayload = [];
            if (action === 'group') {
                zonesPayload = selectedZones.map(id => ({ id, group_id: parseInt(value) }));
            } else if (action === 'icon') {
                zonesPayload = selectedZones.map(id => ({ id, icon: value }));
            } else if (action === 'mqtt') {
                zonesPayload = selectedZones.map(id => ({ id, mqtt_server_id: parseInt(value) }));
            }
            if (zonesPayload.length > 0) {
                const resp = await fetch('/api/zones/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ zones: zonesPayload }) });
                const j = await resp.json();
                if (!j || !j.success) {
                    showNotification('Ошибка применения изменений', 'error');
                } else {
                    showNotification(`Обновлено ${j.updated}, создано ${j.created}, ошибок ${j.failed}`, 'success');
                }
                await loadData();
            }
            
        } catch (error) {
            console.error('Ошибка массового действия:', error);
            showNotification('Ошибка применения изменений', 'error');
        }
    }
    
    // Сортировка таблицы
    function sortTable(columnIndex) {
        if (sortColumn === columnIndex) {
            sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
        } else {
            sortColumn = columnIndex;
            sortDirection = 'asc';
        }
        
        // Обновляем заголовки
        document.querySelectorAll('.zones-table th').forEach((th, index) => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (index === columnIndex) {
                th.classList.add(`sort-${sortDirection}`);
            }
        });
        
        // Сортируем данные
        zonesData.sort((a, b) => {
            let aVal, bVal;
            
            switch (columnIndex) {
                case 0: // № Зоны
                    aVal = a.id;
                    bVal = b.id;
                    break;
                case 1: // Иконка
                    aVal = a.icon;
                    bVal = b.icon;
                    break;
                case 2: // Название
                    aVal = a.name;
                    bVal = b.name;
                    break;
                case 3: // Время
                    aVal = a.duration;
                    bVal = b.duration;
                    break;
                case 4: // Группа
                    aVal = groupsData.find(g => g.id === a.group_id)?.name || '';
                    bVal = groupsData.find(g => g.id === b.group_id)?.name || '';
                    break;
                default:
                    return 0;
            }
            
            if (sortDirection === 'asc') {
                return aVal > bVal ? 1 : -1;
            } else {
                return aVal < bVal ? 1 : -1;
            }
        });
        
        renderZonesTable();
    }
    
    // Модальные окна для зон и групп
    function showZoneModal() { openModalById('zoneModal'); }
    
    function closeZoneModal() { closeModalById('zoneModal'); const f = document.getElementById('zoneForm'); if (f) f.reset(); }
    
    function editGroup(groupId, groupName) {
        editingGroupId = groupId;
        document.getElementById('groupName').value = groupName;
        openModalById('groupModal');
    }
    
    function closeGroupModal() { closeModalById('groupModal'); editingGroupId = null; }
    
    function createGroup() {
        editingGroupId = null;
        document.getElementById('groupName').value = '';
        openModalById('groupModal');
    }

    function showAddGroupModal() { openModalById('addGroupModal'); }
    function closeAddGroupModal() { closeModalById('addGroupModal'); const f = document.getElementById('addGroupForm'); if (f){ f.reset(); } }
    
    // Обработчики форм
    document.getElementById('zoneForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const zoneData = {
            name: document.getElementById('zoneName').value,
            icon: document.getElementById('zoneIcon').value,
            duration: parseInt(document.getElementById('zoneDuration').value),
            group_id: parseInt(document.getElementById('zoneGroup').value),
            topic: document.getElementById('zoneTopic').value,
            mqtt_server_id: (window.mqttServers||[]).length === 1 ? ((window.mqttServers[0] && window.mqttServers[0].id) || null) : undefined
        };
        
        try {
            const success = await api.post('/api/zones', zoneData);
            if (success) {
                showNotification('Зона создана', 'success');
                closeZoneModal();
                await loadData();
            }
        } catch (error) {
            console.error('Ошибка создания зоны:', error);
            showNotification('Ошибка создания зоны', 'error');
        }
    });
    
    document.getElementById('groupForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const groupName = document.getElementById('groupName').value;
        
        try {
            if (editingGroupId) {
                const success = await api.put(`/api/groups/${editingGroupId}`, { name: groupName });
                if (success) {
                    showNotification('Группа обновлена', 'success');
                }
            } else {
                const success = await api.post('/api/groups', { name: groupName });
                if (success) {
                    showNotification('Группа создана', 'success');
                }
            }
            
            closeGroupModal();
            await loadData();
        } catch (error) {
            console.error('Ошибка сохранения группы:', error);
            showNotification('Ошибка сохранения группы', 'error');
        }
    });

    document.getElementById('addGroupForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const name = document.getElementById('newGroupName').value || 'Новая группа';
        try {
            const res = await api.post('/api/groups', { name });
            if (res && (res.id || res.success)) {
                showNotification('Группа создана', 'success');
                closeAddGroupModal();
                await loadData();
            }
        } catch (err) {
            showNotification('Ошибка создания группы', 'error');
        }
    });
    
    // Закрытие модальных окон при клике вне их
    window.onclick = function(event) {
        try{
        const modals = ['zoneModal', 'groupModal'];
        modals.forEach(modalId => {
            const modal = document.getElementById(modalId);
                if (modal && event.target === modal) {
                modal.style.display = 'none';
            }
        });
            // Group-level modals (master/pressure/water) close by click on overlay too
            const master = document.querySelectorAll('[id^="modal-master-"]');
            master.forEach(m=>{ if (event.target === m) { m.style.display = 'none'; } });
            const pressure = document.querySelectorAll('[id^="modal-pressure-"]');
            pressure.forEach(m=>{ if (event.target === m) { m.style.display = 'none'; } });
            const water = document.querySelectorAll('[id^="modal-water-"]');
            water.forEach(m=>{ if (event.target === m) { m.style.display = 'none'; } });
        }catch(e){}
    }
    
    // Инициализация
    document.addEventListener('DOMContentLoaded', () => {
        loadData();
        // Подпишемся на поток статусов зон через SSE
        try {
            const es = new EventSource('/api/mqtt/zones-sse');
            es.onmessage = (ev)=>{
                try{
                    const data = JSON.parse(ev.data);
                    const idx = zonesData.findIndex(z=>z.id===data.zone_id);
                    if (idx>=0){
                        zonesData[idx].state = data.state;
                        const row = document.querySelector(`tr[data-zone-id="${data.zone_id}"]`);
                        if (row){
                            const btn = row.querySelector('.start-btn');
                            if (btn){ btn.textContent = zonesData[idx].state==='on'?'⏹️':'▶️'; }
                        }
                    }
                }catch(e){}
            };
        } catch (e) {}
    });

    // Функции для работы с чекбоксами
    function toggleSelectAll() {
        const selectAll = document.getElementById('selectAll');
        const checkboxes = document.querySelectorAll('.zone-checkbox');
        
        checkboxes.forEach(checkbox => {
            checkbox.checked = selectAll.checked;
        });
        
        updateSelectedCount();
    }
    
    function selectAllZones() {
        const checkboxes = document.querySelectorAll('.zone-checkbox');
        checkboxes.forEach(checkbox => {
            checkbox.checked = true;
        });
        document.getElementById('selectAll').checked = true;
        updateSelectedCount();
    }
    
    function deselectAllZones() {
        const checkboxes = document.querySelectorAll('.zone-checkbox');
        checkboxes.forEach(checkbox => {
            checkbox.checked = false;
        });
        document.getElementById('selectAll').checked = false;
        updateSelectedCount();
    }
    
    function updateSelectedCount() {
        const checkboxes = document.querySelectorAll('.zone-checkbox:checked');
        document.getElementById('selected-count').textContent = `Выбрано: ${checkboxes.length} зон`;
    }
    
    // Функции для работы с выпадающим списком иконок
    function toggleIconDropdown(zoneId) {
        const dropdown = document.getElementById(`icon-dropdown-${zoneId}`);
        const allDropdowns = document.querySelectorAll('.icon-dropdown-content');
        
        // Закрываем все другие выпадающие списки
        allDropdowns.forEach(d => {
            if (d !== dropdown) {
                d.classList.remove('show');
            }
        });
        
        dropdown.classList.toggle('show');
    }
    
    function selectIcon(zoneId, icon) {
        const zone = zonesData.find(z => z.id === zoneId);
        if (zone) {
            zone.icon = icon;
            updateZone(zoneId, 'icon', icon);
            
            // Обновляем отображение иконки
            const iconElement = document.querySelector(`tr[data-zone-id="${zoneId}"] .zone-icon`);
            if (iconElement) {
                iconElement.textContent = icon;
            }
            
            // Закрываем выпадающий список
            const dropdown = document.getElementById(`icon-dropdown-${zoneId}`);
            dropdown.classList.remove('show');
        }
    }
    
    // Закрытие выпадающих списков при клике вне их
    document.addEventListener('click', function(event) {
        if (!event.target.closest('.icon-dropdown')) {
            const dropdowns = document.querySelectorAll('.icon-dropdown-content');
            dropdowns.forEach(dropdown => {
                dropdown.classList.remove('show');
            });
        }
    });
    
    // Функции для работы с фотографиями
    let currentPhotoZoneId = null;
    
    function uploadPhoto(zoneId) {
        currentPhotoZoneId = zoneId;
        document.getElementById('photoInput').click();
    }
    
    async function handlePhotoUpload(event) {
        const file = event.target.files[0];
        if (!file || !currentPhotoZoneId) return;
        
        if (!file.type.startsWith('image/')) {
            showNotification('Пожалуйста, выберите изображение', 'error');
            return;
        }
        
        if (file.size > 5 * 1024 * 1024) { // 5MB limit
            showNotification('Размер файла не должен превышать 5MB', 'error');
            return;
        }
        
        const formData = new FormData();
        formData.append('photo', file);
        
        try {
            showNotification('Загрузка фотографии...', 'info');
            
            const response = await fetch(`/api/zones/${currentPhotoZoneId}/photo`, {
                method: 'POST',
                body: formData
            });
            
            if (response.ok) {
                showNotification('Фотография успешно загружена', 'success');
                await loadData(); // Перезагружаем данные
            } else {
                const error = await response.json();
                showNotification(error.message || 'Ошибка загрузки фотографии', 'error');
            }
        } catch (error) {
            console.error('Ошибка загрузки фотографии:', error);
            showNotification('Ошибка загрузки фотографии', 'error');
        }
        
        // Очищаем input
        event.target.value = '';
        currentPhotoZoneId = null;
    }
    
    async function deletePhoto(zoneId) {
        if (!confirm('Вы уверены, что хотите удалить фотографию этой зоны?')) {
            return;
        }
        
        try {
            const response = await fetch(`/api/zones/${zoneId}/photo`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                showNotification('Фотография удалена', 'success');
                await loadData(); // Перезагружаем данные
            } else {
                const error = await response.json();
                showNotification(error.message || 'Ошибка удаления фотографии', 'error');
            }
        } catch (error) {
            console.error('Ошибка удаления фотографии:', error);
            showNotification('Ошибка удаления фотографии', 'error');
        }
    }

    async function rotatePhoto(zoneId, angle){
        try{
            const r = await fetch(`/api/zones/${zoneId}/photo/rotate`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({angle})});
            const j = await r.json();
            if (j && j.success){
                showNotification('Фото повернуто', 'success');
                await loadData();
            } else {
                showNotification((j && j.message)||'Не удалось повернуть фото', 'error');
            }
        }catch(e){ showNotification('Ошибка поворота фото', 'error'); }
    }
    
    function showPhotoModal(photoUrl) {
        const modal = document.getElementById('photoModal');
        const img = document.getElementById('photoModalImg');
        img.src = photoUrl;
        modal.style.display = 'flex';
    }
    
    function closePhotoModal() {
        const modal = document.getElementById('photoModal');
        modal.style.display = 'none';
    }
    
    // Закрытие модального окна фотографии при клике вне его
    document.getElementById('photoModal').addEventListener('click', function(event) {
        if (event.target === this) {
            closePhotoModal();
        }
    });

    async function saveGroup(groupId) {
        try {
            const group = groupsData.find(g => g.id === groupId);
            if (!group) return;
            
            const row = document.querySelector(`tr[data-group-id="${groupId}"]`);
            const nameInput = row.querySelector('.group-name');
            
            const updatedGroup = {
                ...group,
                name: nameInput.value
            };
            
            const success = await api.put(`/api/groups/${groupId}`, updatedGroup);
            if (success) {
                // Обновляем данные в локальном массиве
                const groupIndex = groupsData.findIndex(g => g.id === groupId);
                if (groupIndex !== -1) {
                    groupsData[groupIndex] = { ...groupsData[groupIndex], ...updatedGroup };
                }
                
                // Убираем модификацию только для этой группы
                row.classList.remove('modified');
                modifiedGroups.delete(groupId);
                row.querySelector('.save-btn').disabled = true;
                showNotification('Группа обновлена', 'success');
                
                // Обновляем только сетку групп
                renderGroupsGrid();
            }
        } catch (error) {
            console.error('Ошибка сохранения группы:', error);
            showNotification('Ошибка сохранения группы', 'error');
        }
    }

    async function saveAllGroups() {
        try {
            if (modifiedGroups.size === 0) {
                showNotification('Нет изменений для сохранения', 'info');
                return;
            }

            showNotification('Сохранение всех изменений...', 'info');
            
            // Сохраняем все измененные группы
            const savePromises = Array.from(modifiedGroups).map(groupId => {
                const group = groupsData.find(g => g.id === groupId);
                if (!group) return Promise.resolve();
                
                const row = document.querySelector(`tr[data-group-id="${groupId}"]`);
                const nameInput = row.querySelector('.group-name');
                
                const updatedGroup = {
                    ...group,
                    name: nameInput.value
                };
                
                return api.put(`/api/groups/${groupId}`, updatedGroup);
            });
            
            const results = await Promise.all(savePromises);
            const successCount = results.filter(result => result).length;
            
            if (successCount === modifiedGroups.size) {
                // Обновляем все данные в локальном массиве
                modifiedGroups.forEach(groupId => {
                    const group = groupsData.find(g => g.id === groupId);
                    if (group) {
                        const row = document.querySelector(`tr[data-group-id="${groupId}"]`);
                        const nameInput = row.querySelector('.group-name');
                        
                        const groupIndex = groupsData.findIndex(g => g.id === groupId);
                        if (groupIndex !== -1) {
                            groupsData[groupIndex].name = nameInput.value;
                        }
                        
                        // Убираем модификацию
                        row.classList.remove('modified');
                        row.querySelector('.save-btn').disabled = true;
                    }
                });
                
                modifiedGroups.clear();
                showNotification(`Сохранено ${successCount} групп`, 'success');
                
                // Обновляем сетку групп
                renderGroupsGrid();
            } else {
                showNotification('Ошибка сохранения некоторых групп', 'error');
            }
        } catch (error) {
            console.error('Ошибка массового сохранения групп:', error);
            showNotification('Ошибка сохранения групп', 'error');
        }
    }

    async function deleteGroup(groupId) {
        if (!confirm(`Удалить группу ${groupsData.find(g => g.id === groupId)?.name || groupId}?`)) {
            return;
        }

        try {
            const resp = await fetch(`/api/groups/${groupId}`, { method: 'DELETE' });
            if (resp.status === 204) {
                showNotification('Группа удалена', 'success');
                groupsData = groupsData.filter(g => g.id !== groupId);
                modifiedGroups.delete(groupId);
                renderGroupsGrid();
                await loadData(); // Перезагружаем все данные, чтобы обновить счетчик зон
            } else {
                const error = await resp.json().catch(() => ({}));
                showNotification(error.message || 'Ошибка удаления группы', 'error');
            }
        } catch (error) {
            console.error('Ошибка удаления группы:', error);
            showNotification('Ошибка удаления группы', 'error');
        }
    }
    
    // Экспорт зон в CSV
    function exportZonesCSV() {
        if (zonesData.length === 0) {
            // Создаем шаблон, если зон нет
            const template = [
                ['id', 'name', 'icon', 'duration', 'group_id', 'state', 'topic', 'mqtt_server_id'],
                ['1', 'Зона 1', '🌿', '10', '1', 'off', '/devices/wb-mr6cv3_101/controls/K1', '1'],
                ['2', 'Зона 2', '🌳', '15', '1', 'off', '/devices/wb-mr6cv3_101/controls/K2', '1']
            ];
            
            const csv = template.map(row => row.join(',')).join('\n');
            downloadCSV(csv, 'zones_template.csv');
            showNotification('Создан шаблон CSV файла', 'info');
            return;
        }
        
        // Экспортируем существующие зоны
        const headers = ['id', 'name', 'icon', 'duration', 'group_id', 'state', 'topic', 'mqtt_server_id'];
        const csvData = [
            headers,
            ...zonesData.map(zone => [
                zone.id,
                zone.name,
                zone.icon,
                zone.duration,
                zone.group_id,
                zone.state,
                zone.topic || '',
                (zone.mqtt_server_id==null?'':zone.mqtt_server_id)
            ])
        ];
        
        const csv = csvData.map(row => row.join(',')).join('\n');
        downloadCSV(csv, `zones_export_${new Date().toISOString().slice(0, 10)}.csv`);
        showNotification(`Экспортировано ${zonesData.length} зон`, 'success');
    }
    
    // Импорт зон из CSV
    function importZonesCSV() {
        document.getElementById('csvFileInput').click();
    }
    
    // Обработка импорта CSV файла
    async function handleCSVImport(event) {
        const file = event.target.files[0];
        if (!file) return;
        
        try {
            const text = await file.text();
            const lines = text.split('\n').filter(line => line.trim());
            const headers = lines[0].split(',').map(h=>h.trim());
            
            // Проверяем заголовки
            // Для импорта обязательно только поле id. Остальные — опциональны
            if (!headers.includes('id')) {
                showNotification('Неверный формат файла. Должен быть столбец id', 'error');
                return;
            }
            
            const zonesToImport = [];
            for (let i = 1; i < lines.length; i++) {
                const values = lines[i].split(',');
                const get = (key) => {
                    const idx = headers.indexOf(key);
                    return idx >= 0 ? (values[idx] ?? '').trim() : '';
                };
                const idStr = get('id');
                if (!idStr) continue;
                const zone = { id: parseInt(idStr,10) };
                const name = get('name'); if (name) zone.name = name;
                const icon = get('icon'); if (icon) zone.icon = icon;
                const dur = get('duration'); if (dur) zone.duration = parseInt(dur,10);
                const gid = get('group_id'); if (gid) zone.group_id = parseInt(gid,10);
                const state = get('state'); if (state) zone.state = state;
                const topic = get('topic'); if (topic) zone.topic = topic;
                const mqtt = get('mqtt_server_id'); if (mqtt) zone.mqtt_server_id = parseInt(mqtt,10);
                zonesToImport.push(zone);
            }
            
            if (zonesToImport.length === 0) {
                showNotification('Файл не содержит данных для импорта', 'warning');
                return;
            }
            
            if (confirm(`Импортировать ${zonesToImport.length} зон?`)) {
                await importZones(zonesToImport);
            }
            
        } catch (error) {
            console.error('Ошибка импорта CSV:', error);
            showNotification('Ошибка чтения CSV файла', 'error');
        }
        
        // Очищаем input
        event.target.value = '';
    }
    
    // Импорт зон в базу данных
    async function importZones(zonesToImport) {
        try {
            showNotification('Импорт зон...', 'info');
            
            // Отправляем одним запросом
            const payload = { zones: zonesToImport };
            const resp = await fetch('/api/zones/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            if (resp.status === 401 || resp.status === 403) {
                const err = await resp.json().catch(() => ({}));
                const msg = err.error_code === 'PASSWORD_MUST_CHANGE'
                    ? 'Необходимо сменить пароль перед импортом'
                    : 'Для импорта требуется авторизация администратора';
                showNotification(msg, 'error');
                return;
            }
            const j = await resp.json();
            
            // Перезагружаем данные
            await loadData();
            if (j && j.success) {
                showNotification(`Импорт: создано ${j.created}, обновлено ${j.updated}, ошибок ${j.failed}`, 'success');
            } else {
                showNotification(j.message || 'Импорт завершился с ошибкой', 'error');
            }
            
        } catch (error) {
            console.error('Ошибка импорта зон:', error);
            showNotification('Ошибка импорта зон', 'error');
        }
    }
    
    // Функция для скачивания CSV файла
    function downloadCSV(csv, filename) {
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        const url = URL.createObjectURL(blob);
        link.setAttribute('href', url);
        link.setAttribute('download', filename);
        link.style.visibility = 'hidden';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }

    // === Early OFF seconds setting ===
    async function loadEarlyOff() {
        try {
            const r = await fetch('/api/settings/early-off');
            const j = await r.json();
            const el = document.getElementById('early-off-seconds');
            if (el && j && typeof j.seconds === 'number') {
                el.value = String(j.seconds);
            }
        } catch (e) {}
    }
    async function saveEarlyOff() {
        const el = document.getElementById('early-off-seconds');
        if (!el) return;
        let v = parseInt(el.value, 10);
        if (isNaN(v)) v = 3;
        if (v < 0 || v > 15) { showNotification('Допустимо только 0–15 секунд', 'warning'); return; }
        try {
            const r = await fetch('/api/settings/early-off', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({seconds: v})});
            const j = await r.json();
            if (!(j && j.success)) showNotification(j && j.message ? j.message : 'Не удалось сохранить настройку', 'error');
            else showNotification('Настройка сохранена', 'success');
        } catch (e) { showNotification('Ошибка сохранения', 'error'); }
    }

    const __zoneSaveTimers = {};
    function scheduleZoneAutoSave(zoneId){
        if (__zoneSaveTimers[zoneId]) clearTimeout(__zoneSaveTimers[zoneId]);
        __zoneSaveTimers[zoneId] = setTimeout(()=>{ saveZone(zoneId); }, 500);
    }
