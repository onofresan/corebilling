/*
 * offline-core.js — Sistema compartido de modo offline para CoreBilling.
 *
 * Qué hace:
 * 1. Guarda en IndexedDB la última respuesta buena de cada endpoint GET
 *    (productos, clientes, habitaciones, reportes, etc.) para poder
 *    mostrarla si se cae el internet.
 * 2. Permite encolar facturas creadas en "Nueva Factura" mientras no hay
 *    conexión, y las sincroniza solas con el servidor apenas vuelve.
 * 3. Muestra un aviso fijo arriba de la pantalla indicando el estado
 *    (conectado / sin conexión / sincronizando / pendientes).
 * 4. Registra el Service Worker que cachea el "cascarón" de la app.
 *
 * Cómo se usa en cada página: solo hace falta incluir
 *   <script src="offline-core.js"></script>
 * y usar las funciones que quedan disponibles en window.CoreOffline.
 */
(function () {
    const DB_NAME = 'corebilling_offline';
    const DB_VERSION = 1;
    const STORE_CACHE = 'cache_datos';
    const STORE_COLA_FACTURAS = 'cola_facturas';

    let dbPromise = null;

    function abrirDB() {
        if (dbPromise) return dbPromise;
        dbPromise = new Promise((resolve, reject) => {
            const req = indexedDB.open(DB_NAME, DB_VERSION);
            req.onupgradeneeded = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains(STORE_CACHE)) {
                    db.createObjectStore(STORE_CACHE, { keyPath: 'key' });
                }
                if (!db.objectStoreNames.contains(STORE_COLA_FACTURAS)) {
                    db.createObjectStore(STORE_COLA_FACTURAS, { keyPath: 'idLocal', autoIncrement: true });
                }
            };
            req.onsuccess = (e) => resolve(e.target.result);
            req.onerror = (e) => reject(e.target.error);
        });
        return dbPromise;
    }

    async function guardarCache(key, data) {
        try {
            const db = await abrirDB();
            return new Promise((resolve, reject) => {
                const tx = db.transaction(STORE_CACHE, 'readwrite');
                tx.objectStore(STORE_CACHE).put({ key, data, fecha: new Date().toISOString() });
                tx.oncomplete = () => resolve();
                tx.onerror = () => reject(tx.error);
            });
        } catch (e) { /* IndexedDB no disponible: seguimos sin caché */ }
    }

    async function leerCache(key) {
        try {
            const db = await abrirDB();
            return new Promise((resolve, reject) => {
                const tx = db.transaction(STORE_CACHE, 'readonly');
                const req = tx.objectStore(STORE_CACHE).get(key);
                req.onsuccess = () => resolve(req.result || null);
                req.onerror = () => reject(req.error);
            });
        } catch (e) { return null; }
    }

    /**
     * Hace un fetch normal (GET) y guarda el resultado en caché.
     * Si falla por falta de conexión, devuelve el último dato bueno guardado.
     * Devuelve: { ok, data, offline, fecha }
     */
    async function fetchConCache(url, opciones, cacheKey) {
        try {
            const res = await fetch(url, opciones);
            if (res.ok) {
                const data = await res.clone().json();
                guardarCache(cacheKey, data);
                return { ok: true, data, offline: false };
            }
            throw new Error('Respuesta no OK: ' + res.status);
        } catch (err) {
            const cacheado = await leerCache(cacheKey);
            if (cacheado) {
                return { ok: true, data: cacheado.data, offline: true, fecha: cacheado.fecha };
            }
            return { ok: false, offline: true, error: err.message };
        }
    }

    async function encolarFactura(payload) {
        const db = await abrirDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_COLA_FACTURAS, 'readwrite');
            const req = tx.objectStore(STORE_COLA_FACTURAS).add({
                payload, fecha: new Date().toISOString(), intentos: 0
            });
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
    }

    async function obtenerColaFacturas() {
        try {
            const db = await abrirDB();
            return new Promise((resolve, reject) => {
                const tx = db.transaction(STORE_COLA_FACTURAS, 'readonly');
                const req = tx.objectStore(STORE_COLA_FACTURAS).getAll();
                req.onsuccess = () => resolve(req.result || []);
                req.onerror = () => reject(req.error);
            });
        } catch (e) { return []; }
    }

    async function eliminarDeCola(idLocal) {
        const db = await abrirDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_COLA_FACTURAS, 'readwrite');
            tx.objectStore(STORE_COLA_FACTURAS).delete(idLocal);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async function contarPendientes() {
        const cola = await obtenerColaFacturas();
        return cola.length;
    }

    let sincronizando = false;
    /**
     * Sube al servidor todas las facturas que se crearon sin conexión,
     * una por una y en orden. Llama a callbackResultado(info) por cada una.
     */
    async function sincronizarFacturasPendientes(apiUrl, token, callbackResultado) {
        if (sincronizando || !navigator.onLine) return;
        sincronizando = true;
        try {
            const cola = await obtenerColaFacturas();
            for (const item of cola) {
                try {
                    const res = await fetch(`${apiUrl}/facturas`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
                        body: JSON.stringify(item.payload)
                    });
                    if (res.ok) {
                        await eliminarDeCola(item.idLocal);
                        if (callbackResultado) callbackResultado({ exito: true, item });
                    } else {
                        const data = await res.json().catch(() => ({}));
                        if (callbackResultado) callbackResultado({ exito: false, item, error: data.error || 'Error al sincronizar', permanente: true });
                        await eliminarDeCola(item.idLocal); // error de negocio (ej: stock insuficiente) — no reintentar solo, requiere revisión
                    }
                } catch (err) {
                    // Se cayó la conexión de nuevo a mitad de camino: paramos y reintentamos luego.
                    break;
                }
            }
        } finally {
            sincronizando = false;
        }
    }

    function crearBanner() {
        let banner = document.getElementById('offlineBanner');
        if (banner) return banner;
        banner = document.createElement('div');
        banner.id = 'offlineBanner';
        banner.style.cssText = 'display:none; position:fixed; top:0; left:0; right:0; z-index:5000; text-align:center; padding:9px; font-weight:600; font-size:0.85rem; color:white; box-shadow:0 2px 8px rgba(0,0,0,0.15);';
        document.body.prepend(banner);
        return banner;
    }

    function actualizarBanner(mensaje, color) {
        const banner = crearBanner();
        banner.innerText = mensaje;
        banner.style.background = color;
        banner.style.display = 'block';
    }

    function ocultarBanner() {
        const banner = document.getElementById('offlineBanner');
        if (banner) banner.style.display = 'none';
    }

    window.CoreOffline = {
        fetchConCache,
        guardarCache,
        leerCache,
        encolarFactura,
        obtenerColaFacturas,
        eliminarDeCola,
        contarPendientes,
        sincronizarFacturasPendientes,
        actualizarBanner,
        ocultarBanner
    };

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js').catch((err) => {
                console.warn('No se pudo registrar el Service Worker:', err);
            });
        });
    }
})();
