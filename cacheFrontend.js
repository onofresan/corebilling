// ================================================================
// cacheFrontend.js - Caché en el navegador para CoreBilling
// ================================================================
// Este archivo proporciona funciones para cachear datos en el 
// navegador del usuario, reduciendo las peticiones al servidor
// y haciendo la aplicación más rápida.
// ================================================================

// ========== CONFIGURACIÓN ==========
const CONFIG = {
    // Tiempos de expiración en minutos para cada tipo de dato
    productos: { minutos: 2, url: '/api/productos' },
    habitaciones: { minutos: 2, url: '/api/habitaciones' },
    clientes: { minutos: 5, url: '/api/clientes' },
    categorias: { minutos: 10, url: '/api/categorias' },
    servicios: { minutos: 10, url: '/api/servicios' },
    proveedores: { minutos: 5, url: '/api/proveedores' },
    kits: { minutos: 5, url: '/api/kits' },
    recetas: { minutos: 5, url: '/api/recetas' },
    empresa: { minutos: 30, url: '/api/empresa' },
    reservas: { minutos: 1, url: '/api/reservas' },  // Cambia rápido
    ordenes_compra: { minutos: 2, url: '/api/ordenes-compra' },
};

// ========== FUNCIONES AUXILIARES ==========

function obtenerToken() {
    return localStorage.getItem('token') || '';
}

function obtenerEmpresaId() {
    return localStorage.getItem('empresa_id') || 'default';
}

function getCacheKey(tipo) {
    const empresaId = obtenerEmpresaId();
    return `cache_${tipo}_${empresaId}`;
}

function getConfig(tipo) {
    return CONFIG[tipo] || { minutos: 5, url: `/api/${tipo}` };
}

// ========== FUNCIONES PRINCIPALES DE CACHE ==========

/**
 * Guarda datos en el caché del navegador (localStorage)
 * @param {string} tipo - Tipo de dato (productos, habitaciones, etc.)
 * @param {any} datos - Datos a guardar
 * @param {number|null} minutosOverride - Minutos de expiración (opcional)
 * @returns {boolean} - True si se guardó correctamente
 */
export function guardarEnCache(tipo, datos, minutosOverride = null) {
    try {
        const key = getCacheKey(tipo);
        const config = getConfig(tipo);
        const minutos = minutosOverride || config.minutos || 5;
        
        const item = {
            datos: datos,
            timestamp: Date.now(),
            expiracion: minutos * 60 * 1000,  // Convertir a milisegundos
            tipo: tipo
        };
        
        localStorage.setItem(key, JSON.stringify(item));
        console.log(`✅ Cache guardado: ${tipo} (expira en ${minutos} min)`);
        return true;
    } catch (e) {
        console.warn(`⚠️ Error guardando cache de ${tipo}:`, e);
        return false;
    }
}

/**
 * Obtiene datos del caché del navegador si no han expirado
 * @param {string} tipo - Tipo de dato
 * @returns {any|null} - Datos del cache o null si expiró/no existe
 */
export function obtenerDelCache(tipo) {
    try {
        const key = getCacheKey(tipo);
        const itemStr = localStorage.getItem(key);
        
        if (!itemStr) {
            return null;
        }
        
        const item = JSON.parse(itemStr);
        const ahora = Date.now();
        
        // Verificar si expiró
        if (ahora - item.timestamp > item.expiracion) {
            localStorage.removeItem(key);
            console.log(`⏰ Cache expirado: ${tipo}`);
            return null;
        }
        
        console.log(`📦 Usando cache local: ${tipo} (${Math.round((ahora - item.timestamp) / 60000)} min de ${Math.round(item.expiracion / 60000)} min)`);
        return item.datos;
    } catch (e) {
        console.warn(`⚠️ Error obteniendo cache de ${tipo}:`, e);
        return null;
    }
}

/**
 * Verifica si hay cache válido para un tipo
 * @param {string} tipo - Tipo de dato
 * @returns {boolean} - True si hay cache válido
 */
