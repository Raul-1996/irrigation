// Service Worker for WB-Irrigation (network-first for HTML to avoid stale auth state)
const CACHE_NAME = 'wb-irrigation-v4';
const urlsToCache = [
    // cache only static assets here if needed; do NOT pre-cache '/'
];

// Install event
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => {
                console.log('Opened cache');
                return Promise.all(urlsToCache.map(u => fetch(u, {cache: 'no-store'}).then(r=>{
                    if(!r.ok) throw new Error('bad response');
                    return cache.put(u, r.clone());
                }).catch(()=>{})));
            })
    );
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
        event.respondWith(
            fetch(req)
                .then(resp => {
                    // Optionally update cache in background
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
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(cacheName => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('Deleting old cache:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
});
