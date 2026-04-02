/**
 * zones-groups.js — Groups grid rendering, group CRUD, autosave, sensors toggles
 * Depends on: zones-core.js
 */

// ===== Groups grid rendering =====
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

// ===== Group name autosave =====
var _saveTimers = {};
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

// ===== Group CRUD =====
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
            const groupIndex = groupsData.findIndex(g => g.id === groupId);
            if (groupIndex !== -1) {
                groupsData[groupIndex] = { ...groupsData[groupIndex], ...updatedGroup };
            }
            row.classList.remove('modified');
            modifiedGroups.delete(groupId);
            row.querySelector('.save-btn').disabled = true;
            showNotification('Группа обновлена', 'success');
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
            modifiedGroups.forEach(groupId => {
                const group = groupsData.find(g => g.id === groupId);
                if (group) {
                    const row = document.querySelector(`tr[data-group-id="${groupId}"]`);
                    const nameInput = row.querySelector('.group-name');
                    
                    const groupIndex = groupsData.findIndex(g => g.id === groupId);
                    if (groupIndex !== -1) {
                        groupsData[groupIndex].name = nameInput.value;
                    }
                    
                    row.classList.remove('modified');
                    row.querySelector('.save-btn').disabled = true;
                }
            });
            
            modifiedGroups.clear();
            showNotification(`Сохранено ${successCount} групп`, 'success');
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
            await loadData();
        } else {
            const error = await resp.json().catch(() => ({}));
            showNotification(error.message || 'Ошибка удаления группы', 'error');
        }
    } catch (error) {
        console.error('Ошибка удаления группы:', error);
        showNotification('Ошибка удаления группы', 'error');
    }
}