export function tieneCache(tipo) {
    try {
        const key = getCacheKey(tipo);
        const itemStr = localStorage.getItem(key);
        if (!itemStr) return false;
        
        const item = JSON.parse(itemStr);
        const ahora = Date.now();
        return (ahora - item.timestamp <= item.expiracion);
    } catch (e) {
        return false;
    }
}

/**
 * Invalida el caché de un tipo específico o de todos
 * @param {string|null} tipo - Tipo a invalidar o null para todos
 */
export function invalidarCacheLocal(tipo = null) {
    try {
        if (tipo) {
            const key = getCacheKey(tipo);
            localStorage.removeItem(key);
            console.log(`🧹 Cache invalidado: ${tipo}`);
        } else {
            // Invalidar todo el cache de la app
            const keys = Object.keys(localStorage);
            let count = 0;
            keys.forEach(key => {
                if (key.startsWith('cache_')) {
                    localStorage.removeItem(key);
                    count++;
                }
            });
            console.log(`🧹 Todo el cache invalidado (${count} items)`);
        }
    } catch (e) {
        console.warn('⚠️ Error invalidando cache:', e);
    }
}

// ========== FUNCIÓN PRINCIPAL PARA OBTENER DATOS CON CACHE ==========

/**
 * Obtiene datos con caché automático. Primero busca en localStorage,
 * si no hay o expiró, hace la petición al servidor y guarda en cache.
 * @param {string} tipo - Tipo de dato
 * @param {boolean} forceRefresh - Si es true, ignora el cache y fuerza petición
 * @param {Object} options - Opciones adicionales para fetch
 * @returns {Promise<{data: any, fromCache: boolean}>}
 */
export async function obtenerDatosConCache(tipo, forceRefresh = false, options = {}) {
    // 1. Intentar obtener del caché del navegador (si no es force refresh)
    if (!forceRefresh) {
        const cacheData = obtenerDelCache(tipo);
        if (cacheData !== null) {
            return { data: cacheData, fromCache: true };
        }
    }
    
    // 2. No hay cache, hacer petición al servidor
    const config = getConfig(tipo);
    const url = config.url || `/api/${tipo}`;
    
    console.log(`🌐 Cargando ${tipo} desde el servidor...`);
    
    try {
        const response = await fetch(url, {
            ...options,
            headers: {
                'Authorization': `Bearer ${obtenerToken()}`,
                'Content-Type': 'application/json',
                ...(options.headers || {})
            }
        });
        
        if (!response.ok) {
            throw new Error(`Error ${response.status}: ${response.statusText}`);
        }
        
        const data = await response.json();
        
        // 3. Guardar en caché del navegador (solo si la respuesta es exitosa)
        if (data && !data.error) {
            guardarEnCache(tipo, data);
        }
        
        return { data, fromCache: false };
    } catch (error) {
        console.error(`❌ Error cargando ${tipo}:`, error);
        
        // Si hay error, intentar devolver cache antiguo aunque esté expirado
        try {
            const key = getCacheKey(tipo);
            const itemStr = localStorage.getItem(key);
            if (itemStr) {
                const item = JSON.parse(itemStr);
                console.warn(`⚠️ Usando cache expirado para ${tipo} por error de red`);
                return { data: item.datos, fromCache: true, expired: true };
            }
        } catch (e) {}
        
        throw error;
    }
}

// ========== FUNCIONES ESPECÍFICAS PARA CADA TIPO ==========

export async function getProductos(forceRefresh = false) {
    const result = await obtenerDatosConCache('productos', forceRefresh);
    return result.data;
}

export async function getHabitaciones(forceRefresh = false) {
    const result = await obtenerDatosConCache('habitaciones', forceRefresh);
    return result.data;
}

export async function getClientes(forceRefresh = false) {
    const result = await obtenerDatosConCache('clientes', forceRefresh);
    return result.data;
}

export async function getCategorias(forceRefresh = false) {
    const result = await obtenerDatosConCache('categorias', forceRefresh);
    return result.data;
}

export async function getServicios(forceRefresh = false) {
    const result = await obtenerDatosConCache('servicios', forceRefresh);
    return result.data;
}

export async function getProveedores(forceRefresh = false) {
    const result = await obtenerDatosConCache('proveedores', forceRefresh);
    return result.data;
}

