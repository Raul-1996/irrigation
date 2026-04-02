/**
 * zones-core.js — Shared state, modal utilities, helpers
 * Part of zones.js decomposition
 */

// ===== Shared state (var for cross-script access) =====
var zonesData = [];
var groupsData = [];
var modifiedZones = new Set();
var modifiedGroups = new Set();
var editingGroupId = null;
var sortColumn = -1;
var sortDirection = 'asc';

// ===== Modal utilities =====
function _calcViewport(){
    const vv = (window.visualViewport ? { w: window.visualViewport.width, h: window.visualViewport.height } : null);
    return {
        w: Math.max(320, Math.min(window.innerWidth || 0, screen.width || Infinity, vv ? vv.w : Infinity)),
        h: Math.max(320, Math.min(window.innerHeight || 0, screen.height || Infinity, vv ? vv.h : Infinity))
    };
}

function _sizeModalContent(modal){
    try{
        const c = modal.querySelector('.modal-content'); if (!c) return;
        const vp = _calcViewport();
        const maxW = Math.min(520, vp.w - 32);
        const widthPx = Math.max(280, maxW);
        const maxH = Math.min(Math.round(vp.h*0.92), vp.h - 32);
        c.style.margin = '0';
        c.style.width = widthPx + 'px';
        c.style.maxWidth = widthPx + 'px';
        c.style.maxHeight = maxH + 'px';
    }catch(e){}
}

function openModalById(id){
    const m = document.getElementById(id); if (!m) return;
    try { if (m.parentElement && m.parentElement !== document.body) { document.body.appendChild(m); } } catch(e){}
    m.style.display = 'flex';
    _sizeModalContent(m);
}

function closeModalById(id){ const m = document.getElementById(id); if (!m) return; m.style.display='none'; }

function recenterOpenModals(){
    try{
        document.querySelectorAll('.modal').forEach(m=>{
            if (m instanceof HTMLElement && getComputedStyle(m).display !== 'none') _sizeModalContent(m);
        });
    }catch(e){}
}

window.addEventListener('resize', recenterOpenModals);
window.addEventListener('orientationchange', recenterOpenModals);
if (window.visualViewport){ try{ window.visualViewport.addEventListener('resize', recenterOpenModals); }catch(e){} }

// ===== Helpers =====
function toTimeString(totalMinutes) {
    const h = Math.floor(totalMinutes / 60) % 24;
    const m = totalMinutes % 60;
    return `${(''+h).padStart(2,'0')}:${(''+m).padStart(2,'0')}`;
}
