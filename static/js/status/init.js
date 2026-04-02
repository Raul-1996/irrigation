// status/init.js — DOMContentLoaded, SSR hydration, global exports

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
            if (window._ssrStatus && window._ssrStatus.groups) {
                statusData = window._ssrStatus;
                updateStatusDisplay();
            }
            renderGroupTabs();
            renderZoneCards();
            try { updateActiveZoneIndicator(zonesData); } catch(e) {}
            try { updateWaterMeter(zonesData); } catch(e) {}
            // Then refresh in background for live data
            setTimeout(function() {
                loadZonesData().then(function(){ return loadStatusData(); }).catch(function(){});
            }, 1000);
        } else {
            // No SSR data, fetch normally — zones FIRST so statusDisplay can find them
            loadZonesData().then(function(){ return loadStatusData(); }).catch(function(){});
        }
        
        // Синхронизация времени раз в 5 минут
        setInterval(syncServerTime, 5 * 60 * 1000);
        
        // Обновление времени каждую секунду
        setInterval(updateDateTime, 1000);
        
        // Обновление данных каждые 30 секунд (was 5s — caused flicker)
        setInterval(() => {
            loadZonesData().then(function(){ return loadStatusData(); }).catch(function(){});
        }, 30000);
        setInterval(tickCountdowns, 1000);

        // При возврате на страницу (iOS background freeze) — пересчитать таймеры + обновить данные
        document.addEventListener('visibilitychange', function() {
            if (!document.hidden) {
                // Мгновенно пересчитать таймеры из planned_end_time (убираем drift от замороженных setInterval)
                recalcTimersFromRealTime();
                // Затем загружаем свежие данные с сервера — zones first
                loadZonesData().then(function(){ return loadStatusData(); }).catch(function(){});
            }
        });
        
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

    window.toggleZoneRun = toggleZoneRun;
    window.changeZoneDur = changeZoneDur;
    window.runGroupWithDefaults = runGroupWithDefaults;
    window.showGroupRunPopup = showGroupRunPopup;
    window.runSelectedGroup = runSelectedGroup;
    window.openZoneSheet = openZoneSheet;
    window.closeZoneSheet = closeZoneSheet;
    window.saveZoneEdit = saveZoneEdit;
    window.confirmRunWithDefaults = confirmRunWithDefaults;
    window.showRunPopup = showRunPopup;
    window.closeRunPopup = closeRunPopup;
    window.setRunDur = setRunDur;
    window.confirmRun = confirmRun;
    window.showLoading = showLoading;
    window.hideLoading = hideLoading;

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

    // showLoading, hideLoading, showZoneToast, showPhotoModal, closePhotoModal
    // are defined in ui-helpers.js (loaded before this file)
