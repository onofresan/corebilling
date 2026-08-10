/*
 * sw.js — Service Worker de CoreBilling
 *
 * Cachea el "cascarón" de la aplicación (páginas HTML, CSS, JS, íconos)
 * para que se puedan ABRIR aunque no haya internet. Las llamadas a /api/
 * y /socket.io/ NUNCA se cachean — siempre van directo a la red, porque
 * son datos que cambian todo el tiempo (eso lo maneja offline-core.js
 * por separado, con IndexedDB).
 */

const CACHE_NAME = 'corebilling-shell-v1';

const ARCHIVOS_CASCARON = [
    '/',
    '/estilos.css',
    '/manifest.json',
    '/offline-core.js',
    '/login.html',
    '/index.html',
    '/facturar.html',
    '/hoteleria.html',
    '/inventario.html',
    '/reporte.html',
    '/lista_facturas.html',
    '/historial_inventario.html',
    '/clientes.html',
    '/static/icon-192.png',
    '/static/icon-512.png'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return Promise.all(
                ARCHIVOS_CASCARON.map((url) => cache.add(url).catch(() => {}))
            );
        }).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((nombres) =>
            Promise.all(nombres.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // La API y los websockets de socket.io SIEMPRE van directo a la red.
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/socket.io/')) {
        return;
    }

    // Solo nos encargamos de peticiones GET del propio sitio.
    if (event.request.method !== 'GET' || url.origin !== self.location.origin) {
        return;
    }

    event.respondWith(
        fetch(event.request)
            .then((respuestaRed) => {
                // Solo guardamos en caché respuestas realmente buenas (200 OK).
                // Guardar una respuesta de error (404, 500) o dañada dejaría
                // ese error "atrapado" en el modo offline, sirviéndolo de
                // nuevo más tarde en vez de la página real.
                if (respuestaRed && respuestaRed.ok) {
                    const copia = respuestaRed.clone();
                    caches.open(CACHE_NAME)
                        .then((cache) => cache.put(event.request, copia))
                        .catch(() => {
                            // Guardar en caché es solo una optimización para
                            // el modo offline — si falla (ej: respuesta
                            // dañada por un proxy/extensión intermedia), no
                            // debe romper nada ni generar errores sin atrapar.
                        });
                }
                return respuestaRed;
            })
            .catch(() => {
                return caches.match(event.request).then((respuestaCache) => {
                    return respuestaCache || caches.match('/login.html');
                });
            })
    );
});
