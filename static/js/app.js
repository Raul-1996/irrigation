        // CSRF token interceptor: attach token to all non-GET fetch requests
        (function() {
            var csrfMeta = document.querySelector('meta[name="csrf-token"]');
            var csrfToken = csrfMeta ? csrfMeta.content : null;
            var origFetch = window.fetch;
            window.fetch = function(url, opts) {
                opts = opts || {};
                if (opts.method && opts.method.toUpperCase() !== 'GET') {
                    if (!opts.headers) opts.headers = {};
                    // Support both plain object and Headers instance
                    if (csrfToken) {
                        if (opts.headers instanceof Headers) {
                            if (!opts.headers.has('X-CSRFToken')) opts.headers.set('X-CSRFToken', csrfToken);
                        } else {
                            if (!opts.headers['X-CSRFToken']) opts.headers['X-CSRFToken'] = csrfToken;
                        }
                    }
                }
                return origFetch.call(this, url, opts);
            };
            // Also patch XMLHttpRequest for legacy code
            var origOpen = XMLHttpRequest.prototype.open;
            var origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method) {
                this._csrfMethod = method;
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function() {
                if (this._csrfMethod && this._csrfMethod.toUpperCase() !== 'GET' && csrfToken) {
                    try { this.setRequestHeader('X-CSRFToken', csrfToken); } catch(e) {}
                }
                return origSend.apply(this, arguments);
            };
        })();

        // Update footer time from server clock to avoid client TZ drift
        let _serverNow = null;
        function _fmt(dt){ const p=n=>String(n).padStart(2,'0'); return `${dt.getFullYear()}-${p(dt.getMonth()+1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}`; }
        async function syncFooterTimeFromServer(){
            try{
                const r = await fetch('/api/server-time?ts=' + Date.now(), { cache: 'no-store' });
                const j = await r.json();
                if (j && j.now_iso){
                    // Parse 'YYYY-MM-DD HH:MM:SS' as local
                    _serverNow = new Date(j.now_iso.replace(' ','T'));
                    document.getElementById('footer-time').textContent = _fmt(_serverNow);
                }
            }catch(e){
                // Fallback to client clock once
                const now = new Date();
                document.getElementById('footer-time').textContent = _fmt(now);
            }
        }
        function tickFooter(){
            if (_serverNow){ _serverNow = new Date(_serverNow.getTime() + 1000); document.getElementById('footer-time').textContent = _fmt(_serverNow); }
            else { syncFooterTimeFromServer(); }
        }
        syncFooterTimeFromServer();
        setInterval(tickFooter, 1000);
        // Periodic resync to avoid drift (each 60s)
        setInterval(syncFooterTimeFromServer, 60000);
        
        // API utility functions
        const api = {
            async request(url, options = {}) {
                const defaultOptions = {
                    headers: {
                        'Content-Type': 'application/json',
                    },
                };
                
                const config = { ...defaultOptions, ...options };
                
                try {
                    const response = await fetch(url, config);
                    
                    // Не бросаем исключение для 4xx/5xx, чтобы экран мог обработать soft-ошибки (например, конфликты программ)
                    // Ответ всё равно прочитаем ниже и вернём вызывающему коду
                    
                    // Check if response is JSON
                    const contentType = response.headers.get('content-type');
                    if (contentType && contentType.includes('application/json')) {
                        const data = await response.json();
                        // Если сервер прислал success:false + has_conflicts — возвращаем как есть
                        if (!response.ok) {
                            return data;
                        }
                        return data;
                    }
                    // Для не-JSON ответов просто возвращаем текст (даже если !ok)
                    return await response.text();
                } catch (error) {
                    console.error('API request failed:', error);
                    throw error;
                }
            },
            
            async get(url) {
                return this.request(url);
            },
            
            async post(url, data) {
                return this.request(url, {
                    method: 'POST',
                    body: JSON.stringify(data),
                });
            },
            
            async put(url, data) {
                return this.request(url, {
                    method: 'PUT',
                    body: JSON.stringify(data),
                });
            },
            
            async delete(url) {
                return this.request(url, {
                    method: 'DELETE',
                });
            },
        };
        
        // Enhanced notification system
        function showNotification(message, type = 'info', duration = 5000) {
            const container = document.getElementById('notification-container');
            const notification = document.createElement('div');
            notification.className = `notification ${type}`;
            
            const icons = {
                success: '✅',
                error: '❌',
                warning: '⚠️',
                info: 'ℹ️'
            };
            
            notification.innerHTML = `
                <span class="notification-icon">${icons[type] || icons.info}</span>
                <span>${message}</span>
                <button class="notification-close" onclick="this.parentElement.remove()">×</button>
            `;
            
            container.appendChild(notification);
            
            // Trigger animation
            setTimeout(() => notification.classList.add('show'), 10);
            
            // Auto remove
            if (duration > 0) {
                setTimeout(() => {
                    notification.classList.remove('show');
                    setTimeout(() => notification.remove(), 300);
                }, duration);
            }
            
            // Add to notification history
            addToNotificationHistory(message, type);
        }
        
        // Notification history for debugging
        const notificationHistory = [];
        function addToNotificationHistory(message, type) {
            notificationHistory.push({
                message,
                type,
                timestamp: new Date().toISOString()
            });
            
            // Keep only last 50 notifications
            if (notificationHistory.length > 50) {
                notificationHistory.shift();
            }
        }
        
        // Global error handler
        window.addEventListener('error', (event) => {
            console.error('Global error:', event.error);
            showNotification('Произошла ошибка в приложении', 'error');
        });
        
        // Service Worker registration for offline support
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                // Проверяем, существует ли файл sw.js перед регистрацией
                fetch('/sw.js', { method: 'HEAD' })
                    .then(response => {
                        if (response.ok) {
                            return navigator.serviceWorker.register('/sw.js');
                        } else {
                            console.log('SW file not found, skipping registration');
                        }
                    })
                    .then(registration => {
                        if (registration) {
                            console.log('SW registered: ', registration);
                        }
                    })
                    .catch(registrationError => {
                        console.log('SW registration failed: ', registrationError);
                    });
            });
        }
        
        // Performance monitoring
        window.addEventListener('load', () => {
            if ('performance' in window) {
                const loadTime = performance.timing.loadEventEnd - performance.timing.navigationStart;
                console.log(`Page load time: ${loadTime}ms`);
                
                if (loadTime > 3000) {
                    showNotification('Страница загружается медленно', 'warning');
                }
            }
        });

    // Health panel (press 'h') with jobs, zones and locks snapshot
    (function(){
        let panel; let visible = false; let timer = null;
        function build(){
            panel = document.createElement('div');
            panel.style.position='fixed'; panel.style.bottom='10px'; panel.style.right='10px';
            panel.style.background='rgba(0,0,0,0.85)'; panel.style.color='#fff'; panel.style.padding='10px 12px'; panel.style.borderRadius='8px'; panel.style.fontSize='12px'; panel.style.zIndex='2000';
            panel.style.maxWidth='480px'; panel.style.width='480px'; panel.style.display='none'; panel.style.maxHeight='60vh'; panel.style.overflow='auto';
            panel.innerHTML = '<div style="font-weight:bold;margin-bottom:6px;display:flex;align-items:center;gap:8px;">Health <button id="health-refresh" style="margin-left:auto;background:#1976d2;color:#fff;border:none;border-radius:4px;padding:2px 6px;cursor:pointer;">Refresh</button><button id="health-close" style="background:#555;color:#fff;border:none;border-radius:4px;padding:2px 6px;cursor:pointer;">×</button></div><div id="health-content" style="white-space:pre-wrap;word-break:break-word;margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;"></div>';
            document.body.appendChild(panel);
            panel.querySelector('#health-close').onclick = ()=> setVisible(false);
            panel.querySelector('#health-refresh').onclick = ()=> refresh(true);
        }
        async function cancelJob(id){
            try{ const r = await fetch(`/api/health/job/${encodeURIComponent(id)}/cancel`, {method:'POST'}); await r.json(); refresh(true);}catch(e){}
        }
        async function cancelGroup(id){
            try{ const r = await fetch(`/api/health/group/${encodeURIComponent(id)}/cancel`, {method:'POST'}); await r.json(); refresh(true);}catch(e){}
        }
        function renderJobs(jobs){
            if (!jobs || !jobs.length) return '-';
            return jobs.map(j=>{
                const id = j.id; const nrt = j.next_run_time||'n/a'; const js = j.jobstore||'-'; const trig = j.trigger||'';
                return `• ${id}\n   next: ${nrt}  store:${js}\n   ${trig}\n   [ cancel ]`;
            }).join('\n');
        }
        function renderZones(zones){
            if (!zones || !zones.length) return '-';
            return zones.map(z=>`• Z${z.id} G${z.group_id} ${z.state}/${z.commanded_state} seq=${z.sequence_id||'-'} cmd=${z.command_id||'-'} ver=${z.version||0} end=${z.planned_end_time||'-'}  [ cancel group ]`).join('\n');
        }
        function wireActions(d){
            const root = document.getElementById('health-content');
            // Attach click handlers using event delegation
            root.onclick = (ev)=>{
                const t = ev.target; if (!(t instanceof HTMLElement)) return;
                if (t.dataset && t.dataset.job){ cancelJob(t.dataset.job); }
                if (t.dataset && t.dataset.group){ cancelGroup(t.dataset.group); }
            };
            // Convert bracketed actions to clickable spans
            root.innerHTML = root.innerHTML
                .replace(/\[ cancel \]/g, '<span data-job-action style="color:#ff8080;cursor:pointer;">[ cancel ]</span>')
                .replace(/\[ cancel group \]/g, '<span data-group-action style="color:#ff8080;cursor:pointer;">[ cancel group ]</span>');
            // Map actions to ids
            const lines = root.textContent.split('\n');
            let html = '';
            const jobs = d.jobs||[];
            const zones = d.zones||[];
            let ji = 0, zi = 0;
            for (const line of lines){
                if (line.includes('[ cancel ]')){
                    const id = (jobs[ji]||{}).id || '';
                    html += `<div>${line.replace('[ cancel ]', `<span data-job="${(id||'').replace(/"/g,'&quot;') }" style='color:#ff8080;cursor:pointer;'>[ cancel ]</span>`)}</div>`;
                    ji++;
                } else if (line.includes('[ cancel group ]')){
                    const gid = (zones[zi]||{}).group_id || '';
                    html += `<div>${line.replace('[ cancel group ]', `<span data-group="${gid}" style='color:#ff8080;cursor:pointer;'>[ cancel group ]</span>`)}</div>`;
                    zi++;
                } else {
                    html += `<div>${line}</div>`;
                }
            }
            root.innerHTML = html;
        }
        async function refresh(force){
            if (!panel) return;
            try{
                const r = await fetch('/api/health-details?ts=' + Date.now(), {cache:'no-store'});
                const d = await r.json();
                const jobsText = renderJobs(d.jobs);
                const zonesText = renderZones(d.zones);
                const gl = d.locks && d.locks.groups ? Object.entries(d.locks.groups).map(([k,v])=>`G${k}:${v?'locked':'free'}`).join(' ') : '';
                const zl = d.locks && d.locks.zones ? Object.entries(d.locks.zones).map(([k,v])=>`Z${k}:${v?'locked':'free'}`).join(' ') : '';
                const cancels = (d.group_cancels||[]).map(gc=>`G${gc.group_id}:${gc.set?'CANCELLED':''}`).join(' ');
                const meta = (d.meta_tail||[]).map(m=>`${m.ts} ${m.topic} ${m.payload}`).join('\n');
                const text = `now: ${String(d.now).replace('T',' ').slice(0,19)}\nScheduler: ${d.scheduler_running?'running':'stopped'}\n\nJobs:\n${jobsText}\n\nZones:\n${zonesText}\n\nLocks:\n${gl}\n${zl}\n\nGroup cancels:\n${cancels||'-'}\n\nMeta (last ${(d.meta_tail||[]).length}):\n${meta||'-'}`;
                const el = document.getElementById('health-content');
                el.textContent = text;
                wireActions(d);
            }catch(e){ document.getElementById('health-content').textContent = 'error'; }
        }
        function setVisible(v){
            visible = v; if (!panel) build(); panel.style.display = visible?'block':'none';
            if (timer){ clearInterval(timer); timer=null; }
            if (visible){ refresh(true); timer = setInterval(refresh, 2500); }
        }
        document.addEventListener('keydown', (ev)=>{
            if (ev.key === 'h' || ev.key === 'H') setVisible(!visible);
        });
    })();
    // Stopwatch panel (press 's') for UI diagnostics; starts on control button click automatically
    (function(){
        let swPanel; let swVisible = false; let marks = []; let t0 = 0;
        function fmt(ms){ return `${Math.round(ms)}ms`; }
        function build(){
            swPanel = document.createElement('div');
            swPanel.style.position='fixed'; swPanel.style.bottom='10px'; swPanel.style.right='10px';
            swPanel.style.background='rgba(0,0,0,0.85)'; swPanel.style.color='#fff'; swPanel.style.padding='10px 12px'; swPanel.style.borderRadius='8px'; swPanel.style.fontSize='12px'; swPanel.style.zIndex='2000';
            swPanel.style.maxWidth='480px'; swPanel.style.width='480px'; swPanel.style.display='none'; swPanel.style.maxHeight='60vh'; swPanel.style.overflow='auto';
            swPanel.innerHTML = '<div style="font-weight:bold;margin-bottom:6px;display:flex;align-items:center;gap:8px;">Stopwatch <button id="sw-reset" style="margin-left:auto;background:#1976d2;color:#fff;border:none;border-radius:4px;padding:2px 6px;cursor:pointer;">Reset</button><button id="sw-close" style="background:#555;color:#fff;border:none;border-radius:4px;padding:2px 6px;cursor:pointer;">×</button></div><div id="sw-content" style="white-space:pre;word-break:break-word;font-family:ui-monospace,Menlo,Consolas,monospace;"></div>';
            document.body.appendChild(swPanel);
            swPanel.querySelector('#sw-close').onclick = ()=> setVisible(false);
            swPanel.querySelector('#sw-reset').onclick = ()=> { marks = []; t0 = 0; render(); };
        }
        function setVisible(v){ swVisible = v; if (!swPanel) build(); swPanel.style.display = swVisible?'block':'none'; if (swVisible) render(); }
        function render(){
            if (!swPanel) return; const el = swPanel.querySelector('#sw-content'); if (!el) return;
            if (!marks.length){ el.textContent = '—'; return; }
            let out = ''; let prev = t0; marks.forEach((m,i)=>{ const d = m.t - prev; const tot = m.t - t0; out += `t${i} ${m.label}: ${fmt(tot)} (+${fmt(d)})\n`; prev = m.t; });
            out += `\nTotal: ${fmt(marks[marks.length-1].t - t0)}`;
            el.textContent = out;
        }
        function start(label){ t0 = performance.now(); marks = [{label: label||'click', t: t0}]; if (swVisible) render(); }
        function mark(label){ if (!t0) return; marks.push({label: label||'mark', t: performance.now()}); if (swVisible) render(); }
        window.__sw = { start, mark, show: setVisible };
        document.addEventListener('keydown', (ev)=>{ if (ev.key === 's' || ev.key === 'S') setVisible(!swVisible); });
    })();
    // Real user timing for Chrome (from navigation start to content complete state on Status page)
    (function(){
      try {
        const t0 = performance.now();
        const isStatus = location.pathname === '/' || document.title.indexOf('Статус') !== -1;
        let zonesDone = false;
        let statusDone = false;
        const marks = { navStart: performance.timing?.navigationStart || Date.now() };
        document.addEventListener('DOMContentLoaded', ()=>{ marks.domContentLoaded = performance.now(); });
        // Hook fetch to detect /api/status and /api/zones completions
        const _fetch = window.fetch;
        window.fetch = async function(input, init){
          const url = (typeof input === 'string') ? input : (input?.url || '');
          const started = performance.now();
          const resp = await _fetch(input, init);
          const done = performance.now();
          try{
            if (url.includes('/api/status')) { statusDone = true; marks.statusMs = Math.round(done - t0); }
            if (url.includes('/api/zones')) { zonesDone = true; marks.zonesFetchMs = Math.round(done - t0); }
          }catch(e){}
          return resp;
        }

        // Wait for client to signal that rows are rendered
        window.addEventListener('zones-rendered', (ev)=>{
          try{
            const done = performance.now();
            marks.rowsMs = Math.round(done - t0);
            if (isStatus && statusDone && zonesDone && !marks.allDone){
              marks.allDone = marks.rowsMs;
              console.log('[Perf] Status page: status=',marks.statusMs,'ms zonesFetch=',marks.zonesFetchMs,'ms rowsRendered=',marks.rowsMs,'ms');
              const footer = document.getElementById('footer-time');
              if (footer){ footer.textContent += ` | ⏱ ${marks.rowsMs}ms (статус ${marks.statusMs} / зоны ${marks.zonesFetchMs})`; }
            }
          }catch(e){}
        });
      } catch(e) {}
    })();
