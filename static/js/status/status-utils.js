    // === Utility functions ===

    async function syncServerTime() {
        try {
            const r = await fetch('/api/server-time?ts=' + Date.now(), { cache: 'no-store' });
            const j = await r.json();
            if (j && j.now_iso) {
                var serverMs = new Date(j.now_iso).getTime();
                _serverTimeOffset = serverMs - Date.now();
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
    
    function anyGroupUsesWaterMeter() {
        try {
            const groups = (statusData && Array.isArray(statusData.groups)) ? statusData.groups : [];
            const _flag = v => { try { if (v===true||v===1) return true; const s=String(v).trim().toLowerCase(); return s==='1'||s==='true'||s==='on'||s==='yes'; } catch(e){ return false; } };
            return groups.some(g => _flag(g.use_water_meter));
        } catch(e) { return false; }
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
