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
    const DB_VERSION = 2;
    const STORE_CACHE = 'cache_datos';
    const STORE_COLA_FACTURAS = 'cola_facturas';
    const STORE_FACTURAS_FALLIDAS = 'facturas_fallidas';

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
                if (!db.objectStoreNames.contains(STORE_FACTURAS_FALLIDAS)) {
                    db.createObjectStore(STORE_FACTURAS_FALLIDAS, { keyPath: 'idLocal', autoIncrement: true });
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
        // Si el navegador ya sabe que no hay conexión, ni intentamos la red:
        // vamos directo a la caché, para que se sienta instantáneo en vez de
        // esperar a que el fetch falle solo.
        if (!navigator.onLine) {
            const cacheado = await leerCache(cacheKey);
            if (cacheado) return { ok: true, data: cacheado.data, offline: true, fecha: cacheado.fecha };
            return { ok: false, offline: true, error: 'Sin conexión y sin datos guardados' };
        }

        try {
            // Límite de 4 segundos: si el navegador dice que hay conexión pero
            // en realidad no responde (ej: proxy caído, wifi sin internet real),
            // no queremos esperar el timeout lento por defecto del navegador
            // (puede tardar 20-30+ segundos) — a los 4s caemos a la caché.
            const controlador = new AbortController();
            const timeoutId = setTimeout(() => controlador.abort(), 4000);
            const res = await fetch(url, { ...opciones, signal: controlador.signal });
            clearTimeout(timeoutId);

            if (res.ok) {
                const data = await res.clone().json();
                guardarCache(cacheKey, data);
                return { ok: true, data, offline: false };
            }

            // ⚠️ IMPORTANTE: un 401/403 significa que SÍ hay conexión, pero la
            // sesión ya no es válida (token vencido, o el servidor se reinició
            // con una clave distinta). Esto NO es un problema de conexión —
            // antes se trataba igual que "sin internet" y mostraba el aviso
            // equivocado de "sin conexión/reconectando" en vez de mandar a
            // iniciar sesión de nuevo.
            if (res.status === 401 || res.status === 403) {
                return { ok: false, offline: false, sesionInvalida: true, error: 'Sesión inválida o expirada' };
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

    async function moverAFallidas(item, mensajeError) {
        const db = await abrirDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction([STORE_COLA_FACTURAS, STORE_FACTURAS_FALLIDAS], 'readwrite');
            tx.objectStore(STORE_COLA_FACTURAS).delete(item.idLocal);
            tx.objectStore(STORE_FACTURAS_FALLIDAS).add({
                payload: item.payload,
                fecha: item.fecha,
                fechaFallo: new Date().toISOString(),
                error: mensajeError
            });
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async function obtenerFacturasFallidas() {
        try {
            const db = await abrirDB();
            return new Promise((resolve, reject) => {
                const tx = db.transaction(STORE_FACTURAS_FALLIDAS, 'readonly');
                const req = tx.objectStore(STORE_FACTURAS_FALLIDAS).getAll();
                req.onsuccess = () => resolve(req.result || []);
                req.onerror = () => reject(req.error);
            });
        } catch (e) { return []; }
    }

    async function eliminarFacturaFallida(idLocal) {
        const db = await abrirDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_FACTURAS_FALLIDAS, 'readwrite');
            tx.objectStore(STORE_FACTURAS_FALLIDAS).delete(idLocal);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    /** Vuelve a poner una factura fallida en la cola normal para reintentar sincronizarla. */
    async function reintentarFacturaFallida(idLocal) {
        const db = await abrirDB();
        const item = await new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_FACTURAS_FALLIDAS, 'readonly');
            const req = tx.objectStore(STORE_FACTURAS_FALLIDAS).get(idLocal);
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
        if (!item) return false;
        await encolarFactura(item.payload);
        await eliminarFacturaFallida(idLocal);
        return true;
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
                        const mensajeError = data.error || 'Error al sincronizar';
                        await moverAFallidas(item, mensajeError);
                        if (callbackResultado) callbackResultado({ exito: false, item, error: mensajeError, permanente: true });
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

    /**
     * Revisa el resultado de fetchConCache: si la sesión ya no es válida
     * (token vencido / servidor reiniciado con otra clave), limpia el token
     * y redirige a login.html. Devuelve true si redirigió (para que el
     * código que llama pueda hacer un `return` inmediatamente después).
     */
    function manejarSesionInvalida(resultado) {
        if (resultado && resultado.sesionInvalida) {
            localStorage.removeItem('token');
            window.location.href = 'login.html';
            return true;
        }
        return false;
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
        obtenerFacturasFallidas,
        eliminarFacturaFallida,
        reintentarFacturaFallida,
        actualizarBanner,
        ocultarBanner,
        manejarSesionInvalida
    };

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js').catch((err) => {
                console.warn('No se pudo registrar el Service Worker:', err);
            });
        });
    }
})();