export async function getKits(forceRefresh = false) {
    const result = await obtenerDatosConCache('kits', forceRefresh);
    return result.data;
}

export async function getRecetas(forceRefresh = false) {
    const result = await obtenerDatosConCache('recetas', forceRefresh);
    return result.data;
}

export async function getEmpresa(forceRefresh = false) {
    const result = await obtenerDatosConCache('empresa', forceRefresh);
    return result.data;
}

export async function getReservas(forceRefresh = false) {
    const result = await obtenerDatosConCache('reservas', forceRefresh);
    return result.data;
}

export async function getOrdenesCompra(forceRefresh = false) {
    const result = await obtenerDatosConCache('ordenes_compra', forceRefresh);
    return result.data;
}

// ========== FUNCIONES PARA INVALIDAR CACHE DESPUÉS DE MODIFICACIONES ==========

// Llamar después de agregar/editar/eliminar un producto
export function invalidarProductos() {
    invalidarCacheLocal('productos');
    invalidarCacheLocal('recetas');
    invalidarCacheLocal('kits');
    console.log('🔄 Cache de productos, recetas y kits invalidado');
}

// Llamar después de agregar/editar/eliminar una habitación
export function invalidarHabitaciones() {
    invalidarCacheLocal('habitaciones');
    invalidarCacheLocal('productos');
    console.log('🔄 Cache de habitaciones y productos invalidado');
}

// Llamar después de una factura
export function invalidarVentas() {
    invalidarCacheLocal('productos');
    invalidarCacheLocal('habitaciones');
    invalidarCacheLocal('reservas');
    console.log('🔄 Cache de ventas invalidado (productos, habitaciones, reservas)');
}

// Llamar después de agregar/editar/eliminar un cliente
export function invalidarClientes() {
    invalidarCacheLocal('clientes');
    console.log('🔄 Cache de clientes invalidado');
}

// Llamar después de agregar/editar/eliminar un servicio
export function invalidarServicios() {
    invalidarCacheLocal('servicios');
    console.log('🔄 Cache de servicios invalidado');
}

// Llamar después de agregar/editar/eliminar un proveedor
export function invalidarProveedores() {
    invalidarCacheLocal('proveedores');
    invalidarCacheLocal('ordenes_compra');
    console.log('🔄 Cache de proveedores y órdenes de compra invalidado');
}

// Llamar después de agregar/editar/eliminar una categoría
export function invalidarCategorias() {
    invalidarCacheLocal('categorias');
    invalidarCacheLocal('productos');
    console.log('🔄 Cache de categorías y productos invalidado');
}

// ========== CARGA INICIAL RÁPIDA - TODOS LOS DATOS DE UNA VEZ ==========

/**
 * Carga todos los datos iniciales en paralelo usando caché
 * @param {boolean} forceRefresh - Si es true, ignora todo el cache
 * @returns {Promise<Object>} - Datos de todos los tipos
 */
export async function cargarDatosIniciales(forceRefresh = false) {
    console.log('🚀 Cargando datos iniciales...');
    
    const startTime = Date.now();
    
    const promesas = [
        getProductos(forceRefresh).catch(() => []),
        getHabitaciones(forceRefresh).catch(() => []),
        getClientes(forceRefresh).catch(() => []),
        getCategorias(forceRefresh).catch(() => []),
        getServicios(forceRefresh).catch(() => []),
        getEmpresa(forceRefresh).catch(() => ({})),
        getProveedores(forceRefresh).catch(() => []),
        getKits(forceRefresh).catch(() => []),
        getRecetas(forceRefresh).catch(() => []),
    ];
    
    // Cargar reservas solo si tenemos token
    const token = obtenerToken();
    let reservasPromise = Promise.resolve([]);
    if (token) {
        reservasPromise = getReservas(forceRefresh).catch(() => []);
    }
    promesas.push(reservasPromise);
    
    const [productos, habitaciones, clientes, categorias, servicios, empresa, proveedores, kits, recetas, reservas] = await Promise.all(promesas);
    
    const elapsed = Date.now() - startTime;
    console.log(`✅ Datos iniciales cargados en ${elapsed}ms`);
    
    return {
        productos,
        habitaciones,
        clientes,
        categorias,
        servicios,
        empresa,
        proveedores,
        kits,
        recetas,
        reservas,
        tiempos: {
            total: elapsed,
            desdeCache: {
                productos: tieneCache('productos'),
                habitaciones: tieneCache('habitaciones'),
                clientes: tieneCache('clientes'),
                categorias: tieneCache('categorias'),
                servicios: tieneCache('servicios'),
                proveedores: tieneCache('proveedores'),
                kits: tieneCache('kits'),
                recetas: tieneCache('recetas'),
                reservas: tieneCache('reservas'),
            }
        },
        cargadoDesdeCache: tieneCache('productos') && tieneCache('habitaciones') && tieneCache('clientes')
    };
}

