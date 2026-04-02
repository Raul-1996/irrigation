// status/weather.js — Weather widget rendering and data fetching

    // --- Weather Widget (full) ---
    var WEATHER_ICONS = {
        0: '☀️', 1: '🌤️', 2: '⛅', 3: '☁️',
        45: '🌫️', 48: '🌫️',
        51: '🌦️', 53: '🌦️', 55: '🌧️',
        56: '🌧️', 57: '🌧️',
        61: '🌧️', 63: '🌧️', 65: '🌧️',
        66: '🌧️', 67: '🌧️',
        71: '❄️', 73: '❄️', 75: '❄️', 77: '❄️',
        80: '🌦️', 81: '🌧️', 82: '🌧️',
        85: '🌨️', 86: '🌨️',
        95: '⛈️', 96: '⛈️', 99: '⛈️'
    };
    var WEATHER_DESCS = {
        0: 'Ясно', 1: 'Малооблачно', 2: 'Переменная облачность', 3: 'Пасмурно',
        45: 'Туман', 48: 'Изморозь',
        51: 'Слабая морось', 53: 'Морось', 55: 'Сильная морось',
        56: 'Морось с заморозком', 57: 'Сильная морось с заморозком',
        61: 'Небольшой дождь', 63: 'Дождь', 65: 'Сильный дождь',
        66: 'Дождь с заморозком', 67: 'Сильный дождь с заморозком',
        71: 'Небольшой снег', 73: 'Снег', 75: 'Сильный снег', 77: 'Снежная крупа',
        80: 'Кратковр. дождь', 81: 'Ливень', 82: 'Сильный ливень',
        85: 'Снегопад', 86: 'Сильный снегопад',
        95: 'Гроза', 96: 'Гроза с градом', 99: 'Сильная гроза с градом'
    };

    function getWeatherIcon(code) {
        return WEATHER_ICONS[code] || '🌡️';
    }
    function getWeatherDesc(code) {
        return WEATHER_DESCS[code] || '';
    }
    function formatTemp(v) {
        if (v === null || v === undefined) return '—';
        var sign = v > 0 ? '+' : '';
        return sign + Math.round(v);
    }
    function coeffColor(c) {
        if (c === 0) return 'var(--danger-color, #f44336)';
        if (c < 80) return 'var(--warning-color, #ff9800)';
        if (c > 120) return 'var(--primary-color, #2196f3)';
        return 'var(--success-color, #4caf50)';
    }
    function formatCacheAge(sec) {
        if (!sec && sec !== 0) return '';
        if (sec < 60) return Math.round(sec) + ' сек назад';
        return Math.round(sec / 60) + ' мин назад';
    }
    function formatSourceLine(j) {
        var parts = [];
        var sensors = j.sensors || {};
        if (sensors.temperature && sensors.temperature.online) parts.push('📡 WB-MSW (t°)');
        else if (sensors.temperature && sensors.temperature.enabled && !sensors.temperature.online) parts.push('⚠️📡 WB-MSW (offline)');
        if (sensors.rain && sensors.rain.enabled) parts.push('📡 (🌧)');
        parts.push('🌐 Open-Meteo');
        if (j.cache_age_sec !== undefined && j.cache_age_sec !== null) parts.push(formatCacheAge(j.cache_age_sec));
        return parts.join(' · ');
    }

    function renderWeatherSummary(j) {
        var cur = j.current || {};
        var adj = j.adjustment || {};
        // Icon + desc
        var code = cur.weather_code;
        if (code === undefined || code === null) code = j.weather_code;
        var iconEl = document.getElementById('w-icon');
        var descEl = document.getElementById('w-desc');
        if (iconEl) iconEl.textContent = (code !== undefined && code !== null) ? getWeatherIcon(code) : '🌡️';
        if (descEl) descEl.textContent = (cur.weather_desc) ? cur.weather_desc : ((code !== undefined && code !== null) ? getWeatherDesc(code) : '');
        // Temperature
        var tempVal = (cur.temperature && cur.temperature.value !== undefined) ? cur.temperature.value : j.temperature;
        var tempEl = document.getElementById('w-temp');
        if (tempEl) tempEl.textContent = formatTemp(tempVal);
        // Coefficient
        var coeff = (adj.coefficient !== undefined) ? adj.coefficient : j.coefficient;
        var skip = (adj.skip !== undefined) ? adj.skip : j.skip;
        var coeffEl = document.getElementById('w-coeff');
        if (coeffEl) {
            if (skip) {
                coeffEl.textContent = 'SKIP';
                coeffEl.style.color = 'var(--danger-color, #f44336)';
                coeffEl.classList.add('skip');
            } else {
                coeffEl.textContent = (coeff !== null && coeff !== undefined) ? coeff + '%' : '—';
                coeffEl.style.color = coeffColor(coeff || 100);
                coeffEl.classList.remove('skip');
            }
        }
        // Metrics
        var humVal = (cur.humidity && cur.humidity.value !== undefined) ? cur.humidity.value : j.humidity;
        var windVal = (cur.wind_speed && cur.wind_speed.value !== undefined) ? cur.wind_speed.value : j.wind_speed;
        var precipVal = (j.stats && j.stats.precipitation_24h !== undefined) ? j.stats.precipitation_24h : j.precipitation_24h;
        var metricsEl = document.getElementById('w-metrics');
        if (metricsEl) {
            metricsEl.innerHTML = '<span>💧 ' + (humVal !== null && humVal !== undefined ? Math.round(humVal) + '%' : '—') + '</span>'
                + '<span>💨 ' + (windVal !== null && windVal !== undefined ? (typeof windVal === 'number' ? windVal.toFixed(1) : windVal) + ' м/с' : '—') + '</span>'
                + '<span>🌧 ' + (precipVal !== null && precipVal !== undefined ? (typeof precipVal === 'number' ? precipVal.toFixed(1) : precipVal) + ' мм' : '—') + '</span>';
        }
        // Source
        var srcEl = document.getElementById('w-source');
        if (srcEl) srcEl.textContent = formatSourceLine(j);
    }

    function renderForecast24h(items) {
        var el = document.getElementById('w-hours');
        if (!el) return;
        if (!items || !items.length) { el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Нет данных</div>'; return; }
        // Фильтруем до 6 интервалов (каждый 4-й час)
        var filtered = [];
        if (items.length >= 18) {
            for (var i = 0; i < items.length; i += 4) {
                filtered.push(items[i]);
                if (filtered.length >= 6) break;
            }
        } else {
            filtered = items.slice(0, 6);
        }
        var html = '';
        for (var i = 0; i < filtered.length; i++) {
            var it = filtered[i];
            var icon = it.icon || getWeatherIcon(it.weather_code);
            html += '<div class="hour-cell">'
                + '<div class="hour-time">' + (it.time || '') + '</div>'
                + '<div class="hour-icon">' + icon + '</div>'
                + '<div class="hour-temp">' + formatTemp(it.temp) + '°</div>'
                + '<div class="hour-detail">'
                + (it.precip != null ? (typeof it.precip === 'number' ? it.precip.toFixed(1) : it.precip) : '0') + 'мм · '
                + (it.wind != null ? (typeof it.wind === 'number' ? it.wind.toFixed(1) : it.wind) : '—') + 'м/с'
                + '</div></div>';
        }
        el.innerHTML = html;
    }

    function renderForecast3d(items) {
        var el = document.getElementById('w-days');
        if (!el) return;
        if (!items || !items.length) { el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Нет данных</div>'; return; }
        var html = '';
        for (var i = 0; i < items.length; i++) {
            var it = items[i];
            var icon = it.icon || getWeatherIcon(it.weather_code);
            html += '<div class="weather-day">'
                + '<span class="weather-day-dow">' + (it.day_name || '') + '</span>'
                + '<span class="weather-day-icon">' + icon + '</span>'
                + '<span class="weather-day-temps">' + formatTemp(it.temp_min) + '° / ' + formatTemp(it.temp_max) + '°</span>'
                + '<span class="weather-day-rain">🌧 ' + (it.precip_sum !== null && it.precip_sum !== undefined ? (typeof it.precip_sum === 'number' ? it.precip_sum.toFixed(1) : it.precip_sum) : '0') + ' мм</span>'
                + '</div>';
        }
        el.innerHTML = html;
    }

    function renderWeatherDetails(j) {
        var el = document.getElementById('w-details');
        if (!el) return;
        var stats = j.stats || {};
        var astro = j.astronomy || {};
        var precipFc = (stats.precipitation_forecast_6h !== undefined) ? stats.precipitation_forecast_6h : j.precipitation_forecast_6h;
        var precip24 = (stats.precipitation_24h !== undefined) ? stats.precipitation_24h : j.precipitation_24h;
        var et0 = (stats.daily_et0 !== undefined) ? stats.daily_et0 : j.daily_et0;
        var html = '<div class="weather-params">';
        if (astro.sunrise) html += '<span class="weather-params-label">🌅 Восход</span><span class="weather-params-val">' + astro.sunrise + '</span>';
        if (astro.sunset) html += '<span class="weather-params-label">🌇 Закат</span><span class="weather-params-val">' + astro.sunset + '</span>';
        html += '<span class="weather-params-label">🌧 Осадки 24ч</span><span class="weather-params-val">' + (precip24 !== null && precip24 !== undefined ? (typeof precip24 === 'number' ? precip24.toFixed(1) : precip24) + ' мм' : '—') + '</span>';
        html += '<span class="weather-params-label">🔮 Прогноз 6ч</span><span class="weather-params-val">' + (precipFc !== null && precipFc !== undefined ? (typeof precipFc === 'number' ? precipFc.toFixed(1) : precipFc) + ' мм' : '—') + '</span>';
        html += '<span class="weather-params-label">🔬 ET₀</span><span class="weather-params-val">' + (et0 !== null && et0 !== undefined ? (typeof et0 === 'number' ? et0.toFixed(2) : et0) + ' мм/день' : '—') + '</span>';
        html += '</div>';
        el.innerHTML = html;
    }

    function renderWeatherFactors(adj) {
        var el = document.getElementById('w-factors');
        if (!el) return;
        var factors = adj.factors || {};
        var factorNames = {
            rain: '🌧️ Дождь',
            heat: '🌡️ Жара',
            freeze: '❄️ Заморозок',
            wind: '💨 Ветер',
            humidity: '💧 Влажность'
        };
        var order = ['rain', 'heat', 'freeze', 'wind', 'humidity'];
        var html = '';
        for (var i = 0; i < order.length; i++) {
            var key = order[i];
            var f = factors[key];
            if (!f) continue;
            var statusCls = 'wf-ok';
            if (f.status === 'warn') statusCls = 'wf-warn';
            else if (f.status === 'danger') statusCls = 'wf-danger';
            var statusMark = f.status === 'ok' ? '✓' : (f.status === 'danger' ? '✕' : '⚠');
            html += '<div class="weather-factor">'
                + '<div class="weather-factor-row">'
                + '<span class="weather-factor-name">' + (factorNames[key] || key) + '</span>'
                + '<span class="weather-factor-status ' + statusCls + '">' + statusMark + ' ' + (f.detail || '') + '</span>'
                + '</div></div>';
        }
        // Summary line
        var coeff = adj.coefficient;
        var skip = adj.skip;
        if (coeff !== undefined || skip) {
            var summaryColor = skip ? 'var(--danger-color, #f44336)' : 'var(--success-color, #4caf50)';
            var summaryText = skip ? ('SKIP: ' + (adj.skip_reason || '')) : ('Коэффициент: ' + coeff + '%');
            html += '<div style="margin-top:0.5rem;padding:0.4rem;background:rgba(33,150,243,0.08);border-radius:6px;text-align:center;font-size:0.8rem;">'
                + '<strong style="color:' + summaryColor + ';">' + summaryText + '</strong></div>';
        }
        el.innerHTML = html;
    }

    async function renderWeatherHistory() {
        var el = document.getElementById('w-history');
        if (!el) return;
        try {
            var r = await fetch('/api/weather/decisions?days=7&limit=10');
            var j = await r.json();
            var items = (j && j.decisions) ? j.decisions : [];
            if (!items.length) {
                el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Нет данных</div>';
                return;
            }
            var html = '';
            for (var i = 0; i < items.length; i++) {
                var it = items[i];
                var date = (it.date || '').slice(5).replace('-', '.');
                var badgeCls = 'weather-badge-ok';
                var badgeText = 'ПОЛИВ';
                if (it.decision === 'skip' || it.decision === 'stop') {
                    badgeCls = 'weather-badge-skip';
                    badgeText = 'SKIP';
                } else if (it.decision === 'adjust' || (it.coefficient && it.coefficient < 100)) {
                    badgeCls = 'weather-badge-adj';
                    badgeText = it.coefficient + '%';
                } else if (it.decision === 'postpone') {
                    badgeCls = 'weather-badge-adj';
                    badgeText = 'ОТЛОЖЕН';
                }
                html += '<div class="weather-hist-item">'
                    + '<span class="weather-hist-date">' + date + '</span>'
                    + '<span class="weather-badge ' + badgeCls + '">' + badgeText + '</span>'
                    + '<span>' + (it.reason || '') + '</span>'
                    + '</div>';
            }
            el.innerHTML = html;
        } catch (e) {
            el.innerHTML = '<div style="color:#999;font-size:0.75rem;">Ошибка загрузки</div>';
        }
    }

    async function refreshWeatherWidget() {
        try {
            var r = await fetch('/api/weather');
            var j = await r.json();
            var widget = document.getElementById('weather-widget');
            if (!widget) return;
            if (!j || !j.available) { widget.style.display = 'none'; return; }
            widget.style.display = '';
            renderWeatherSummary(j);
            renderForecast24h(j.forecast_24h || []);
            renderForecast3d(j.forecast_3d || []);
            renderWeatherDetails(j);
            renderWeatherFactors(j.adjustment || {});
            renderWeatherHistory();
        } catch (e) { /* ignore weather fetch errors */ }
    }
    // Initial load + periodic refresh (every 5 min)
    refreshWeatherWidget();
    setInterval(refreshWeatherWidget, 5 * 60 * 1000);
