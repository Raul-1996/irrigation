// status/ui-helpers.js — Loading overlay, toast, photo modal, UI timing

    // UI timing helpers
    (function(){
      function nowMs(){ return performance && performance.now ? performance.now() : Date.now(); }
      function logUiTiming(kind, detail, ms){
        try{
          console.log(`[UI Timing] ${kind} ${detail}: ${Math.round(ms)}ms`);
        }catch(e){}
      }
      // Wrap fetch to time control actions
      const _fetch = window.fetch;
      window.fetch = async function(input, init){
        const url = (typeof input === 'string') ? input : (input && input.url) || '';
        const isControl = /\/api\/(zones\/.+\/(mqtt\/)?(start|stop)|groups\/\d+\/(start-from-first|stop)|emergency-(stop|resume)|postpone)/.test(url);
        const t0 = nowMs();
        const resp = await _fetch(input, init);
        const t1 = nowMs();
        if (isControl){ logUiTiming('HTTP', url, t1 - t0); }
        return resp;
      };
      // Time button clicks to response end
      function wireBtnTiming(){
        const btnSelectors = [
          '.zone-start-btn', '#emergency-btn', '#resume-btn',
        ];
        btnSelectors.forEach(sel=>{
          document.querySelectorAll(sel).forEach(btn=>{
            if (btn.__timed) return; btn.__timed = true;
            btn.addEventListener('click', ()=>{ btn.__t0 = nowMs(); }, {capture:true});
          });
        });
        // Generic listener to measure end of network roundtrip via DOM updates
        document.addEventListener('zones-rendered', ()=>{
          try{
            const tNow = nowMs();
            document.querySelectorAll('.zone-start-btn').forEach(b=>{
              if (b.__t0){ logUiTiming('UI', 'zone-toggle->render', tNow - b.__t0); b.__t0 = null; }
            });
          }catch(e){}
        });
      }
      document.addEventListener('DOMContentLoaded', wireBtnTiming);
    })();

    // Модальное окно для просмотра фотографий
    function showPhotoModal(photoUrl) {
        const img = document.getElementById('photoModalImg');
        img.src = photoUrl;
        const modal = document.getElementById('photoModal');
        modal.style.display = 'flex'; // чтобы сработало центрирование по flex
    }

    function closePhotoModal() {
        document.getElementById('photoModal').style.display = 'none';
    }

    // Loading overlay
    var _loadingTimer = null;
    function showLoading(text) {
        var el = document.getElementById('loadingOverlay');
        var txt = document.getElementById('loadingText');
        if (txt) txt.textContent = text || 'Загрузка...';
        if (el) el.classList.add('show');
        clearTimeout(_loadingTimer);
        _loadingTimer = setTimeout(hideLoading, 15000); // safety: auto-hide after 15s
    }
    function hideLoading() {
        var el = document.getElementById('loadingOverlay');
        if (el) el.classList.remove('show');
    }
    window.showLoading = showLoading;
    window.hideLoading = hideLoading;

    // Toast
    function showZoneToast(msg, type) {
        var t = document.getElementById('zoneToast');
        if (!t) return;
        t.textContent = msg;
        t.className = 'zone-toast show' + (type ? ' ' + type : '');
        clearTimeout(t._timer);
        t._timer = setTimeout(function() { t.className = 'zone-toast'; }, 2500);
    }
