    let programsData = [];
    let zonesData = [];
    let currentStep = 1;
    let editId = null;
    
    // Загрузка данных
    async function loadData() {
        try {
            const [progs, zones, groups] = await Promise.all([
                api.get('/api/programs'),
                api.get('/api/zones'),
                api.get('/api/groups')
            ]);
            programsData = progs; zonesData = zones; window.groupsCache = groups;
            renderProgramsList();
        } catch (error) {
            console.error('Ошибка загрузки данных:', error);
            showNotification('Ошибка загрузки данных', 'error');
        }
    }
    
    // Форматирование списка зон
    function formatZonesList(zones) {
        if (!zones || zones.length === 0) {
            return '—';
        }
        
        // Сортируем зоны по ID
        const sortedZones = [...zones].sort((a, b) => a - b);
        
        // Если зон мало, просто перечисляем их
        if (sortedZones.length <= 5) {
            return sortedZones.join(', ');
        }
        
        // Ищем последовательные диапазоны
        const ranges = [];
        let start = sortedZones[0];
        let end = sortedZones[0];
        
        for (let i = 1; i < sortedZones.length; i++) {
            if (sortedZones[i] === end + 1) {
                end = sortedZones[i];
            } else {
                // Добавляем текущий диапазон
                if (start === end) {
                    ranges.push(start.toString());
                } else {
                    ranges.push(`${start}-${end}`);
                }
                start = end = sortedZones[i];
            }
        }
        
        // Добавляем последний диапазон
        if (start === end) {
            ranges.push(start.toString());
        } else {
            ranges.push(`${start}-${end}`);
        }
        
        return ranges.join(', ');
    }
    
    // Рендер списка программ
    function renderProgramsList() {
        const tbody = document.getElementById('prog-body');
        tbody.innerHTML = '';
        
        programsData.forEach(program => {
            const tr = document.createElement('tr');
            
            // Форматируем список зон
            const zonesText = formatZonesList(program.zones);
            
            const startText = computeProgramStartText(program);
            const finishText = computeProgramFinishText(program);
            tr.innerHTML = `
                <td>${program.id}</td>
                <td>${program.name}</td>
                <td>${startText}</td>
                <td>${finishText}</td>
                <td>${formatDays(program.days)}</td>
                <td>${zonesText}</td>
                <td>
                    <button class="edit-btn" onclick="editProg(${program.id})">✏</button>
                    <button class="del-btn" onclick="deleteProg(${program.id})">🗑️</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    }

    function formatDays(days) {
        const map = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
        return (days||[]).map(d=>map[Number(d)||0]).join(', ');
    }

    function computeProgramGroupTimes(program){
        try{
            const time = program.time; // 'HH:MM'
            const [h,m] = time.split(':').map(Number);
            const startMin = h*60+m;
            // последовательный порядок — по возрастанию id зон
            const sortedZones = (program.zones||[]).slice().sort((a,b)=>a-b);
            const groupInfo = {}; // gid -> {start, total}
            let cumulative = 0;
            for (const zid of sortedZones){
                const z = zonesData.find(x=>x.id===zid);
                if(!z) continue;
                const gid = z.group_id;
                if (!groupInfo[gid]){
                    groupInfo[gid] = { start: startMin + cumulative, total: 0 };
                }
                const dur = Number(z.duration||0);
                groupInfo[gid].total += dur;
                cumulative += dur;
            }
            const rows = [];
            const fmt = (min)=>String(Math.floor((min%1440)/60)).padStart(2,'0')+":"+String(min%60).padStart(2,'0');
            for (const gid of Object.keys(groupInfo)){
                const info = groupInfo[gid];
                const gname = (gid==999)?'БЕЗ ПОЛИВА':(((window.groupsCache||[]).find(g=>g.id==gid)||{}).name||`Группа ${gid}`);
                rows.push({ gid: Number(gid), name: gname, start: fmt(info.start), end: fmt(info.start + info.total) });
            }
            rows.sort((a,b)=>a.gid-b.gid);
            return rows;
        }catch(e){return []}
    }

    function computeProgramStartText(program){
        const rows = computeProgramGroupTimes(program);
        if (rows.length<=1) return program.time;
        return rows.map(r=>`${r.name}: ${r.start}`).join('<br/>');
    }

    function computeProgramFinishText(program){
        const rows = computeProgramGroupTimes(program);
        if (rows.length===0) return '—';
        return rows.map(r=>`${r.name}: ${r.end}`).join('<br/>');
    }
    
    // Мастер создания/редактирования
    async function openWizard() {
        closeWizard();
        document.getElementById('wizardModal').style.display = 'flex';
        showStep(1);
        await loadZoneSelector();
        clearFields();
        // После загрузки селектора навесим обработчики синхронизации чекбоксов
        wireZoneSelectionHandlers();
    }
    
    async function editProg(id) {
        const program = programsData.find(x => x.id === id);
        if (!program) return;
        
        editId = id;
        document.getElementById('wizardModal').style.display = 'flex';
        showStep(1);
        await loadZoneSelector();
        
        // Заполняем поля
        document.getElementById('progName').value = program.name;
        document.getElementById('progTime').value = program.time;
        
        // Выбираем дни
        Array.from(document.getElementById('progDays').options).forEach(option => {
            option.selected = program.days.includes(parseInt(option.value));
        });
        
        // Выбираем зоны
        setTimeout(() => {
            document.querySelectorAll('.zone-check').forEach(checkbox => {
                checkbox.checked = program.zones.includes(parseInt(checkbox.value));
            });
            // После проставления зон — обновим состояние групп и мастера
            updateAllGroupCheckboxes();
            updateMasterCheckbox();
        }, 100);
        // Навесим обработчики
        wireZoneSelectionHandlers();
    }
    
    async function deleteProg(id) {
        const program = programsData.find(x => x.id === id);
        if (!program) return;
        
        if (!confirm(`Удалить программу "${program.name}"?`)) return;
        
        try {
            await api.delete(`/api/programs/${id}`);
            programsData = programsData.filter(x => x.id !== id);
            renderProgramsList();
            showNotification('Программа удалена', 'success');
        } catch (error) {
            console.error('Ошибка удаления программы:', error);
            showNotification('Ошибка удаления программы', 'error');
        }
    }
    
    function clearFields() {
        document.getElementById('progName').value = '';
        document.getElementById('progTime').value = '';
        Array.from(document.getElementById('progDays').options).forEach(option => {
            option.selected = false;
        });
        document.querySelectorAll('.zone-check, .group-check').forEach(checkbox => {
            checkbox.checked = false;
        });
        editId = null;
    }
    
    function closeWizard() {
        document.getElementById('wizardModal').style.display = 'none';
        clearFields();
    }
    
    function showStep(n) {
        [1, 2].forEach(i => {
            document.getElementById('step' + i).classList.toggle('active', i === n);
        });
        
        document.getElementById('prevBtn').disabled = n === 1;
        document.getElementById('nextBtn').style.display = n < 2 ? 'inline-block' : 'none';
        document.getElementById('saveBtn').style.display = n === 2 ? 'inline-block' : 'none';
        currentStep = n;
    }
    
    function nextStep() {
        if (currentStep < 2) showStep(currentStep + 1);
    }
    
    function prevStep() {
        if (currentStep > 1) showStep(currentStep - 1);
    }
    
    function selectAllDays() {
        Array.from(document.getElementById('progDays').options).forEach(option => {
            option.selected = true;
        });
    }
    
    async function loadZoneSelector() {
        const container = document.getElementById('zoneSelector');
        container.innerHTML = '';
        
        try {
            // Загружаем группы и зоны из API
            const groups = await api.get('/api/groups');
            const zones = await api.get('/api/zones');
            
            // Группируем зоны по группам (по факту наличия зон)
            const zonesByGroup = {};
            zones.forEach(zone => {
                const gid = Number(zone.group_id ?? zone.group);
                if (!Number.isFinite(gid)) return;
                zonesByGroup[gid] = zonesByGroup[gid] || [];
                zonesByGroup[gid].push(zone);
            });
            
            // Построим метаданные групп: из API групп + из самих зон (если вдруг группа есть в зонах, но не пришла в списке)
            const groupMeta = {};
            (groups || []).forEach(g => { groupMeta[Number(g.id)] = { id: Number(g.id), name: g.name }; });
            Object.keys(zonesByGroup).forEach(k => {
                const gid = Number(k);
                if (!groupMeta[gid]) groupMeta[gid] = { id: gid, name: `Группа ${gid}` };
            });
            
            // Список групп, которые реально имеют зоны, отсортированный по id
            const renderGroupIds = Object.keys(zonesByGroup).map(n=>Number(n)).filter(gid => gid !== 999).sort((a,b)=>a-b);
            
            // Если почему-то ничего не отрендерилось — покажем подсказку
            if (renderGroupIds.length === 0) {
                const hint = document.createElement('div');
                hint.style.color = '#555';
                hint.textContent = 'Зоны не найдены. Проверьте, что зоны привязаны к группам.';
                container.appendChild(hint);
            }
            
            // Создаем блоки
            renderGroupIds.forEach(gid => {
                const groupZones = (zonesByGroup[gid] || []).slice().sort((a,b)=>a.id-b.id);
                if (!groupZones.length) return;
                const meta = groupMeta[gid] || { id: gid, name: `Группа ${gid}` };
                
                const groupDiv = document.createElement('div');
                groupDiv.className = 'group-block';
                
                const groupLabel = document.createElement('label');
                groupLabel.innerHTML = `<input type='checkbox' class='group-check' data-group='${gid}' onclick='toggleGroup(${gid}, this)'> ${meta.name}`;
                groupDiv.appendChild(groupLabel);
                
                const zonesDiv = document.createElement('div');
                zonesDiv.className = 'zone-item';
                
                groupZones.forEach(zone => {
                    const zoneLabel = document.createElement('label');
                    const icon = zone.icon || '';
                    zoneLabel.innerHTML = `<input type='checkbox' class='zone-check' data-group='${gid}' value='${zone.id}'> ${icon} Зона ${zone.id} (${zone.name})`;
                    zonesDiv.appendChild(zoneLabel);
                });
                
                groupDiv.appendChild(zonesDiv);
                container.appendChild(groupDiv);
            });
            
        } catch (error) {
            console.error('Ошибка загрузки зон:', error);
            showNotification('Ошибка загрузки зон', 'error');
        }
    }
    
    function toggleAllZones(checkbox) {
        const isChecked = checkbox.checked;
        document.querySelectorAll('.group-check, .zone-check').forEach(ch => {
            ch.checked = isChecked;
        });
        // Сбросим промежуточное состояние у групп и пересчитаем мастер
        document.querySelectorAll('.group-check').forEach(ch=>{ ch.indeterminate = false; });
        updateMasterCheckbox();
    }
    
    function toggleGroup(group, checkbox) {
        document.querySelectorAll(`.zone-check[data-group='${group}']`).forEach(ch => {
            ch.checked = checkbox.checked;
        });
        // Группа кликнута пользователем — убираем indeterminate и пересчитываем мастер
        checkbox.indeterminate = false;
        updateMasterCheckbox();
    }

    function updateGroupCheckbox(groupId){
        const groupCb = document.querySelector(`.group-check[data-group='${groupId}']`);
        if (!groupCb) return;
        const zones = document.querySelectorAll(`.zone-check[data-group='${groupId}']`);
        const total = zones.length;
        const checked = Array.from(zones).filter(z=>z.checked).length;
        groupCb.checked = (total>0 && checked === total);
        groupCb.indeterminate = (checked>0 && checked<total);
    }

    function updateAllGroupCheckboxes(){
        document.querySelectorAll('.group-check').forEach(cb=>{
            const gid = cb.getAttribute('data-group');
            updateGroupCheckbox(gid);
        });
    }

    function updateMasterCheckbox(){
        const master = document.getElementById('selectAllZones');
        const zones = document.querySelectorAll('.zone-check');
        const groups = document.querySelectorAll('.group-check');
        const totalZ = zones.length;
        const checkedZ = Array.from(zones).filter(z=>z.checked).length;
        const totalG = groups.length;
        const checkedG = Array.from(groups).filter(g=>g.checked).length;
        master.checked = (totalZ>0 && checkedZ === totalZ && totalG>0 && checkedG === totalG);
        master.indeterminate = (!master.checked) && ((checkedZ>0 && checkedZ<totalZ) || (checkedG>0 && checkedG<totalG));
    }

    function wireZoneSelectionHandlers(){
        // Делегируем клики на контейнер зон
        const container = document.getElementById('zoneSelector');
        if (!container) return;
        container.addEventListener('change', (ev)=>{
            const t = ev.target;
            if (!(t instanceof HTMLInputElement)) return;
            if (t.classList.contains('zone-check')){
                const gid = t.getAttribute('data-group');
                updateGroupCheckbox(gid);
                updateMasterCheckbox();
            } else if (t.classList.contains('group-check')){
                const gid = t.getAttribute('data-group');
                toggleGroup(gid, t);
                updateGroupCheckbox(gid);
                updateMasterCheckbox();
            }
        });
        // Также навесим на мастер
        const master = document.getElementById('selectAllZones');
        if (master){
            master.addEventListener('change', (ev)=>{
                toggleAllZones(ev.target);
                updateAllGroupCheckboxes();
                updateMasterCheckbox();
            });
        }
        // Начальная синхронизация
        updateAllGroupCheckboxes();
        updateMasterCheckbox();
    }
    
    async function checkConflicts() {
        const time = document.getElementById('progTime').value;
        const days = Array.from(document.getElementById('progDays').selectedOptions).map(option => parseInt(option.value));
        const zones = Array.from(document.querySelectorAll('.zone-check:checked')).map(checkbox => parseInt(checkbox.value));
        
        if (!time || !days.length || !zones.length) {
            return { has_conflicts: false, conflicts: [] };
        }
        
        try {
            const response = await api.post('/api/programs/check-conflicts', {
                program_id: editId,
                time: time,
                days: days,
                zones: zones
            });
            
            return response;
        } catch (error) {
            console.error('Ошибка проверки конфликтов:', error);
            return { has_conflicts: false, conflicts: [] };
        }
    }
    
    function formatConflictMessage(conflict) {
        const startTime = Math.floor(conflict.overlap_start / 60).toString().padStart(2, '0') + ':' + 
                         (conflict.overlap_start % 60).toString().padStart(2, '0');
        const endTime = Math.floor(conflict.overlap_end / 60).toString().padStart(2, '0') + ':' + 
                       (conflict.overlap_end % 60).toString().padStart(2, '0');
        
        let message = `Программа "${conflict.program_name}" (${conflict.program_time}, ${conflict.program_duration} мин) - пересечение с ${startTime} до ${endTime}`;
        
        // Добавляем информацию о пересечении зон
        if (conflict.common_zones && conflict.common_zones.length > 0) {
            message += `\nПересекающиеся зоны: ${conflict.common_zones.join(', ')}`;
        }
        
        // Добавляем информацию о пересечении групп
        if (conflict.common_groups && conflict.common_groups.length > 0) {
            message += `\nПересекающиеся группы: ${conflict.common_groups.join(', ')}`;
        }
        
        return message;
    }
    
    async function saveProg() {
        const name = document.getElementById('progName').value.trim();
        const time = document.getElementById('progTime').value;
        const days = Array.from(document.getElementById('progDays').selectedOptions).map(option => parseInt(option.value));
        const zones = Array.from(document.querySelectorAll('.zone-check:checked')).map(checkbox => parseInt(checkbox.value));
        
        if (!name || !time || !days.length || !zones.length) {
            showNotification('Заполните все поля и выберите зоны', 'warning');
            return;
        }
        
        // Проверяем конфликты перед сохранением
        const conflictCheck = await checkConflicts();
        if (conflictCheck.has_conflicts) {
            let message = '⚠️ Обнаружены конфликты с существующими программами:\n\n';
            conflictCheck.conflicts.forEach(conflict => {
                message += `• ${formatConflictMessage(conflict)}\n`;
            });
            message += '\nПожалуйста, измените время начала полива или выберите другие зоны.';
            
            showNotification(message, 'warning');
            return;
        }
        
        try {
            if (editId) {
                // Редактирование
                const program = programsData.find(x => x.id === editId);
                if (!program) throw new Error('Программа не найдена');
                
                const updatedProgram = { ...program, name, time, days, zones };
                const res = await api.put(`/api/programs/${editId}`, updatedProgram);
                if (res && res.has_conflicts) {
                    let message = '⚠️ Обнаружены конфликты с существующими программами:\n\n';
                    res.conflicts.forEach(conflict => {
                        message += `• ${formatConflictMessage(conflict)}\n`;
                    });
                    showNotification(message, 'warning');
                    return;
                }
                Object.assign(program, updatedProgram);
                showNotification('Программа обновлена', 'success');
            } else {
                // Создание
                const res = await api.post('/api/programs', { name, time, days, zones });
                if (res && res.has_conflicts) {
                    let message = '⚠️ Обнаружены конфликты с существующими программами:\n\n';
                    res.conflicts.forEach(conflict => {
                        message += `• ${formatConflictMessage(conflict)}\n`;
                    });
                    showNotification(message, 'warning');
                    return;
                }
                programsData.push(res);
                showNotification('Программа создана', 'success');
            }
            
            renderProgramsList();
            closeWizard();
        } catch (error) {
            console.error('Ошибка сохранения программы:', error);
            showNotification('Ошибка сохранения программы', 'error');
        }
    }
    
    // Инициализация
    document.addEventListener('DOMContentLoaded', () => {
        loadData();
        
        // Закрытие модального окна при клике вне его
        document.getElementById('wizardModal').addEventListener('click', (e) => {
            if (e.target.classList.contains('modal')) {
                closeWizard();
            }
        });
    });
