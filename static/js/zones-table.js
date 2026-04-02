/**
 * zones-table.js — Zone table rendering, sorting, inline editing, icon dropdown, selection
 * Depends on: zones-core.js
 */

// ===== Zone table rendering =====
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

// ===== Sorting =====
function sortTable(columnIndex) {
    if (sortColumn === columnIndex) {
        sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        sortColumn = columnIndex;
        sortDirection = 'asc';
    }
    
    document.querySelectorAll('.zones-table th').forEach((th, index) => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (index === columnIndex) {
            th.classList.add(`sort-${sortDirection}`);
        }
    });
    
    zonesData.sort((a, b) => {
        let aVal, bVal;
        
        switch (columnIndex) {
            case 0: aVal = a.id; bVal = b.id; break;
            case 1: aVal = a.icon; bVal = b.icon; break;
            case 2: aVal = a.name; bVal = b.name; break;
            case 3: aVal = a.duration; bVal = b.duration; break;
            case 4:
                aVal = groupsData.find(g => g.id === a.group_id)?.name || '';
                bVal = groupsData.find(g => g.id === b.group_id)?.name || '';
                break;
            default: return 0;
        }
        
        if (sortDirection === 'asc') {
            return aVal > bVal ? 1 : -1;
        } else {
            return aVal < bVal ? 1 : -1;
        }
    });
    
    renderZonesTable();
}

// ===== Inline zone editing =====
function updateZone(zoneId, field, value) {
    const row = document.querySelector(`tr[data-zone-id="${zoneId}"]`);
    if (row) {
        row.classList.add('modified');
        modifiedZones.add(zoneId);
        scheduleZoneAutoSave(zoneId);
    }
}

var __zoneSaveTimers = {};
function scheduleZoneAutoSave(zoneId){
    if (__zoneSaveTimers[zoneId]) clearTimeout(__zoneSaveTimers[zoneId]);
    __zoneSaveTimers[zoneId] = setTimeout(()=>{ saveZone(zoneId); }, 500);
}

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

        // Проверка конфликтов
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

// ===== Icon dropdown =====
function toggleIconDropdown(zoneId) {
    const dropdown = document.getElementById(`icon-dropdown-${zoneId}`);
    const allDropdowns = document.querySelectorAll('.icon-dropdown-content');
    allDropdowns.forEach(d => {
        if (d !== dropdown) { d.classList.remove('show'); }
    });
    dropdown.classList.toggle('show');
}

function selectIcon(zoneId, icon) {
    const zone = zonesData.find(z => z.id === zoneId);
    if (zone) {
        zone.icon = icon;
        updateZone(zoneId, 'icon', icon);
        const iconElement = document.querySelector(`tr[data-zone-id="${zoneId}"] .zone-icon`);
        if (iconElement) { iconElement.textContent = icon; }
        const dropdown = document.getElementById(`icon-dropdown-${zoneId}`);
        dropdown.classList.remove('show');
    }
}

function changeZoneIcon(zoneId) {
    const icons = ['🌿', '🌳', '🌺', '🌻', '🌹', '🌸', '🌼', '🌷', '🌱', '🌲', '🌴', '🌵', '🍀', '🌾', '🌽', '🥕', '🍅', '🥬', '🧱'];
    const currentIcon = zonesData.find(z => z.id === zoneId)?.icon || '🌿';
    const currentIndex = icons.indexOf(currentIcon);
    const nextIndex = (currentIndex + 1) % icons.length;
    const newIcon = icons[nextIndex];
    
    updateZone(zoneId, 'icon', newIcon);
    const iconElement = document.querySelector(`tr[data-zone-id="${zoneId}"] .zone-icon`);
    if (iconElement) { iconElement.textContent = newIcon; }
}

// Close icon dropdowns on outside click
document.addEventListener('click', function(event) {
    if (!event.target.closest('.icon-dropdown')) {
        const dropdowns = document.querySelectorAll('.icon-dropdown-content');
        dropdowns.forEach(dropdown => { dropdown.classList.remove('show'); });
    }
});

// ===== Selection =====
function toggleSelectAll() {
    const selectAll = document.getElementById('selectAll');
    const checkboxes = document.querySelectorAll('.zone-checkbox');
    checkboxes.forEach(checkbox => { checkbox.checked = selectAll.checked; });
    updateSelectedCount();
}

function selectAllZones() {
    const checkboxes = document.querySelectorAll('.zone-checkbox');
    checkboxes.forEach(checkbox => { checkbox.checked = true; });
    document.getElementById('selectAll').checked = true;
    updateSelectedCount();
}

function deselectAllZones() {
    const checkboxes = document.querySelectorAll('.zone-checkbox');
    checkboxes.forEach(checkbox => { checkbox.checked = false; });
    document.getElementById('selectAll').checked = false;
    updateSelectedCount();
}

function updateSelectedCount() {
    const checkboxes = document.querySelectorAll('.zone-checkbox:checked');
    document.getElementById('selected-count').textContent = `Выбрано: ${checkboxes.length} зон`;
}