// ========== CARGA PERESOZA (Lazy Loading) ==========

// Cache en memoria para carga perezosa (solo en la sesión actual)
let datosCargados = {};

/**
 * Carga un tipo de dato solo si no está ya cargado en memoria
 * @param {string} tipo - Tipo de dato
 * @param {boolean} forceRefresh - Forzar recarga
 * @returns {Promise<any>}
 */
export async function cargarSiNoExiste(tipo, forceRefresh = false) {
    if (!forceRefresh && datosCargados[tipo]) {
        console.log(`📦 Usando datos en memoria: ${tipo}`);
        return datosCargados[tipo];
    }
    
    try {
        const result = await obtenerDatosConCache(tipo, forceRefresh);
        datosCargados[tipo] = result.data;
        return result.data;
    } catch (error) {
        console.error(`Error cargando ${tipo}:`, error);
        return null;
    }
}

/**
 * Limpia los datos cargados en memoria (no el localStorage)
 * @param {string|null} tipo - Tipo a limpiar o null para todos
 */
export function limpiarDatosMemoria(tipo = null) {
    if (tipo) {
        delete datosCargados[tipo];
        console.log(`🧹 Datos en memoria limpiados: ${tipo}`);
    } else {
        datosCargados = {};
        console.log('🧹 Todos los datos en memoria limpiados');
    }
}

// ========== CONFIGURACIÓN PARA PRODUCCIÓN (RENDER) ==========

/**
 * Configura tiempos de cache más largos para producción
 */
export function configurarParaProduccion() {
    Object.assign(CONFIG, {
        productos: { minutos: 5, url: '/api/productos' },
        habitaciones: { minutos: 5, url: '/api/habitaciones' },
        clientes: { minutos: 10, url: '/api/clientes' },
        categorias: { minutos: 15, url: '/api/categorias' },
        servicios: { minutos: 15, url: '/api/servicios' },
        proveedores: { minutos: 10, url: '/api/proveedores' },
        kits: { minutos: 10, url: '/api/kits' },
        recetas: { minutos: 10, url: '/api/recetas' },
        empresa: { minutos: 60, url: '/api/empresa' },
        reservas: { minutos: 2, url: '/api/reservas' },
        ordenes_compra: { minutos: 5, url: '/api/ordenes-compra' },
    });
    console.log('⚙️ Configuración de caché para PRODUCCIÓN activada');
}

// Detectar si estamos en Render y configurar automáticamente
if (typeof window !== 'undefined' && window.location && window.location.hostname && window.location.hostname.includes('onrender.com')) {
    configurarParaProduccion();
}

// ========== EXPORTAR TODO ==========
export default {
    // Funciones principales
    guardarEnCache,
    obtenerDelCache,
    tieneCache,
    invalidarCacheLocal,
    obtenerDatosConCache,
    cargarDatosIniciales,
    cargarSiNoExiste,
    limpiarDatosMemoria,
    configurarParaProduccion,
    
    // Getters específicos
    getProductos,
    getHabitaciones,
    getClientes,
    getCategorias,
    getServicios,
    getProveedores,
    getKits,
    getRecetas,
    getEmpresa,
    getReservas,
    getOrdenesCompra,
    
    // Invalidaciones específicas
    invalidarProductos,
    invalidarHabitaciones,
    invalidarVentas,
    invalidarClientes,
    invalidarServicios,
    invalidarProveedores,
    invalidarCategorias,
};