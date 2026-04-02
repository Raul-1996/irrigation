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

    // Загрузка и обновление данных статуса
    var statusData = null;
    var zonesData = [];
    var connectionError = false;
    var mqttNoServers = false;
    var mqttNoConnection = false;
    var envProbeTimer = null;
    var envProbeAttempts = 0;
    
    // Функция обновления времени (локальное, без fetch)
    var _serverTimeOffset = 0;
