// Service Worker for WB-Irrigation (network-first for HTML to avoid stale auth state)
const CACHE_NAME = 'wb-irrigation-__APP_VERSION__';
const urlsToCache = [
    // PWA assets: tiny, constant, required for install + offline boot.
    // Do NOT pre-cache '/' — navigations stay network-first.
    '/static/manifest.json',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/icons/icon-512-maskable.png',
];

// Install event
self.addEventListener('install', event => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE_NAME);
        console.log('Opened cache');
        await Promise.all(urlsToCache.map(u => fetch(u, {cache: 'no-store'}).then(r=>{
            if(!r.ok) throw new Error('bad response');
            return cache.put(u, r.clone());
        }).catch(()=>{})));
        // skipWaiting AFTER precache: otherwise the new SW could start serving
        // fetches with an empty cache and miss entries that should have been
        // primed (manifest.json + PWA icons).
        await self.skipWaiting();
    })());
});

// Fetch event: network-first for navigations/HTML, cache-first for others
self.addEventListener('fetch', event => {
    const req = event.request;
    const accept = req.headers.get('accept') || '';
    const isEventStream = accept.includes('text/event-stream');
    if (isEventStream) {
        // Never intercept SSE: stream directly from network
        event.respondWith(fetch(req, { cache: 'no-store' }));
        return;
    }
    const isNavigation = req.mode === 'navigate' || accept.includes('text/html');
    const url = new URL(req.url);
    const isApi = url.pathname.startsWith('/api/');
    if (isApi) {
        // Network-first for API to avoid stale state after actions (e.g., cancel postpone)
        event.respondWith(
            fetch(req, { cache: 'no-store' })
                .then(resp => resp)
                .catch(() => caches.match(req))
        );
        return;
    }
    if (isNavigation) {
        // Network-first: live request always reaches the server, so auth/role
        // is always re-evaluated. The cache.put below is an OFFLINE-ONLY
        // fallback — only consulted in .catch() when the network fails.
        event.respondWith(
            fetch(req)
                .then(resp => {
                    const copy = resp.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(req, copy)).catch(()=>{});
                    return resp;
                })
                .catch(() => caches.match(req))
        );
        return;
    }
    event.respondWith(
        caches.match(req).then(cacheResp => {
            return cacheResp || fetch(req).then(networkResp => {
                // Cache a copy of non-HTML requests
                const copy = networkResp.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(req, copy)).catch(()=>{});
                return networkResp;
            });
        })
    );
});

// Activate event
self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const cacheNames = await caches.keys();
        await Promise.all(cacheNames.map(name => {
            if (name !== CACHE_NAME) {
                console.log('Deleting old cache:', name);
                return caches.delete(name);
            }
        }));
        // Take control of already-open pages so the next fetch goes through this SW.
        await self.clients.claim();
    })());
});
