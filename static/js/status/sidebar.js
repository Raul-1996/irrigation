// status/sidebar.js — Sidebar: active zone indicator, water meter, toggle

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
            var end = parseDate(active.planned_end_time);
            if (end) {
            var now = new Date();
            var remain = Math.max(0, Math.floor((end - now) / 1000));
            var mins = Math.floor(remain / 60);
            var secs = remain % 60;
            timerEl.innerHTML = 'осталось <strong>' + mins + ':' + (secs < 10 ? '0' : '') + secs + '</strong>';
            // Progress
            if (active.watering_start_time && progressEl) {
                var start = parseDate(active.watering_start_time);
                if (start) {
                var total = (end - start) / 1000;
                var elapsed = (now - start) / 1000;
                var pct = Math.min(100, Math.max(0, (elapsed / total) * 100));
                progressEl.style.width = pct + '%';
                }
            }
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
    function updateWaterMeter(zones) {
        var el = document.getElementById('sidebar-water-meter');
        if (!el) return;
        var total = 0;
        var perZone = [];
        zones.forEach(function(z) {
            if (z.last_total_liters > 0) {
                total += z.last_total_liters;
                perZone.push({name: z.name, liters: z.last_total_liters});
            }
        });
        if (total === 0) {
            el.style.display = 'none';
            return;
        }
        el.style.display = '';
        var valEl = document.getElementById('water-meter-value');
        var detEl = document.getElementById('water-meter-detail');
        if (valEl) valEl.innerHTML = Math.round(total).toLocaleString() + ' <span class="unit">л</span>';
        if (detEl) {
            perZone.sort(function(a,b) { return b.liters - a.liters; });
            detEl.innerHTML = perZone.slice(0, 3).map(function(z) {
                return '<span>' + escapeHtml(z.name) + ': ' + Math.round(z.liters) + 'л</span>';
            }).join('');
        }
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
