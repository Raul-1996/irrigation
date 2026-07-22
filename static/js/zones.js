    let zonesData = [];
    let groupsData = [];
    let modifiedZones = new Set();
    let modifiedGroups = new Set(); // Новый набор для отслеживания модифицированных групп
    let editingGroupId = null;
    let sortColumn = -1;
    let sortDirection = 'asc';
    const zoneStateVersions = new Map();
    let loadDataGeneration = 0;
    let zoneStateResyncGeneration = 0;
    let zoneStateFeedFailed = false;

    const GROUP_HARDWARE_FIELDS = new Set([
        'use_master_valve', 'master_mqtt_server_id', 'master_mqtt_topic', 'master_mode',
        'master_close_delay_sec', 'use_pressure_sensor', 'pressure_mqtt_server_id',
        'pressure_mqtt_topic', 'pressure_unit', 'use_water_meter', 'water_mqtt_server_id',
        'water_mqtt_topic', 'water_pulse_size', 'water_base_value_m3', 'water_base_pulses'
    ]);
    const ZONE_HARDWARE_FIELDS = new Set(['group_id', 'topic', 'mqtt_server_id']);

    function responseSucceeded(response) {
        if (response === true) return true;
        if (!response || typeof response !== 'object') return false;
        return response.success !== false;
    }

    function responseMessage(response, fallback) {
        if (response && typeof response === 'object') {
            return response.message || response.error || fallback;
        }
        return fallback;
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

    async function recoverFromZoneCasConflict(response) {
        showNotification(zoneCasConflictMessage(response), 'warning');
        await loadData();
    }

    function isZoneHardwareLocked(zoneId) {
        const zone = zonesData.find(item => Number(item.id) === Number(zoneId));
        if (!zone) return false;
        const state = String(zone.state || '').toLowerCase();
        return state !== '' && state !== 'off';
    }

    function isGroupHardwareLocked(groupId) {
        return zonesData.some(zone =>
            Number(zone.group_id) === Number(groupId) && isZoneHardwareLocked(zone.id)
        );
    }

    function parseCanonicalPositiveInt(value) {
        const raw = String(value == null ? '' : value).trim();
        if (!/^[1-9]\d*$/.test(raw)) return null;
        const parsed = Number(raw);
        return Number.isSafeInteger(parsed) ? parsed : null;
    }

    function parseZoneDuration(value) {
        const parsed = parseCanonicalPositiveInt(value);
        return parsed !== null && parsed <= 240 ? parsed : null;
    }

    async function checkDurationConflicts(changes) {
        const response = await fetch('/api/zones/check-duration-conflicts-bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ changes })
        });
        const data = await response.json().catch(() => null);
        if (!response.ok || !data || data.success === false || !data.results) {
            throw new Error(responseMessage(data, 'Не удалось проверить конфликты длительности'));
        }
        const missing = changes
            .map(change => String(change.zone_id))
            .filter(zoneId => !Object.prototype.hasOwnProperty.call(data.results, zoneId));
        if (missing.length) {
            throw new Error(`Проверка конфликтов не вернула зоны: ${missing.join(', ')}`);
        }
        return data.results;
    }

    async function putGroupSettings(groupId, payload) {
        if (isGroupHardwareLocked(groupId) && Object.keys(payload || {}).some(key => GROUP_HARDWARE_FIELDS.has(key))) {
            return {
                ok: false,
                data: null,
                message: 'Остановите полив группы перед изменением аппаратных настроек',
                status: 409
            };
        }
        const response = await fetch(`/api/groups/${groupId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const contentType = response.headers.get('content-type') || '';
        let data = null;
        if (response.status !== 204) {
            data = contentType.includes('application/json')
                ? await response.json().catch(() => null)
                : await response.text().catch(() => '');
        }
        const payloadRejected = !!(
            data && typeof data === 'object' && data.success === false
        );
        const message = (data && typeof data === 'object' && (data.message || data.error))
            || (typeof data === 'string' ? data : '')
            || `HTTP ${response.status}`;
        return {
            ok: response.ok && !payloadRejected,
            data,
            message,
            status: response.status
        };
    }
    
    // Загрузка данных
    async function loadData() {
        const requestGeneration = ++loadDataGeneration;
        const versionsAtRequest = new Map(zoneStateVersions);
        try {
            const [zonesRes, groupsRes, mqttRes, rainRes, envRes] = await Promise.all([
                api.get('/api/zones'),
                api.get('/api/groups'),
                api.get('/api/mqtt/servers'),
                api.get('/api/rain'),
                api.get('/api/env')
            ]);
            if (requestGeneration !== loadDataGeneration) return false;
            if (!Array.isArray(zonesRes) || !Array.isArray(groupsRes)) {
                throw new Error('Некорректный ответ зон или групп');
            }
            if (!responseSucceeded(mqttRes) || !Array.isArray(mqttRes.servers)
                || !responseSucceeded(rainRes) || !rainRes.config
                || !responseSucceeded(envRes) || !envRes.config) {
                throw new Error('Не удалось загрузить связанные аппаратные настройки');
            }
            const currentStates = new Map(zonesData.map(zone => [Number(zone.id), zone.state]));
            zonesRes.forEach(zone => {
                const versionBefore = versionsAtRequest.get(zone.id) || 0;
                const versionNow = zoneStateVersions.get(zone.id) || 0;
                if (versionNow !== versionBefore && currentStates.has(Number(zone.id))) {
                    zone.state = currentStates.get(Number(zone.id));
                }
            });
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
            return true;
        } catch (error) {
            if (requestGeneration !== loadDataGeneration) return false;
            console.error('Ошибка загрузки данных:', error);
            showNotification(responseMessage(error, error.message || 'Ошибка загрузки данных'), 'error');
            return false;
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
            const hardwareLocked = isZoneHardwareLocked(zone.id);
            const hardwareDisabled = hardwareLocked ? 'disabled' : '';
            const hardwareTitle = hardwareLocked ? 'Остановите зону перед изменением аппаратной конфигурации' : '';
            
            row.innerHTML = `
                <td><input type="checkbox" class="zone-checkbox" value="${zone.id}" onchange="updateSelectedCount()"></td>
                <td style="color: #333 !important;">
                    <span class="zone-status-indicator ${zone.state === 'on' ? 'active' : 'inactive'}"></span>
                    ${zone.id}
                </td>
                <td>
                    <div class="icon-dropdown">
                        <span class="zone-icon" onclick="toggleIconDropdown(${zone.id})">${escapeHtml(zone.icon || '🌿')}</span>
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
                    <select class="zone-group" ${hardwareDisabled} title="${hardwareTitle}" onchange="updateZone(${zone.id}, 'group_id', this.value)">
                        ${groupsData.map(group => 
                            (group.id === 999 ? `<option value="999" ${zone.group_id == 999 ? 'selected' : ''}>БЕЗ ПОЛИВА</option>` :
                            `<option value="${group.id}" ${zone.group_id == group.id ? 'selected' : ''}>${escapeHtml(group.name)}</option>`)
                        ).join('')}
                    </select>
                </td>
                <td>
                    <input type="text" class="zone-topic" value="${escapeHtml(zone.topic || '')}" ${hardwareDisabled} title="${hardwareTitle}"
                           placeholder="zone/1" onchange="updateZone(${zone.id}, 'topic', this.value)">
                </td>
                <td>
                    <select class="zone-mqtt" ${hardwareDisabled} title="${hardwareTitle}" onchange="updateZone(${zone.id}, 'mqtt_server_id', this.value)">
                        <option value="" ${zone.mqtt_server_id == null ? 'selected' : ''}>Не выбран</option>
                        ${window.mqttServers.map(s => `<option value="${s.id}" ${String(zone.mqtt_server_id||'')===String(s.id)?'selected':''}>${escapeHtml(s.name)}</option>`).join('')}
                    </select>
                </td>
                <td>
                    <div class="zone-photo">
                        ${zone.photo_path ?
                            `<img src="/api/zones/${zone.id}/photo?variant=thumb" alt="Фото зоны ${zone.id}" onclick="showPhotoModal('/api/zones/${zone.id}/photo')">` :
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
                        <button class="delete-btn" ${hardwareDisabled} title="${hardwareTitle || 'Удалить зону'}" onclick="deleteZone(${zone.id})">🗑️</button>
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
        const currentGroupIds = new Set(
            groupsData.filter(group => group.id !== 999).map(group => Number(group.id))
        );
        const preservedModals = new Map();
        document.querySelectorAll(
            'body > [id^="modal-master-"], body > [id^="modal-pressure-"], body > [id^="modal-water-"]'
        ).forEach(modal => {
            const match = modal.id.match(/^modal-(?:master|pressure|water)-(\d+)$/);
            if (match && currentGroupIds.has(Number(match[1]))) {
                preservedModals.set(modal.id, modal);
            } else if (match) {
                modal.remove();
            }
        });
        container.innerHTML = '';
        
        groupsData
            .filter(group => group.id !== 999)
            .forEach(group => {
            const card = document.createElement('div');
            card.className = 'group-card';
            card.dataset.groupId = group.id;
            card.style.position = 'relative';
            const hardwareLocked = isGroupHardwareLocked(group.id);
            const hardwareDisabled = hardwareLocked ? 'disabled' : '';
            const hardwareTitle = hardwareLocked ? 'Остановите полив группы перед изменением аппаратной конфигурации' : '';
            card.innerHTML = `
                <button class="delete-btn" ${hardwareDisabled} title="${hardwareTitle || 'Удалить группу'}" onclick="deleteGroup(${group.id})">✖</button>
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
                        <div class="toggle-row"><input type="checkbox" class="switch group-use-mv" ${group.use_master_valve ? 'checked' : ''} ${hardwareDisabled} title="${hardwareTitle}" onchange="toggleGroupUseMaster(${group.id}, this.checked)"><label style="margin-left:6px; color:#222;">Использовать мастер клапан</label></div>
                        <div class="toggle-row"><input type="checkbox" class="switch group-use-pressure" ${group.use_pressure_sensor ? 'checked' : ''} ${hardwareDisabled} title="${hardwareTitle}" onchange="toggleGroupUsePressure(${group.id}, this.checked)"><label style="margin-left:6px; color:#222;">Использовать датчик давления</label></div>
                        <div class="toggle-row"><input type="checkbox" class="switch group-use-water" ${group.use_water_meter ? 'checked' : ''} ${hardwareDisabled} title="${hardwareTitle}" onchange="toggleGroupUseWater(${group.id}, this.checked)"><label style="margin-left:6px; color:#222;">Использовать счётчик воды</label></div>
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
                                <select id="mv-server-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" onchange="scheduleAutoSave(${group.id})">
                                    <option value="" ${group.master_mqtt_server_id == null ? 'selected' : ''}>Не выбран</option>
                                    ${(window.mqttServers||[]).map(s=>`<option value="${s.id}" ${String(s.id)===String(group.master_mqtt_server_id)?'selected':''}>${escapeHtml(s.name)}</option>`).join('')}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Топик MQTT мастер-клапана</label>
                                <input type="text" id="mv-topic-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" value="${(group.master_mqtt_topic||'').replaceAll('"','&quot;')}" placeholder="/devices/wb-mr6c_101/controls/K1" oninput="scheduleAutoSave(${group.id})" onblur="saveGroupMasterTopic(${group.id})">
                            </div>
                            <div class="form-group">
                                <label>Удержание мастера после стопа (сек)</label>
                                <input type="number" min="1" max="3600" id="mv-delay-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" value="${(group.master_close_delay_sec ?? 60)}" onblur="saveGroupMasterCloseDelay(${group.id})">
                            </div>
                            <div class="form-group">
                                <label>Режим</label>
                                <select id="mv-mode-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" onchange="saveGroupMasterMode(${group.id})">
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
                                <select id="pressure-server-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" onchange="scheduleAutoSave(${group.id})">
                                    <option value="" ${group.pressure_mqtt_server_id == null ? 'selected' : ''}>Не выбран</option>
                                    ${(window.mqttServers||[]).map(s=>`<option value="${s.id}" ${String(s.id)===String(group.pressure_mqtt_server_id)?'selected':''}>${escapeHtml(s.name)}</option>`).join('')}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Топик MQTT датчика давления</label>
                                <input type="text" id="pressure-topic-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" value="${(group.pressure_mqtt_topic||'').replaceAll('"','&quot;')}" placeholder="/devices/wb-ms_10/controls/pressure" oninput="scheduleAutoSave(${group.id})">
                            </div>
                            <div class="form-group">
                                <label>Единицы измерения</label>
                                <select id="pressure-unit-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" onchange="scheduleAutoSave(${group.id})">
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
                                <select id="water-server-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" onchange="scheduleWaterAutoSave(${group.id})">
                                    <option value="" ${group.water_mqtt_server_id == null ? 'selected' : ''}>Не выбран</option>
                                    ${(window.mqttServers||[]).map(s=>`<option value="${s.id}" ${String(s.id)===String(group.water_mqtt_server_id)?'selected':''}>${escapeHtml(s.name)}</option>`).join('')}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Топик MQTT счётчика воды</label>
                                <input type="text" id="water-topic-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" value="${(group.water_mqtt_topic||'').replaceAll('"','&quot;')}" placeholder="/devices/wb-water/controls/meter" oninput="scheduleWaterAutoSave(${group.id})">
                            </div>
                            <div class="form-group">
                                <label>Величина импульса</label>
                                <select id="water-pulse-${group.id}" ${hardwareDisabled} title="${hardwareTitle}" onchange="scheduleWaterAutoSave(${group.id})">
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
                                        <button type="button" class="btn-small" ${hardwareDisabled} title="${hardwareTitle}" onclick="waterIncDigit(${group.id}, ${i}, 1)">+</button>
                                        <div class="digit" id="water-digit-${group.id}-${i}">0</div>
                                        <button type="button" class="btn-small" ${hardwareDisabled} title="${hardwareTitle}" onclick="waterIncDigit(${group.id}, ${i}, -1)">−</button>
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
        preservedModals.forEach((modal, id) => {
            const replacement = container.querySelector(`[id="${id}"]`);
            if (replacement) replacement.remove();
        });
    }

    function markGroupModified(groupId) {
        modifiedGroups.add(groupId);
    }

    function updateSaveAllButton() {}
    
    // Загрузка селекторов групп
    function loadGroupSelectors() {
        const nullOption = '<option value="">Не выбран</option>';
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
            rs.innerHTML = nullOption + (window.mqttServers||[]).map(s=>`<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
        }
        // Env servers
        const ets = document.getElementById('env-temp-server');
        if (ets) ets.innerHTML = nullOption + (window.mqttServers||[]).map(s=>`<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
        const ehs = document.getElementById('env-hum-server');
        if (ehs) ehs.innerHTML = nullOption + (window.mqttServers||[]).map(s=>`<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
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
                const result = await putGroupSettings(groupId, { name: value });
                if (result.ok) {
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
            if (rs) rs.value = cfg.server_id == null ? '' : String(cfg.server_id);
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
            if (resp.ok && data && data.success && data.config && Array.isArray(data.groups)) {
                // The API applies global policy to every group.  Its returned
                // snapshot is authoritative; never infer per-group flags from
                // the requested global toggle.
                window.rainConfig = data.config;
                const rainFlags = new Map(
                    data.groups.map(group => [Number(group.id), group.use_rain_sensor])
                );
                groupsData.forEach(group => {
                    if (!rainFlags.has(Number(group.id))) return;
                    const rawFlag = rainFlags.get(Number(group.id));
                    group.use_rain_sensor = rawFlag === true || rawFlag === 1
                        || String(rawFlag).trim().toLowerCase() === 'true'
                        || String(rawFlag).trim() === '1';
                });
                initRainUi();
                renderGroupsGrid();
                try{ updateGlobalToggleTitles(); }catch(e){}
                showNotification('Конфигурация датчика дождя сохранена', 'success');
            } else {
                initRainUi();
                showNotification((data && (data.message || data.error)) || 'Не удалось сохранить конфигурацию', 'error');
            }
        } catch (e) {
            try{ initRainUi(); }catch(_e){}
            showNotification('Ошибка сохранения конфигурации', 'error');
        }
    }

    function initEnvUi() {
        try {
            const cfg = window.envConfig || { temp:{enabled:false}, hum:{enabled:false} };
            const tempServerId = cfg.temp ? cfg.temp.server_id : null;
            const humServerId = cfg.hum ? cfg.hum.server_id : null;
            document.getElementById('env-temp-enabled').checked = !!(cfg.temp && cfg.temp.enabled);
            document.getElementById('env-temp-topic').value = (cfg.temp && cfg.temp.topic) || '';
            document.getElementById('env-temp-server').value = tempServerId == null ? '' : String(tempServerId);
            document.getElementById('env-hum-enabled').checked = !!(cfg.hum && cfg.hum.enabled);
            document.getElementById('env-hum-topic').value = (cfg.hum && cfg.hum.topic) || '';
            document.getElementById('env-hum-server').value = humServerId == null ? '' : String(humServerId);
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
            if (serverEl) payload.master_mqtt_server_id = serverEl.value ? parseInt(serverEl.value) : null;
            const result = await putGroupSettings(groupId, payload);
            if (!result.ok) {
                showNotification(result.message || 'Не удалось сохранить настройки мастер-клапана', 'error');
            } else {
                // update local cache
                const gi = groupsData.findIndex(g=>g.id===groupId);
                if (gi>=0) {
                    groupsData[gi].use_master_valve = !!enabled;
                    if (topic) groupsData[gi].master_mqtt_topic = topic;
                    if (modeEl && modeEl.value) groupsData[gi].master_mode = modeEl.value;
                    if (serverEl) groupsData[gi].master_mqtt_server_id = serverEl.value ? parseInt(serverEl.value) : null;
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
            const result = await putGroupSettings(groupId, { use_pressure_sensor: !!enabled });
            if (result.ok){
                const gi = groupsData.findIndex(g=>g.id===groupId);
                if (gi>=0) groupsData[gi].use_pressure_sensor = !!enabled;
                showNotification('Настройка датчика давления сохранена', 'success');
            } else {
                showNotification(result.message || 'Не удалось сохранить настройку давления', 'error');
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
            const result = await putGroupSettings(groupId, { use_water_meter: !!enabled });
            if (result.ok){
                const gi = groupsData.findIndex(g=>g.id===groupId);
                if (gi>=0) groupsData[gi].use_water_meter = !!enabled;
                showNotification('Настройка счётчика воды сохранена', 'success');
            } else {
                showNotification(result.message || 'Не удалось сохранить настройку счётчика воды', 'error');
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
    function closeModalById(id){
        const m = document.getElementById(id); if (!m) return;
        m.style.display='none';
        const match = id.match(/^modal-(?:master|pressure|water)-(\d+)$/);
        if (match) {
            const card = document.querySelector(`.group-card[data-group-id="${match[1]}"]`);
            if (card) card.appendChild(m);
        }
    }
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
    const __waterClosing = {};
    function openWaterSettings(groupId){
        openModalById(`modal-water-${groupId}`);
        try { waterStartLive(groupId); } catch(e){}
    }
    async function closeWaterSettings(groupId){
        __waterClosing[groupId] = true;
        try { waterStopLive(groupId); } catch(e){}
        try {
            const saved = await waterFlushSave(groupId, {restartLive: false});
            if (saved === false && __waterState[groupId]?.editing) return;
        } catch(e){}
        finally { delete __waterClosing[groupId]; }
        if (!__waterState[groupId]?.editing) closeModalById(`modal-water-${groupId}`);
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
                const result = await putGroupSettings(groupId, payload);
                if (result.ok){ if (badge){ badge.textContent='Сохранено'; badge.className='save-badge saved'; setTimeout(()=>{ if (badge) { badge.textContent=''; badge.className='save-badge'; } }, 1200); } }
                else { if (badge){ badge.textContent='Ошибка'; badge.className='save-badge error'; } showNotification(result.message || 'Не удалось сохранить настройки группы', 'error'); }
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
        scheduleWaterAutoSave(groupId);
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
    const __waterLiveGenerations = {};
    async function waterProbeOnce(groupId, options = {}){
        const generation = options.generation;
        const isCurrent = () => generation == null || __waterLiveGenerations[groupId] === generation;
        try{
            const serverEl = document.getElementById(`water-server-${groupId}`);
            const topicEl = document.getElementById(`water-topic-${groupId}`);
            const sid = serverEl && serverEl.value ? parseInt(serverEl.value) : null;
            const topic = topicEl && topicEl.value ? topicEl.value.trim() : '';
            if (!sid || !topic){
                if (!options.silent) showNotification('Укажите MQTT сервер и MQTT-топик счётчика воды', 'warning');
                return false;
            }
            const res = await fetch(`/api/mqtt/${sid}/probe`, {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({ filter: topic, duration: 1.5 }),
                signal: options.signal
            });
            const data = await res.json().catch(() => null);
            if (!res.ok || !data || data.success === false) throw new Error(responseMessage(data, `HTTP ${res.status}`));
            if (!isCurrent()) return false;
            const item = (data && data.items || []).find(it=> it.topic===topic);
            if (!item){
                if (!options.silent) showNotification(`Нет данных по топику: ${topic}`, 'warning');
                return false;
            }
            const pulses = parseInt((item.payload||'').replace(/[^0-9-]/g,''))||0;
            const pulsesEl = document.getElementById(`water-pulses-${groupId}`);
            if (pulsesEl) pulsesEl.value = String(pulses);
            __waterState[groupId] = __waterState[groupId] || {};
            __waterState[groupId].currentPulses = pulses;
            if (!__waterState[groupId].editing){
                const val = waterCurrentFromPulses(groupId);
                waterSetDigits(groupId, val);
            }
            return true;
        }catch(e){
            if (e && e.name === 'AbortError') return false;
            if (isCurrent() && !options.silent) showNotification('Ошибка запроса MQTT', 'error');
            return false;
        }
    }
    async function waterCancel(groupId){
        if (__waterSaveTimers[groupId]) {
            clearTimeout(__waterSaveTimers[groupId]);
            delete __waterSaveTimers[groupId];
        }
        const state = __waterState[groupId] = __waterState[groupId] || {};
        const baseValueM3 = Number(state.baseValueM3 || 0);
        const basePulses = Number(state.basePulses || 0);
        const hadInFlightSave = Boolean(__waterSaveInFlight[groupId]);
        const hadPendingRestore = Boolean(__waterCalibrationOverrides[groupId]);
        const revision = (__waterSaveRevisions[groupId] || 0) + 1;
        __waterSaveRevisions[groupId] = revision;
        if (hadInFlightSave || hadPendingRestore) {
            // The old request may already be on the wire.  Invalidate its UI
            // result and serialize an explicit compensating write after it.
            __waterCalibrationOverrides[groupId] = {revision, baseValueM3, basePulses};
        } else {
            delete __waterCalibrationOverrides[groupId];
        }
        state.editing = false;
        waterStopLive(groupId);
        const actions = document.getElementById(`water-actions-${groupId}`);
        if (actions) actions.style.display = 'none';
        waterSetDigits(groupId, baseValueM3);
        let restored = true;
        if (hadInFlightSave || hadPendingRestore) {
            restored = await waterFlushSave(groupId, {restartLive: false});
            if (!restored) {
                state.editing = true;
                if (actions) actions.style.display = 'flex';
            }
        }
        if (waterModalIsOpen(groupId)) waterStartLive(groupId);
        return restored;
    }
    const __waterSaveTimers = {};
    const __waterSaveRevisions = {};
    const __waterSaveInFlight = {};
    const __waterCalibrationOverrides = {};
    function scheduleWaterAutoSave(groupId){
        // A response for the previous topic/pulse snapshot must never update
        // the calibration UI after the user has edited it.
        waterStopLive(groupId);
        delete __waterCalibrationOverrides[groupId];
        __waterSaveRevisions[groupId] = (__waterSaveRevisions[groupId] || 0) + 1;
        if (__waterSaveTimers[groupId]) clearTimeout(__waterSaveTimers[groupId]);
        __waterSaveTimers[groupId] = setTimeout(()=>{ waterFlushSave(groupId); }, 600);
    }
    async function performWaterSave(groupId, revision){
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
            const state = __waterState[groupId] || {};
            const calibrationOverride = __waterCalibrationOverrides[groupId];
            const hasCalibrationOverride = Boolean(
                calibrationOverride && calibrationOverride.revision === revision
            );
            let newValue = null;
            let currentPulses = null;
            const editing = state.editing === true || hasCalibrationOverride;
            if (editing) {
                if (hasCalibrationOverride) {
                    newValue = Number(calibrationOverride.baseValueM3 || 0);
                    currentPulses = Number(calibrationOverride.basePulses || 0);
                } else {
                    newValue = waterDigitsToValue(groupId);
                    const pulsesEl = document.getElementById(`water-pulses-${groupId}`);
                    const rawPulses = pulsesEl ? String(pulsesEl.value || '').trim() : '';
                    currentPulses = rawPulses
                        ? (parseInt(rawPulses, 10) || 0)
                        : Number(state.currentPulses ?? state.basePulses ?? 0);
                }
                payload.water_base_value_m3 = newValue;
                payload.water_base_pulses = currentPulses;
            }
            const result = await putGroupSettings(groupId, payload);
            if (!result || !result.ok) {
                if ((__waterSaveRevisions[groupId] || 0) === revision) {
                    showNotification((result && result.message) || 'Не удалось сохранить настройки счётчика воды', 'error');
                }
                return false;
            }
            if (editing && (__waterSaveRevisions[groupId] || 0) === revision) {
                state.baseValueM3 = newValue;
                state.basePulses = currentPulses;
                state.editing = false;
                if (hasCalibrationOverride) delete __waterCalibrationOverrides[groupId];
                const actions = document.getElementById(`water-actions-${groupId}`);
                if (actions) actions.style.display = 'none';
            }
            const group = groupsData.find(item => Number(item.id) === Number(groupId));
            if (group && (__waterSaveRevisions[groupId] || 0) === revision) Object.assign(group, payload);
            return true;
        }catch(e){
            if ((__waterSaveRevisions[groupId] || 0) === revision) {
                showNotification('Ошибка сохранения настроек счётчика воды', 'error');
            }
            return false;
        }
    }
    async function waterFlushSave(groupId, options = {}){
        const restartLive = options.restartLive !== false;
        if (__waterSaveTimers[groupId]) {
            clearTimeout(__waterSaveTimers[groupId]);
            delete __waterSaveTimers[groupId];
        }
        const revision = __waterSaveRevisions[groupId] || 0;
        const active = __waterSaveInFlight[groupId];
        if (active) {
            const activeResult = await active.promise;
            if ((__waterSaveRevisions[groupId] || 0) !== active.revision) {
                return waterFlushSave(groupId, options);
            }
            if (activeResult && restartLive && !__waterClosing[groupId] && waterModalIsOpen(groupId)) {
                waterStartLive(groupId);
            }
            return activeResult;
        }
        const entry = {revision: revision, promise: performWaterSave(groupId, revision)};
        __waterSaveInFlight[groupId] = entry;
        let result;
        try {
            result = await entry.promise;
        } finally {
            if (__waterSaveInFlight[groupId] === entry) delete __waterSaveInFlight[groupId];
        }
        if ((__waterSaveRevisions[groupId] || 0) !== revision) {
            return waterFlushSave(groupId, options);
        }
        if (result && restartLive && !__waterClosing[groupId] && waterModalIsOpen(groupId)) {
            waterStartLive(groupId);
        }
        return result;
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
    const __waterLiveControllers = {};
    const __waterLiveInFlight = {};
    function waterModalIsOpen(groupId){
        const modal = document.getElementById(`modal-water-${groupId}`);
        return !!modal && modal.style.display !== 'none';
    }
    function waterStartLive(groupId){
        waterStopLive(groupId);
        const generation = (__waterLiveGenerations[groupId] || 0) + 1;
        __waterLiveGenerations[groupId] = generation;
        const scheduleNext = () => {
            if (__waterLiveGenerations[groupId] !== generation) return;
            __waterLiveTimers[groupId] = setTimeout(run, 3000);
        };
        const run = async ()=>{
            if (__waterLiveGenerations[groupId] !== generation) return;
            if (__waterLiveInFlight[groupId] || __waterState[groupId]?.editing) {
                scheduleNext();
                return;
            }
            const controller = new AbortController();
            __waterLiveControllers[groupId] = controller;
            __waterLiveInFlight[groupId] = true;
            try {
                await waterProbeOnce(groupId, {
                    generation,
                    signal: controller.signal,
                    silent: true
                });
            } finally {
                if (__waterLiveGenerations[groupId] === generation) {
                    __waterLiveInFlight[groupId] = false;
                    delete __waterLiveControllers[groupId];
                    scheduleNext();
                }
            }
        };
        run();
    }
    function waterStopLive(groupId){
        __waterLiveGenerations[groupId] = (__waterLiveGenerations[groupId] || 0) + 1;
        if (__waterLiveTimers[groupId]){ clearTimeout(__waterLiveTimers[groupId]); delete __waterLiveTimers[groupId]; }
        if (__waterLiveControllers[groupId]) {
            __waterLiveControllers[groupId].abort();
            delete __waterLiveControllers[groupId];
        }
        delete __waterLiveInFlight[groupId];
    }

    async function saveGroupMasterTopic(groupId) {
        try {
            const topicEl = document.getElementById(`mv-topic-${groupId}`);
            const modeEl = document.getElementById(`mv-mode-${groupId}`);
            const topic = (topicEl && topicEl.value ? topicEl.value.trim() : '');
            if (topicEl) topicEl.style.border = '';
            const payload = { master_mqtt_topic: topic };
            if (modeEl && modeEl.value) payload.master_mode = modeEl.value;
            const result = await putGroupSettings(groupId, payload);
            if (!result.ok) {
                showNotification(result.message || 'Не удалось сохранить топик мастер-клапана', 'error');
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
            const result = await putGroupSettings(groupId, payload);
            if (!result.ok) {
                showNotification(result.message || 'Не удалось сохранить режим мастер-клапана', 'error');
            } else {
                const gi = groupsData.findIndex(g=>g.id===groupId);
                if (gi>=0) { groupsData[gi].master_mode = mode; }
                showNotification('Режим мастер-клапана сохранён', 'success');
            }
        } catch (e) {
            showNotification('Ошибка сохранения режима мастер-клапана', 'error');
        }
    }

    async function saveGroupMasterCloseDelay(groupId) {
        try {
            const el = document.getElementById(`mv-delay-${groupId}`);
            if (!el) return;
            let v = parseInt(el.value, 10);
            if (isNaN(v)) v = 60;
            if (v < 1) v = 1;
            if (v > 3600) v = 3600;
            el.value = v;
            const result = await putGroupSettings(groupId, { master_close_delay_sec: v });
            if (!result.ok) {
                showNotification(result.message || 'Не удалось сохранить задержку закрытия мастера', 'error');
            } else {
                const gi = groupsData.findIndex(g=>g.id===groupId);
                if (gi>=0) { groupsData[gi].master_close_delay_sec = v; }
                showNotification('Задержка закрытия мастера сохранена', 'success');
            }
        } catch (e) {
            showNotification('Ошибка сохранения задержки мастер-клапана', 'error');
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
            const result = await putGroupSettings(groupId, { name: groupsData.find(g=>g.id===groupId).name, use_rain_sensor: !!enabled });
            if (result.ok) {
                const gi = groupsData.findIndex(g=>g.id===groupId);
                if (gi>=0) groupsData[gi].use_rain_sensor = !!enabled;
                showNotification('Настройка датчика дождя для группы сохранена', 'success');
            } else {
                showNotification(result.message || 'Не удалось сохранить настройку датчика дождя', 'error');
            }
        } catch (e) {
            showNotification('Ошибка сохранения настройки группы', 'error');
        }
    }
    
    // Обновление зоны (отметка как измененной)
    function updateZone(zoneId, field, value) {
        if (isZoneHardwareLocked(zoneId) && ZONE_HARDWARE_FIELDS.has(field)) {
            showNotification('Остановите зону перед изменением аппаратной конфигурации', 'warning');
            renderZonesTable();
            return;
        }
        const row = document.querySelector(`tr[data-zone-id="${zoneId}"]`);
        if (row) {
            row.classList.add('modified');
            modifiedZones.add(zoneId);
            __zoneEditRevisions[zoneId] = (__zoneEditRevisions[zoneId] || 0) + 1;
            
            // Автосохранение через debounce
            scheduleZoneAutoSave(zoneId);
        }
    }
    
    // Сохранение зоны
    async function saveZone(zoneId) {
        try {
            const zone = zonesData.find(z => z.id === zoneId);
            if (!zone) return false;
            const saveRevision = __zoneEditRevisions[zoneId] || 0;
            
            const row = document.querySelector(`tr[data-zone-id="${zoneId}"]`);
            const nameInput = row.querySelector('.zone-name');
            const durationInput = row.querySelector('.zone-duration');
            const groupSelect = row.querySelector('.zone-group');
            const topicInput = row.querySelector('.zone-topic');
            const mqttSelect = row.querySelector('.zone-mqtt');
            
            const payload = {
                name: nameInput.value,
                duration: parseZoneDuration(durationInput.value),
                group_id: parseInt(groupSelect.value),
                icon: zone.icon
            };
            if (payload.duration === null) {
                showNotification('Длительность должна быть целым числом от 1 до 240 минут', 'error');
                durationInput.value = zone.duration;
                return false;
            }
            if (topicInput) {
                payload.topic = topicInput.value;
            }
            if (mqttSelect) {
                const val = mqttSelect.value;
                payload.mqtt_server_id = val === '' ? null : parseInt(val);
            }

            const hardwareChanged = Number(zone.group_id) !== Number(payload.group_id)
                || String(zone.topic || '') !== String(payload.topic || '')
                || Number(zone.mqtt_server_id || 0) !== Number(payload.mqtt_server_id || 0);
            if (hardwareChanged && isZoneHardwareLocked(zoneId)) {
                showNotification('Остановите зону перед изменением аппаратной конфигурации', 'warning');
                renderZonesTable();
                return false;
            }

            // Conflict service is required only when duration actually changes.
            if (Number(zone.duration) !== Number(payload.duration)) {
                let results;
                try {
                    results = await checkDurationConflicts([{ zone_id: zoneId, new_duration: payload.duration }]);
                } catch (err) {
                    showNotification(err.message || 'Не удалось проверить конфликты. Изменение не сохранено.', 'error');
                    return false;
                }
                const zres = results[String(zoneId)];
                if (zres && zres.has_conflicts) {
                    showDurationConflictModal(zres.conflicts);
                    showNotification('Обнаружены конфликты программ. Изменение не сохранено.', 'warning');
                    return false;
                }
            }

            if (!Number.isInteger(zone.version) || zone.version < 0) {
                modifiedZones.delete(zoneId);
                await recoverFromZoneCasConflict({ error_code: 'EXPECTED_VERSION_REQUIRED' });
                cancelZoneAutoSave(zoneId);
                return false;
            }

            const requestPayload = { ...payload, expected_version: zone.version };
            const result = await api.put(`/api/zones/${zoneId}`, requestPayload);
            if (isZoneCasConflict(result)) {
                modifiedZones.delete(zoneId);
                await recoverFromZoneCasConflict(result);
                cancelZoneAutoSave(zoneId);
                return false;
            }
            if (result && result.success === false) {
                showNotification(result.message || 'Ошибка сохранения зоны', 'error');
                return false;
            }
            if (!result || typeof result !== 'object' || !Number.isInteger(result.version)) {
                showNotification('Сервер не вернул новую версию зоны. Загружены актуальные данные.', 'error');
                modifiedZones.delete(zoneId);
                await loadData();
                cancelZoneAutoSave(zoneId);
                return false;
            }
            const zoneIndex = zonesData.findIndex(z => z.id === zoneId);
            if (zoneIndex !== -1) {
                zonesData[zoneIndex] = {
                    ...zonesData[zoneIndex],
                    ...payload,
                    version: result.version
                };
            }
            const hasNewerEdit = (__zoneEditRevisions[zoneId] || 0) !== saveRevision;
            if (hasNewerEdit) {
                __zoneSavePending[zoneId] = true;
            } else {
                row.classList.remove('modified');
                modifiedZones.delete(zoneId);
                showNotification('Зона сохранена', 'success');
            }
            renderGroupsGrid();
            return true;
        } catch (error) {
            showNotification('Ошибка автосохранения зоны', 'error');
            return false;
        }
    }
    
    // Удаление зоны
    async function deleteZone(zoneId) {
        if (isZoneHardwareLocked(zoneId)) {
            showNotification('Остановите зону перед удалением', 'warning');
            return;
        }
        if (!confirm(`Удалить зону ${zoneId}?`)) return;
        
        try {
            const response = await fetch(`/api/zones/${zoneId}`, { method: 'DELETE' });
            if (!response.ok) {
                const contentType = response.headers.get('content-type') || '';
                const error = contentType.includes('application/json')
                    ? await response.json().catch(() => null)
                    : await response.text().catch(() => '');
                const message = (error && typeof error === 'object' && (error.message || error.error))
                    || (typeof error === 'string' ? error : '')
                    || `Ошибка удаления зоны (HTTP ${response.status})`;
                showNotification(message, 'error');
                return;
            }

            if (response.status !== 204) {
                const result = await response.json().catch(() => null);
                if (result && result.success === false) {
                    showNotification(responseMessage(result, 'Ошибка удаления зоны'), 'error');
                    return;
                }
            }

            // A successful DELETE intentionally returns 204 with an empty body.
            zonesData = zonesData.filter(z => z.id !== zoneId);
            modifiedZones.delete(zoneId);
            showNotification('Зона удалена', 'success');
            renderZonesTable();
            renderGroupsGrid();
            updateZonesCount();
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
            
            // Reserve a version before the request so a later SSE event (or a
            // second control request) remains authoritative over this reply.
            const stateVersionAtRequest = (zoneStateVersions.get(zoneId) || 0) + 1;
            zoneStateVersions.set(zoneId, stateVersionAtRequest);

            // MQTT: публикуем '1' и ждём подтверждения через zones-sse
            const response = await api.post(`/api/zones/${zoneId}/mqtt/start`);
            
            if (responseSucceeded(response)) {
                showNotification(`Зона ${zoneId} запущена`, 'success');

                if ((zoneStateVersions.get(zoneId) || 0) === stateVersionAtRequest) {
                    applyZoneState(zoneId, 'on', false);
                    renderZonesTable();
                    renderGroupsGrid();
                }
                try {
                    await resyncZoneStates();
                } catch (error) {
                    console.warn('Не удалось обновить версию зоны после запуска:', error);
                }
                
                // Обновляем статус на странице статуса
                if (window.location.pathname === '/') {
                    loadStatusData();
                }
            } else {
                showNotification(responseMessage(response, 'Ошибка запуска зоны'), 'error');
            }
        } catch (error) {
            console.error('Ошибка запуска зоны:', error);
            showNotification('Ошибка запуска зоны', 'error');
        }
    }
    
    // Остановка зоны
    async function stopZone(zoneId) {
        try {
            const stateVersionAtRequest = (zoneStateVersions.get(zoneId) || 0) + 1;
            zoneStateVersions.set(zoneId, stateVersionAtRequest);
            const response = await api.post(`/api/zones/${zoneId}/mqtt/stop`);
            
            if (responseSucceeded(response)) {
                showNotification(`Зона ${zoneId} остановлена`, 'success');

                if ((zoneStateVersions.get(zoneId) || 0) === stateVersionAtRequest) {
                    applyZoneState(zoneId, 'off', false);
                    renderZonesTable();
                    renderGroupsGrid();
                }
                try {
                    await resyncZoneStates();
                } catch (error) {
                    console.warn('Не удалось обновить версию зоны после остановки:', error);
                }
                
                // Обновляем статус на странице статуса
                if (window.location.pathname === '/') {
                    loadStatusData();
                }
            } else {
                showNotification(responseMessage(response, 'Ошибка остановки зоны'), 'error');
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
        
        const selectedZones = Array.from(document.querySelectorAll('.zone-checkbox:checked'))
            .map(cb => parseCanonicalPositiveInt(cb.value))
            .filter(zoneId => zoneId !== null);
        if (selectedZones.length === 0) {
            showNotification('Выберите зоны для изменения', 'warning');
            return;
        }
        
        try {
            let value = null;
            if (action === 'group') {
                value = parseCanonicalPositiveInt(document.getElementById('bulkGroup').value);
                if (value === null) {
                    showNotification('Выберите корректную группу', 'error');
                    return;
                }
            } else if (action === 'icon') {
                value = document.getElementById('bulkIcon').value;
                if (!value) {
                    showNotification('Выберите иконку', 'error');
                    return;
                }
            } else if (action === 'duration') {
                value = parseZoneDuration(document.getElementById('bulkDuration').value);
                if (value === null) {
                    showNotification('Длительность должна быть целым числом от 1 до 240 минут', 'error');
                    return;
                }
            } else if (action === 'mqtt') {
                value = parseCanonicalPositiveInt(document.getElementById('bulkMqtt').value);
                if (value === null) {
                    showNotification('Выберите корректный MQTT сервер', 'error');
                    return;
                }
            }

            if ((action === 'group' || action === 'mqtt' || action === 'delete')
                && selectedZones.some(isZoneHardwareLocked)) {
                showNotification('Остановите выбранные активные зоны перед изменением аппаратной конфигурации', 'warning');
                return;
            }

            // Destructive bulk actions require one explicit confirmation and
            // report the truth of every HTTP response instead of assuming success.
            if (action === 'delete' || action === 'delphoto') {
                const noun = action === 'delete' ? 'зоны' : 'фотографии зон';
                if (!confirm(`Удалить ${noun}: ${selectedZones.length}?`)) return;
                const failedIds = [];
                let successCount = 0;
                for (const zoneId of selectedZones) {
                    const url = action === 'delete'
                        ? `/api/zones/${zoneId}`
                        : `/api/zones/${zoneId}/photo`;
                    try {
                        const response = await fetch(url, { method: 'DELETE' });
                        const result = response.status === 204
                            ? null
                            : await response.json().catch(() => null);
                        if (!response.ok || (result && result.success === false)) {
                            failedIds.push(zoneId);
                        } else {
                            successCount += 1;
                        }
                    } catch (_) {
                        failedIds.push(zoneId);
                    }
                }
                if (successCount) {
                    showNotification(`Изменения применены к ${successCount} зонам`, failedIds.length ? 'warning' : 'success');
                    await loadData();
                }
                if (failedIds.length) {
                    showNotification(`Не удалось обработать зоны: ${failedIds.join(', ')}`, 'error');
                }
                return;
            }

            // A duration write is unsafe unless every selected zone received a
            // complete, successful conflict-check result.
            if (action === 'duration') {
                const changes = selectedZones.map(zoneId => ({ zone_id: zoneId, new_duration: value }));
                let results;
                try {
                    results = await checkDurationConflicts(changes);
                } catch (error) {
                    showNotification(error.message || 'Не удалось проверить конфликты. Изменения не применены.', 'error');
                    return;
                }
                const conflicted = selectedZones.filter(zoneId => results[String(zoneId)].has_conflicts);
                const okIds = selectedZones.filter(zoneId => !results[String(zoneId)].has_conflicts);
                if (conflicted.length) {
                    showNotification(`Конфликты у зон: ${conflicted.join(', ')} — они пропущены`, 'warning');
                }
                if (!okIds.length) return;
                const zonesPayload = okIds.map(id => ({ id, duration: value }));
                const response = await fetch('/api/zones/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ zones: zonesPayload })
                });
                const result = await response.json().catch(() => null);
                if (!response.ok || !responseSucceeded(result)) {
                    showNotification(responseMessage(result, 'Ошибка применения изменений'), 'error');
                    return;
                }
                showNotification(
                    `Обновлено ${result.updated || 0}, создано ${result.created || 0}, ошибок ${result.failed || 0}`,
                    result.failed ? 'warning' : 'success'
                );
                await loadData();
                return;
            }

            let zonesPayload = [];
            if (action === 'group') zonesPayload = selectedZones.map(id => ({ id, group_id: value }));
            else if (action === 'icon') zonesPayload = selectedZones.map(id => ({ id, icon: value }));
            else if (action === 'mqtt') zonesPayload = selectedZones.map(id => ({ id, mqtt_server_id: value }));
            if (zonesPayload.length) {
                const response = await fetch('/api/zones/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ zones: zonesPayload })
                });
                const result = await response.json().catch(() => null);
                if (!response.ok || !responseSucceeded(result)) {
                    showNotification(responseMessage(result, 'Ошибка применения изменений'), 'error');
                    return;
                }
                showNotification(
                    `Обновлено ${result.updated || 0}, создано ${result.created || 0}, ошибок ${result.failed || 0}`,
                    result.failed ? 'warning' : 'success'
                );
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
    async function createZone(e) {
        e.preventDefault();
        const duration = parseZoneDuration(document.getElementById('zoneDuration').value);
        const groupId = parseCanonicalPositiveInt(document.getElementById('zoneGroup').value);
        if (duration === null || groupId === null) {
            showNotification('Проверьте длительность и группу зоны', 'error');
            return;
        }
        const zoneData = {
            name: document.getElementById('zoneName').value,
            icon: document.getElementById('zoneIcon').value,
            duration,
            group_id: groupId,
            topic: document.getElementById('zoneTopic').value,
            mqtt_server_id: (window.mqttServers||[]).length === 1 ? ((window.mqttServers[0] && window.mqttServers[0].id) || null) : undefined
        };
        
        try {
            const result = await api.post('/api/zones', zoneData);
            if (responseSucceeded(result)) {
                showNotification('Зона создана', 'success');
                closeZoneModal();
                await loadData();
            } else {
                showNotification(responseMessage(result, 'Ошибка создания зоны'), 'error');
            }
        } catch (error) {
            console.error('Ошибка создания зоны:', error);
            showNotification('Ошибка создания зоны', 'error');
        }
    }
    document.getElementById('zoneForm').addEventListener('submit', createZone);
    
    document.getElementById('groupForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const groupName = document.getElementById('groupName').value;
        
        try {
            if (editingGroupId) {
                const result = await api.put(`/api/groups/${editingGroupId}`, { name: groupName });
                if (responseSucceeded(result)) {
                    showNotification('Группа обновлена', 'success');
                } else {
                    showNotification(responseMessage(result, 'Ошибка обновления группы'), 'error');
                    return;
                }
            } else {
                const result = await api.post('/api/groups', { name: groupName });
                if (responseSucceeded(result)) {
                    showNotification('Группа создана', 'success');
                } else {
                    showNotification(responseMessage(result, 'Ошибка создания группы'), 'error');
                    return;
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
            if (responseSucceeded(res) && res && (res.id || res.success)) {
                showNotification('Группа создана', 'success');
                closeAddGroupModal();
                await loadData();
            } else {
                showNotification(responseMessage(res, 'Ошибка создания группы'), 'error');
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
    
    function applyZoneState(zoneId, state, fromEvent) {
        if (fromEvent) {
            zoneStateVersions.set(zoneId, (zoneStateVersions.get(zoneId) || 0) + 1);
        }
        const idx = zonesData.findIndex(zone => zone.id === zoneId);
        if (idx < 0) return;
        zonesData[idx].state = state;
        const row = document.querySelector(`tr[data-zone-id="${zoneId}"]`);
        if (!row) return;
        const button = row.querySelector('.start-btn');
        if (button) button.textContent = state === 'on' ? '⏹️' : '▶️';
        const indicator = row.querySelector('.zone-status-indicator');
        if (indicator) {
            indicator.classList.toggle('active', state === 'on');
            indicator.classList.toggle('inactive', state !== 'on');
        }
    }

    function canApplyResyncedZoneVersion(zoneId, incomingVersion) {
        if (!Number.isInteger(incomingVersion)) return false;
        if (modifiedZones.has(zoneId)
            || __zoneSaveInFlight[zoneId]
            || __zoneSavePending[zoneId]) {
            return false;
        }
        const localZone = zonesData.find(item => item.id === zoneId);
        return !localZone
            || !Number.isInteger(localZone.version)
            || incomingVersion >= localZone.version;
    }

    async function resyncZoneStates() {
        const requestGeneration = ++zoneStateResyncGeneration;
        const versionsAtRequest = new Map(zoneStateVersions);
        const snapshot = await api.get('/api/zones');
        if (requestGeneration !== zoneStateResyncGeneration) return;
        if (!Array.isArray(snapshot)) throw new Error('Некорректный ответ состояний зон');
        snapshot.forEach(zone => {
            const versionBefore = versionsAtRequest.get(zone.id) || 0;
            const versionNow = zoneStateVersions.get(zone.id) || 0;
            if (versionNow === versionBefore) {
                applyZoneState(zone.id, zone.state, false);
                const localZone = zonesData.find(item => item.id === zone.id);
                if (localZone && canApplyResyncedZoneVersion(zone.id, zone.version)) {
                    localZone.version = zone.version;
                }
            }
        });
    }

    // Инициализация
    document.addEventListener('DOMContentLoaded', async () => {
        await loadData();
        // Подпишемся на поток статусов зон через SSE
        try {
            const es = new EventSource('/api/mqtt/zones-sse');
            es.onopen = () => {
                zoneStateFeedFailed = false;
                resyncZoneStates().catch(error => {
                    console.warn('Не удалось синхронизировать состояния зон после подключения SSE:', error);
                    showNotification('Не удалось синхронизировать состояния зон', 'error');
                });
            };
            es.onmessage = (ev)=>{
                try{
                    const data = JSON.parse(ev.data);
                    applyZoneState(data.zone_id, data.state, true);
                    resyncZoneStates().catch(error => {
                        console.warn('Не удалось обновить версию зоны после события SSE:', error);
                    });
                }catch(e){}
            };
            es.onerror = () => {
                if (!zoneStateFeedFailed) {
                    zoneStateFeedFailed = true;
                    showNotification('Поток состояний зон недоступен; показанные состояния могут устареть', 'warning');
                }
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
        
        if (file.size > 20 * 1024 * 1024) { // 20MB limit (issue #11)
            showNotification('Размер файла не должен превышать 20 МБ', 'error');
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

    // Issue #11: Esc closes the lightbox.
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        var modal = document.getElementById('photoModal');
        if (modal && modal.style.display === 'flex') {
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
            
            const result = await api.put(`/api/groups/${groupId}`, updatedGroup);
            if (responseSucceeded(result)) {
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
            } else {
                showNotification(responseMessage(result, 'Ошибка сохранения группы'), 'error');
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
            const successCount = results.filter(responseSucceeded).length;
            
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
        if (isGroupHardwareLocked(groupId)) {
            showNotification('Остановите полив группы перед удалением', 'warning');
            return;
        }
        if (!confirm(`Удалить группу ${groupsData.find(g => g.id === groupId)?.name || groupId}?`)) {
            return;
        }

        try {
            const resp = await fetch(`/api/groups/${groupId}`, { method: 'DELETE' });
            const result = resp.status === 204 ? null : await resp.json().catch(() => null);
            if (resp.ok && responseSucceeded(result === null ? true : result)) {
                showNotification('Группа удалена', 'success');
                groupsData = groupsData.filter(g => g.id !== groupId);
                modifiedGroups.delete(groupId);
                renderGroupsGrid();
                await loadData(); // Перезагружаем все данные, чтобы обновить счетчик зон
            } else {
                showNotification(responseMessage(result, 'Ошибка удаления группы'), 'error');
            }
        } catch (error) {
            console.error('Ошибка удаления группы:', error);
            showNotification('Ошибка удаления группы', 'error');
        }
    }
    
    function encodeCSVCell(value) {
        let text = value == null ? '' : String(value);
        // Spreadsheet programs execute cells beginning with formula sigils.
        // Prefix the original value before normal RFC 4180 quoting.
        if (/^[\u0000-\u0020]*[=+\-@]/.test(text)) text = "'" + text;
        const needsQuotes = text.includes('"') || text.includes(',') || text.includes('\r') || text.includes('\n');
        return needsQuotes ? `"${text.replace(/"/g, '""')}"` : text;
    }

    function parseCSV(text) {
        const rows = [];
        let row = [];
        let field = '';
        let quoted = false;

        for (let index = 0; index < text.length; index++) {
            const char = text[index];
            if (quoted) {
                if (char === '"' && text[index + 1] === '"') {
                    field += '"';
                    index += 1;
                } else if (char === '"') {
                    quoted = false;
                } else {
                    field += char;
                }
            } else if (char === '"' && field === '') {
                quoted = true;
            } else if (char === ',') {
                row.push(field);
                field = '';
            } else if (char === '\n' || char === '\r') {
                row.push(field);
                rows.push(row);
                row = [];
                field = '';
                if (char === '\r' && text[index + 1] === '\n') index += 1;
            } else {
                field += char;
            }
        }
        if (field !== '' || row.length > 0) {
            row.push(field);
            rows.push(row);
        }
        return rows;
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
            
            const csv = template.map(row => row.map(encodeCSVCell).join(',')).join('\n');
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
        
        const csv = csvData.map(row => row.map(encodeCSVCell).join(',')).join('\n');
        downloadCSV(csv, `zones_export_${new Date().toISOString().slice(0, 10)}.csv`);
        showNotification(`Экспортировано ${zonesData.length} зон`, 'success');
    }
    
    // Импорт зон из CSV
    function importZonesCSV() {
        document.getElementById('csvFileInput').click();
    }
    
    // Обработка импорта CSV файла
    async function handleCSVImport(event) {
        const input = event.currentTarget || event.target;
        try {
            const file = input && input.files ? input.files[0] : null;
            if (!file) return;
            const text = await file.text();
            const rows = parseCSV(text).filter(row => row.some(cell => cell.trim()));
            if (rows.length === 0) {
                showNotification('Файл не содержит данных для импорта', 'warning');
                return;
            }
            const headers = rows[0].map((header, index) => {
                const normalized = header.trim();
                return index === 0 ? normalized.replace(/^\uFEFF/, '') : normalized;
            });
            
            // Проверяем заголовки
            // Для импорта обязательно только поле id. Остальные — опциональны
            if (!headers.includes('id')) {
                showNotification('Неверный формат файла. Должен быть столбец id', 'error');
                return;
            }
            if (new Set(headers).size !== headers.length) {
                showNotification('Заголовки CSV не должны повторяться', 'error');
                return;
            }
            
            const zonesToImport = [];
            const seenIds = new Set();
            for (let i = 1; i < rows.length; i++) {
                const values = rows[i];
                const get = (key) => {
                    const idx = headers.indexOf(key);
                    return idx >= 0 ? (values[idx] ?? '').trim() : '';
                };
                const idStr = get('id');
                if (!idStr) continue;
                const zoneId = parseCanonicalPositiveInt(idStr);
                if (zoneId === null) throw new Error(`Строка ${i + 1}: id должен быть каноническим положительным целым числом`);
                if (seenIds.has(zoneId)) throw new Error(`Строка ${i + 1}: повторяющийся id ${zoneId}`);
                seenIds.add(zoneId);
                const zone = { id: zoneId };
                const name = get('name'); if (name) zone.name = name;
                const icon = get('icon'); if (icon) zone.icon = icon;
                const dur = get('duration');
                if (dur) {
                    zone.duration = parseZoneDuration(dur);
                    if (zone.duration === null) throw new Error(`Строка ${i + 1}: duration должна быть целым числом от 1 до 240`);
                }
                const gid = get('group_id');
                if (gid) {
                    zone.group_id = parseCanonicalPositiveInt(gid);
                    if (zone.group_id === null) throw new Error(`Строка ${i + 1}: некорректный group_id`);
                }
                // state is exported for diagnostics, but is never imported:
                // DB state is authoritative and follows confirmed hardware events.
                const topic = get('topic'); if (topic) zone.topic = topic;
                const mqtt = get('mqtt_server_id');
                if (mqtt) {
                    zone.mqtt_server_id = parseCanonicalPositiveInt(mqtt);
                    if (zone.mqtt_server_id === null) throw new Error(`Строка ${i + 1}: некорректный mqtt_server_id`);
                }
                zonesToImport.push(zone);
            }
            
            if (zonesToImport.length === 0) {
                showNotification('Файл не содержит данных для импорта', 'warning');
                return;
            }

            const activeHardwareChanges = zonesToImport.filter(zone => {
                const current = zonesData.find(item => Number(item.id) === Number(zone.id));
                if (!current || !isZoneHardwareLocked(zone.id)) return false;
                return (zone.group_id !== undefined && Number(zone.group_id) !== Number(current.group_id))
                    || (zone.topic !== undefined && String(zone.topic) !== String(current.topic || ''))
                    || (zone.mqtt_server_id !== undefined
                        && Number(zone.mqtt_server_id) !== Number(current.mqtt_server_id || 0));
            });
            if (activeHardwareChanges.length) {
                showNotification(
                    `Остановите активные зоны перед изменением аппаратной конфигурации: ${activeHardwareChanges.map(zone => zone.id).join(', ')}`,
                    'warning'
                );
                return;
            }

            const durationChanges = zonesToImport
                .filter(zone => {
                    const current = zonesData.find(item => Number(item.id) === Number(zone.id));
                    return current && zone.duration !== undefined && Number(current.duration) !== Number(zone.duration);
                })
                .map(zone => ({ zone_id: zone.id, new_duration: zone.duration }));
            if (durationChanges.length) {
                let results;
                try {
                    results = await checkDurationConflicts(durationChanges);
                } catch (error) {
                    showNotification(error.message || 'Не удалось проверить конфликты. Импорт отменён.', 'error');
                    return;
                }
                const conflicted = durationChanges.filter(change => results[String(change.zone_id)].has_conflicts);
                if (conflicted.length) {
                    const first = results[String(conflicted[0].zone_id)];
                    showDurationConflictModal(first.conflicts || []);
                    showNotification(`Импорт отменён: конфликты длительности у зон ${conflicted.map(item => item.zone_id).join(', ')}`, 'warning');
                    return;
                }
            }
            
            if (confirm(`Импортировать ${zonesToImport.length} зон?`)) {
                await importZones(zonesToImport);
            }
            
        } catch (error) {
            console.error('Ошибка импорта CSV:', error);
            showNotification(error.message || 'Ошибка чтения CSV файла', 'error');
        } finally {
            // Selecting the same corrected file must always fire `change`
            // after any validation, conflict, cancellation, or network exit.
            if (input) input.value = '';
        }
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
            const j = await resp.json().catch(() => null);
            if (resp.ok && responseSucceeded(j)) {
                await loadData();
                showNotification(
                    `Импорт: создано ${j.created || 0}, обновлено ${j.updated || 0}, ошибок ${j.failed || 0}`,
                    j.failed ? 'warning' : 'success'
                );
            } else {
                showNotification(responseMessage(j, `Импорт завершился с ошибкой (HTTP ${resp.status})`), 'error');
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
    const __zoneSaveInFlight = {};
    const __zoneSavePending = {};
    const __zoneEditRevisions = {};
    function cancelZoneAutoSave(zoneId){
        if (__zoneSaveTimers[zoneId]) clearTimeout(__zoneSaveTimers[zoneId]);
        __zoneSaveTimers[zoneId] = null;
    }
    function queueZoneAutoSave(zoneId){
        if (__zoneSaveInFlight[zoneId]) {
            __zoneSavePending[zoneId] = true;
            return;
        }
        __zoneSaveInFlight[zoneId] = true;
        saveZone(zoneId).then(saved => {
            if (!saved) {
                __zoneSavePending[zoneId] = false;
                cancelZoneAutoSave(zoneId);
            }
        }).catch(() => {
            __zoneSavePending[zoneId] = false;
            cancelZoneAutoSave(zoneId);
        }).finally(() => {
            __zoneSaveInFlight[zoneId] = false;
            if (__zoneSavePending[zoneId]) {
                __zoneSavePending[zoneId] = false;
                cancelZoneAutoSave(zoneId);
                queueZoneAutoSave(zoneId);
            }
        });
    }
    function scheduleZoneAutoSave(zoneId){
        if (__zoneSaveTimers[zoneId]) clearTimeout(__zoneSaveTimers[zoneId]);
        __zoneSaveTimers[zoneId] = setTimeout(()=>{ queueZoneAutoSave(zoneId); }, 500);
    }
