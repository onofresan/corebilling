import os
import mysql.connector
from mysql.connector import pooling
from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
from datetime import datetime, timedelta, date, timezone

# ⚠️ IMPORTANTE: Render, Clever Cloud y la mayoría de servidores en la nube
# corren en UTC, no en la hora de Venezuela (UTC-4). Si se usa datetime.now()
# o NOW() de MySQL directamente, las fechas/horas guardadas (facturas,
# alertas, historial, etc.) quedan adelantadas ~4-5 horas respecto a la hora
# real de Venezuela. Venezuela no tiene horario de verano, así que su offset
# UTC-4 es fijo todo el año — no hace falta ninguna librería de zonas
# horarias, basta con este offset fijo.
VENEZUELA_TZ = timezone(timedelta(hours=-4))

def ahora_venezuela():
    """Reemplazo directo de datetime.now(): devuelve un datetime 'naive'
    (sin tzinfo) pero con la hora de reloj correcta de Venezuela, sin
    importar en qué zona horaria esté físicamente el servidor."""
    return datetime.now(timezone.utc).astimezone(VENEZUELA_TZ).replace(tzinfo=None)

import json
import calendar
import bcrypt
from functools import wraps
import csv
import io
import traceback
from decimal import Decimal
from werkzeug.utils import secure_filename
from werkzeug.exceptions import NotFound
import jwt
from flask_socketio import SocketIO, emit
import stripe
import threading
import time
import requests

# ============================================
# CONFIGURACIÓN DE RENDIMIENTO
# ============================================

# Cache de consultas frecuentes en memoria
_query_cache = {}
_query_cache_ttl = 30  # segundos

def cache_get(key):
    """Obtiene datos del caché si no han expirado"""
    if key in _query_cache:
        data, timestamp = _query_cache[key]
        if time.time() - timestamp < _query_cache_ttl:
            return data
    return None

def cache_set(key, data):
    """Guarda datos en caché con timestamp"""
    _query_cache[key] = (data, time.time())

def cache_clear(key=None):
    """Limpia el caché, opcionalmente solo una clave"""
    if key:
        _query_cache.pop(key, None)
    else:
        _query_cache.clear()

# Cache de productos por empresa
_productos_cache = {}
_productos_cache_time = {}

def get_productos_cached(empresa_id):
    """Obtiene productos con caché de 30 segundos"""
    key = f"productos_{empresa_id}"
    now = time.time()
    
    if key in _productos_cache and (now - _productos_cache_time.get(key, 0)) < 30:
        return _productos_cache[key]
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT codigo, descripcion as nombre, categoria, 
                   COALESCE(unidad_medida, 'unidad') as unidad_medida,
                   COALESCE(tipo_producto, 'normal') as tipo_producto,
                   COALESCE(precio_compra, 0) as costo,
                   COALESCE(precio_venta, 0) as venta,
                   COALESCE(iva, 16) as iva,
                   COALESCE(existencia, 0) as stock
            FROM productos 
            WHERE empresa_id = %s
            ORDER BY codigo
        """, (empresa_id,))
        productos = cursor.fetchall()
        for p in productos:
            for key in ['costo', 'venta', 'iva', 'stock']:
                if p[key] is not None:
                    p[key] = float(p[key])
        safe_close_conn(conn, cursor)
        
        _productos_cache[key] = productos
        _productos_cache_time[key] = now
        return productos
    except Exception as e:
        safe_close_conn(conn, cursor)
        raise e

def invalidar_cache_productos(empresa_id):
    """Invalida el caché de productos de una empresa"""
    key = f"productos_{empresa_id}"
    _productos_cache.pop(key, None)
    _productos_cache_time.pop(key, None)

# ========== CONFIGURACIÓN ==========
app = Flask(__name__)

# ===== CLAVE JWT =====
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')
if not JWT_SECRET_KEY:
    import secrets as _secrets
    JWT_SECRET_KEY = _secrets.token_hex(32)
    print("⚠️⚠️⚠️ ADVERTENCIA DE SEGURIDAD: JWT_SECRET_KEY no está configurada en las")
    print("variables de entorno. Se generó una clave aleatoria temporal para esta")
    print("sesión del servidor. Configura JWT_SECRET_KEY en las variables de entorno")
    print("con un valor fijo y aleatorio para que las sesiones no se invaliden")
    print("cada vez que el servidor se reinicie.")
app.config['SECRET_KEY'] = JWT_SECRET_KEY
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=8)

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_...')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY', 'pk_test_...')

# ===== CORS =====
_origenes_extra = os.environ.get('CORS_ORIGENES', '')
ORIGENES_PERMITIDOS = [
    'https://corebilling-1.onrender.com',
    'http://localhost:5000',
    'http://127.0.0.1:5000',
] + [o.strip() for o in _origenes_extra.split(',') if o.strip()]

socketio = SocketIO(app, cors_allowed_origins=ORIGENES_PERMITIDOS)

UPLOAD_FOLDER = 'static/logos'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CORS(app, origins=ORIGENES_PERMITIDOS, supports_credentials=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ===== POOL DE CONEXIONES CON FALLBACK Y TIMEOUTS =====
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'user': os.environ.get('DB_USER', 'root'),
    'password': os.environ.get('DB_PASSWORD', 'Koko.2590'),
    'database': os.environ.get('DB_NAME', 'facturacion'),
    'connect_timeout': 5,
    'read_timeout': 10,
    'write_timeout': 10,
}

try:
    connection_pool = pooling.MySQLConnectionPool(
        pool_name="mypool",
        pool_size=3,
        pool_reset_session=True,
        **DB_CONFIG
    )
    print(f"✅ Pool de conexiones creado con tamaño: 3")
except Exception as e:
    print(f"❌ Error creando pool de conexiones: {e}")
    print("⚠️ Usando conexión directa como fallback...")
    connection_pool = None

def get_db_connection():
    """Obtiene una conexión del pool o directa si el pool falló"""
    if connection_pool:
        try:
            return connection_pool.get_connection()
        except Exception as e:
            print(f"❌ Error obteniendo conexión del pool: {e}")
            # Intentar reconectar directamente
            try:
                return mysql.connector.connect(**DB_CONFIG)
            except:
                # Si falla, esperar y reintentar
                time.sleep(1)
                return mysql.connector.connect(**DB_CONFIG)
    else:
        return mysql.connector.connect(**DB_CONFIG)

def safe_close_conn(conn, cursor=None):
    """Cierra conexiones de manera segura"""
    try:
        if cursor:
            cursor.close()
    except:
        pass
    try:
        if conn:
            conn.close()
    except:
        pass

# ========== DECORADORES ==========
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'Token missing'}), 401
        try:
            token = token.split(' ')[1]
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            request.user_id = data['user_id']
            request.role = data['role']
            request.username = data['username']
            request.empresa_id = data.get('empresa_id')
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expirado'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Token inválido'}), 401
        return f(*args, **kwargs)
    return decorated

def requiere_rol(rol_permitido):
    def decorator(f):
        @wraps(f)
        @token_required
        def decorated(*args, **kwargs):
            if request.role != 'admin' and request.role != rol_permitido:
                return jsonify({'error': 'Permisos insuficientes'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

def requiere_super_admin(f):
    @wraps(f)
    @token_required
    def decorated(*args, **kwargs):
        if request.role != 'super_admin':
            return jsonify({'error': 'Acceso denegado'}), 403
        return f(*args, **kwargs)
    return decorated

# ========== FUNCIONES AUXILIARES ==========
def columna_existe(conn, tabla, columna):
    """Verifica si una columna existe en una tabla usando INFORMATION_SCHEMA"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) 
        FROM INFORMATION_SCHEMA.COLUMNS 
        WHERE TABLE_SCHEMA = DATABASE() 
        AND TABLE_NAME = %s 
        AND COLUMN_NAME = %s
    """, (tabla, columna))
    result = cursor.fetchone()
    cursor.close()
    return result[0] > 0

def formatear_hora(valor):
    """Convierte de forma segura un valor de columna TIME a texto 'HH:MM:SS'.
    mysql-connector-python devuelve las columnas TIME como datetime.timedelta
    (no como datetime.time), así que no basta con revisar hasattr(strftime)."""
    if valor is None:
        return None
    if isinstance(valor, timedelta):
        total_segundos = int(valor.total_seconds())
        horas = (total_segundos // 3600) % 24
        minutos = (total_segundos % 3600) // 60
        segundos = total_segundos % 60
        return f'{horas:02d}:{minutos:02d}:{segundos:02d}'
    if hasattr(valor, 'strftime'):
        return valor.strftime('%H:%M:%S')
    return str(valor)

def tabla_existe(conn, tabla):
    """Verifica si una tabla existe en la base de datos"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) 
        FROM INFORMATION_SCHEMA.TABLES 
        WHERE TABLE_SCHEMA = DATABASE() 
        AND TABLE_NAME = %s
    """, (tabla,))
    result = cursor.fetchone()
    cursor.close()
    return result[0] > 0

def verificar_y_crear_tabla_alertas():
    """Verifica y crea la tabla alertas con la estructura correcta"""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'alertas'
        """)
        tabla_existe = cursor.fetchone()[0] > 0
        
        if not tabla_existe:
            cursor.execute("""
                CREATE TABLE alertas (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    empresa_id INT NOT NULL DEFAULT 1,
                    tipo VARCHAR(50),
                    mensaje TEXT,
                    usuario_id INT,
                    fecha DATETIME DEFAULT CURRENT_TIMESTAMP,
                    leida BOOLEAN DEFAULT FALSE,
                    INDEX idx_empresa_id (empresa_id)
                )
            """)
            conn.commit()
            print("✅ Tabla alertas creada correctamente")
        else:
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'alertas' 
                AND COLUMN_NAME = 'empresa_id'
            """)
            columna_existe = cursor.fetchone()[0] > 0
            
            if not columna_existe:
                cursor.execute("ALTER TABLE alertas ADD COLUMN empresa_id INT NOT NULL DEFAULT 1")
                cursor.execute("ALTER TABLE alertas ADD INDEX idx_empresa_id (empresa_id)")
                conn.commit()
                print("✅ Columna empresa_id agregada a alertas")
            else:
                cursor.execute("ALTER TABLE alertas MODIFY COLUMN empresa_id INT NOT NULL DEFAULT 1")
                conn.commit()
                print("✅ Columna empresa_id corregida en alertas")
            
            columnas_necesarias = [
                ('tipo', 'VARCHAR(50)'),
                ('mensaje', 'TEXT'),
                ('usuario_id', 'INT'),
                ('fecha', 'DATETIME DEFAULT CURRENT_TIMESTAMP'),
                ('leida', 'BOOLEAN DEFAULT FALSE')
            ]
            
            for columna, tipo in columnas_necesarias:
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_SCHEMA = DATABASE() 
                    AND TABLE_NAME = 'alertas' 
                    AND COLUMN_NAME = %s
                """, (columna,))
                existe = cursor.fetchone()[0] > 0
                if not existe:
                    cursor.execute(f"ALTER TABLE alertas ADD COLUMN {columna} {tipo}")
                    conn.commit()
                    print(f"✅ Columna {columna} agregada a alertas")
                    
    except Exception as e:
        print(f"❌ Error verificando/creando tabla alertas: {e}")
        if conn:
            conn.rollback()
    finally:
        safe_close_conn(conn, cursor)

def verificar_y_crear_tabla_disponibilidad():
    """Verifica y crea la tabla disponibilidad_habitaciones"""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Verificar si la tabla existe
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'disponibilidad_habitaciones'
        """)
        tabla_existe = cursor.fetchone()[0] > 0
        
        if not tabla_existe:
            cursor.execute("""
                CREATE TABLE disponibilidad_habitaciones (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    habitacion_id INT NOT NULL,
                    fecha DATE NOT NULL,
                    estado VARCHAR(20) DEFAULT 'no_disponible',
                    motivo VARCHAR(255),
                    reserva_id INT NULL,
                    empresa_id INT NOT NULL DEFAULT 1,
                    creado_en DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_habitacion_fecha (habitacion_id, fecha),
                    INDEX idx_empresa_id (empresa_id),
                    INDEX idx_fecha (fecha),
                    FOREIGN KEY (habitacion_id) REFERENCES habitaciones(id) ON DELETE CASCADE,
                    FOREIGN KEY (reserva_id) REFERENCES reservas(id) ON DELETE CASCADE,
                    UNIQUE KEY unique_habitacion_fecha (habitacion_id, fecha)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            conn.commit()
            print("✅ Tabla disponibilidad_habitaciones creada correctamente")
        else:
            # Verificar columnas necesarias
            columnas_necesarias = [
                ('estado', 'VARCHAR(20) DEFAULT "no_disponible"'),
                ('motivo', 'VARCHAR(255)'),
                ('reserva_id', 'INT NULL'),
                ('empresa_id', 'INT NOT NULL DEFAULT 1'),
                ('creado_en', 'DATETIME DEFAULT CURRENT_TIMESTAMP')
            ]
            
            for columna, tipo in columnas_necesarias:
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_SCHEMA = DATABASE() 
                    AND TABLE_NAME = 'disponibilidad_habitaciones' 
                    AND COLUMN_NAME = %s
                """, (columna,))
                existe = cursor.fetchone()[0] > 0
                if not existe:
                    cursor.execute(f"ALTER TABLE disponibilidad_habitaciones ADD COLUMN {columna} {tipo}")
                    conn.commit()
                    print(f"✅ Columna {columna} agregada a disponibilidad_habitaciones")
                    
    except Exception as e:
        print(f"❌ Error verificando/creando tabla disponibilidad_habitaciones: {e}")
        if conn:
            conn.rollback()
    finally:
        safe_close_conn(conn, cursor)

def verificar_empresa_id_en_tablas():
    """Verifica que todas las tablas tengan la columna empresa_id"""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        tablas = ['ordenes_compra', 'ordenes_detalle', 'historial_cierres', 'historial_inventario', 
                  'caja_sesion', 'recepciones_ordenes', 'categorias', 'habitaciones', 'reservas',
                  'reservas_servicios', 'historial_habitaciones', 'servicios_adicionales', 'reservas_pagos',
                  'disponibilidad_habitaciones']
        
        for tabla in tablas:
            try:
                cursor.execute(f"""
                    SELECT COUNT(*) 
                    FROM INFORMATION_SCHEMA.TABLES 
                    WHERE TABLE_SCHEMA = DATABASE() 
                    AND TABLE_NAME = '{tabla}'
                """)
                if cursor.fetchone()[0] == 0:
                    if tabla == 'disponibilidad_habitaciones':
                        verificar_y_crear_tabla_disponibilidad()
                    elif tabla == 'recepciones_ordenes':
                        cursor.execute("""
                            CREATE TABLE IF NOT EXISTS recepciones_ordenes (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                orden_id INT NOT NULL,
                                usuario_id INT NOT NULL,
                                fecha_recepcion DATETIME DEFAULT CURRENT_TIMESTAMP,
                                observaciones TEXT,
                                empresa_id INT NOT NULL DEFAULT 1,
                                INDEX idx_orden_id (orden_id),
                                INDEX idx_empresa_id (empresa_id),
                                FOREIGN KEY (orden_id) REFERENCES ordenes_compra(id) ON DELETE CASCADE
                            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """)
                        conn.commit()
                        print(f"✅ Tabla {tabla} creada correctamente")
                    elif tabla == 'categorias':
                        cursor.execute("""
                            CREATE TABLE IF NOT EXISTS categorias (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                nombre VARCHAR(50) NOT NULL UNIQUE,
                                descripcion TEXT,
                                empresa_id INT NOT NULL DEFAULT 1,
                                fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP,
                                activa BOOLEAN DEFAULT TRUE,
                                INDEX idx_empresa_id (empresa_id),
                                INDEX idx_nombre (nombre)
                            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """)
                        conn.commit()
                        cursor.execute("""
                            INSERT IGNORE INTO categorias (nombre, descripcion, empresa_id) VALUES 
                            ('licor', 'Bebidas alcohólicas', 1),
                            ('viveres', 'Alimentos no perecederos', 1),
                            ('confiteria', 'Dulces y golosinas', 1),
                            ('enlatados', 'Productos enlatados', 1),
                            ('bebidas', 'Bebidas en general', 1),
                            ('carnes', 'Carnes y embutidos', 1),
                            ('frutas', 'Frutas frescas', 1),
                            ('otros', 'Otros productos', 1)
                        """)
                        conn.commit()
                        print(f"✅ Tabla {tabla} creada correctamente con categorías por defecto")
                    elif tabla == 'reservas_pagos':
                        cursor.execute("""
                            CREATE TABLE IF NOT EXISTS reservas_pagos (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                reserva_id INT NOT NULL,
                                metodo_pago VARCHAR(50) NOT NULL,
                                monto_usd DECIMAL(10,2) NOT NULL DEFAULT 0,
                                monto_bs DECIMAL(10,2) DEFAULT 0,
                                moneda VARCHAR(10) DEFAULT 'USD',
                                referencia VARCHAR(100),
                                usuario_id INT,
                                fecha DATETIME DEFAULT CURRENT_TIMESTAMP,
                                empresa_id INT NOT NULL DEFAULT 1,
                                INDEX idx_reserva_id (reserva_id),
                                INDEX idx_empresa_id (empresa_id),
                                FOREIGN KEY (reserva_id) REFERENCES reservas(id) ON DELETE CASCADE
                            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """)
                        conn.commit()
                        print(f"✅ Tabla {tabla} creada correctamente")
                    elif tabla in ['habitaciones', 'reservas', 'reservas_servicios', 'historial_habitaciones', 'servicios_adicionales']:
                        print(f"⚠️ La tabla {tabla} no existe. Ejecuta el script SQL de hotelería primero.")
                    else:
                        print(f"⚠️ La tabla {tabla} no existe, saltando...")
                    continue
                    
                cursor.execute(f"""
                    SELECT COUNT(*) 
                    FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_SCHEMA = DATABASE() 
                    AND TABLE_NAME = '{tabla}' 
                    AND COLUMN_NAME = 'empresa_id'
                """)
                existe = cursor.fetchone()[0] > 0
                
                if not existe:
                    print(f"⚠️ La tabla {tabla} no tiene empresa_id, agregando...")
                    cursor.execute(f"ALTER TABLE {tabla} ADD COLUMN empresa_id INT NOT NULL DEFAULT 1")
                    cursor.execute(f"ALTER TABLE {tabla} ADD INDEX idx_empresa_id (empresa_id)")
                    conn.commit()
                    print(f"✅ Columna empresa_id agregada a {tabla}")
                else:
                    cursor.execute(f"ALTER TABLE {tabla} MODIFY COLUMN empresa_id INT NOT NULL DEFAULT 1")
                    conn.commit()
                    print(f"✅ Columna empresa_id corregida en {tabla}")
                    
            except mysql.connector.Error as e:
                print(f"❌ Error en {tabla}: {e}")
                
    except Exception as e:
        print(f"❌ Error verificando empresa_id en tablas: {e}")
    finally:
        safe_close_conn(conn, cursor)

def registrar_historial_inventario(cursor, codigo, descripcion, tipo, cantidad_anterior, cantidad_nueva, nota=''):
    usuario = request.username
    empresa_id = request.empresa_id
    cursor.execute("""
        INSERT INTO historial_inventario (usuario, producto_codigo, producto_descripcion, tipo, cantidad_anterior, cantidad_nueva, nota, empresa_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (usuario, codigo, descripcion, tipo, cantidad_anterior, cantidad_nueva, nota, empresa_id))

def crear_alerta(empresa_id, tipo, mensaje, usuario_id=None):
    """Crea una alerta de manera segura"""
    try:
        verificar_y_crear_tabla_alertas()
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM empresas WHERE id = %s", (empresa_id,))
        if not cursor.fetchone():
            print(f"⚠️ La empresa {empresa_id} no existe")
            safe_close_conn(conn, cursor)
            return
        
        cursor.execute("""
            INSERT INTO alertas (empresa_id, tipo, mensaje, usuario_id, fecha, leida)
            VALUES (%s, %s, %s, %s, %s, 0)
        """, (empresa_id, tipo, mensaje, usuario_id, ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        
        try:
            socketio.emit('nueva_alerta', {'tipo': tipo, 'mensaje': mensaje}, room=str(empresa_id))
        except Exception as e:
            print(f"⚠️ Error emitiendo evento WebSocket: {e}")
            
        safe_close_conn(conn, cursor)
        
    except Exception as e:
        print(f"❌ Error creando alerta: {e}")

# ========== TASA BCV (Banco Central de Venezuela) ==========
def obtener_tasa_bcv():
    """
    Consulta la tasa oficial del dólar publicada por el BCV usando una API
    pública gratuita (dolarapi.com), que a su vez toma el dato directo del
    sitio del Banco Central de Venezuela. Devuelve (tasa, fecha_publicacion)
    o (None, None) si falla la consulta.
    """
    try:
        resp = requests.get('https://ve.dolarapi.com/v1/dolares/oficial', timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tasa = float(data.get('promedio') or data.get('venta') or data.get('compra'))
        fecha_pub = data.get('fechaActualizacion')
        if tasa and tasa > 0:
            return tasa, fecha_pub
        return None, None
    except Exception as e:
        print(f"❌ Error consultando tasa BCV: {e}")
        return None, None

def actualizar_tasas_automaticas():
    """
    Hilo en segundo plano: cada cierto tiempo consulta la tasa oficial BCV
    y la aplica a todas las empresas que tengan activada la actualización
    automática (columna empresas.tasa_auto = 1). Las empresas que prefieran
    ingresarla manualmente (tasa_auto = 0) no se tocan aquí.
    """
    ultima_fecha_aplicada = None
    while True:
        try:
            tasa, fecha_pub = obtener_tasa_bcv()
            if tasa and fecha_pub != ultima_fecha_aplicada:
                conn = get_db_connection()
                cursor = conn.cursor(dictionary=True)

                if not columna_existe(conn, 'empresas', 'tasa_auto'):
                    cursor.execute("ALTER TABLE empresas ADD COLUMN tasa_auto TINYINT(1) NOT NULL DEFAULT 1")
                    print("✅ Columna tasa_auto agregada a empresas")

                cursor.execute("SELECT id FROM empresas WHERE tasa_auto = 1")
                empresas = cursor.fetchall()

                for emp in empresas:
                    cursor.execute("UPDATE empresas SET tasa_cambio = %s WHERE id = %s", (tasa, emp['id']))
                    crear_alerta(emp['id'], 'tasa_actualizada', f"💱 Tasa BCV actualizada automáticamente: {tasa} Bs/$")
                    try:
                        socketio.emit('tasa_actualizada', {'tasa': tasa}, room=f"empresa_{emp['id']}")
                    except Exception:
                        pass

                conn.commit()
                safe_close_conn(conn, cursor)
                ultima_fecha_aplicada = fecha_pub
                print(f"✅ Tasa BCV actualizada automáticamente: {tasa} Bs/$ (publicada: {fecha_pub}) en {len(empresas)} empresa(s)")

        except Exception as e:
            print(f"❌ Error en actualización automática de tasa: {e}")

        # Revisa cada hora si el BCV publicó una tasa nueva
        time.sleep(3600)

# ========== VERIFICADOR DE HABITACIONES VENCIDAS ==========
def verificar_habitaciones_vencidas():
    """Función que se ejecuta en segundo plano para verificar habitaciones vencidas"""
    while True:
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            
            ahora_dt = ahora_venezuela()
            ahora_str = ahora_dt.strftime('%Y-%m-%d %H:%M:%S')
            
            cursor.execute("""
                SELECT id, numero, codigo_producto, fecha_salida_ultima, hora_salida_ultima, empresa_id
                FROM habitaciones 
                WHERE estado = 'ocupada' 
                AND fecha_salida_ultima IS NOT NULL
                AND TIMESTAMP(fecha_salida_ultima, COALESCE(hora_salida_ultima, '12:00:00')) <= %s
            """, (ahora_str,))
            
            habitaciones_vencidas = cursor.fetchall()
            
            for hab in habitaciones_vencidas:
                cursor.execute("""
                    UPDATE habitaciones 
                    SET estado = 'sucia',
                        observaciones = CONCAT(COALESCE(observaciones, ''), '\n', %s, ' - Check-out automático por vencimiento de estadía')
                    WHERE id = %s
                """, (ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), hab['id']))
                
                if hab.get('codigo_producto'):
                    cursor.execute("""
                        UPDATE productos 
                        SET existencia = 0 
                        WHERE codigo = %s AND empresa_id = %s
                    """, (hab['codigo_producto'], hab['empresa_id']))
                    print(f"✅ Stock de {hab['codigo_producto']} → 0 (habitación sucia)")
                
                cursor.execute("""
                    DELETE FROM disponibilidad_habitaciones
                    WHERE habitacion_id = %s AND empresa_id = %s
                """, (hab['id'], hab['empresa_id']))
                print(f"✅ Fechas liberadas para habitación {hab['numero']}")
                
                cursor.execute("""
                    INSERT INTO historial_habitaciones (habitacion_id, estado_anterior, estado_nuevo, usuario_id, motivo, empresa_id)
                    VALUES (%s, 'ocupada', 'sucia', 1, 'Check-out automático por vencimiento', %s)
                """, (hab['id'], hab['empresa_id']))
                
                if hab.get('empresa_id'):
                    crear_alerta(hab['empresa_id'], 'habitacion_vencida', 
                               f"🔄 Habitación {hab['numero']} - Check-out automático - SUCIA (stock=0)")
                
                print(f"✅ Habitación {hab['numero']} pasó a SUCIA automáticamente")
            
            conn.commit()
            safe_close_conn(conn, cursor)
            
        except Exception as e:
            print(f"❌ Error en verificación de habitaciones: {e}")
        
        time.sleep(300)

# ========== WEBSOCKETS ==========
@socketio.on('join')
def handle_join(data):
    empresa_id = data.get('empresa_id')
    if empresa_id:
        from flask_socketio import join_room
        join_room(str(empresa_id))

# ========== AUTENTICACIÓN ==========
# ========== PROTECCIÓN CONTRA FUERZA BRUTA EN EL LOGIN ==========
_intentos_login_lock = threading.Lock()
_intentos_login_fallidos = {}
MAX_INTENTOS_LOGIN = 5
VENTANA_BLOQUEO_MINUTOS = 15

def _clave_rate_limit(username):
    ip = request.headers.get('X-Forwarded-For', request.remote_addr) or 'desconocida'
    return f"{ip}:{(username or '').lower()}"

def login_bloqueado(username):
    clave = _clave_rate_limit(username)
    ahora = ahora_venezuela()
    with _intentos_login_lock:
        intentos = _intentos_login_fallidos.get(clave, [])
        intentos = [t for t in intentos if (ahora - t).total_seconds() < VENTANA_BLOQUEO_MINUTOS * 60]
        _intentos_login_fallidos[clave] = intentos
        return len(intentos) >= MAX_INTENTOS_LOGIN

def registrar_intento_fallido(username):
    clave = _clave_rate_limit(username)
    with _intentos_login_lock:
        _intentos_login_fallidos.setdefault(clave, []).append(ahora_venezuela())

def limpiar_intentos_fallidos(username):
    clave = _clave_rate_limit(username)
    with _intentos_login_lock:
        _intentos_login_fallidos.pop(clave, None)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('usuario')
    password = data.get('contrasena')

    if login_bloqueado(username):
        return jsonify({'error': f'Demasiados intentos fallidos. Espera {VENTANA_BLOQUEO_MINUTOS} minutos antes de volver a intentar.'}), 429
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            SELECT u.*, e.activa as empresa_activa 
            FROM usuarios u
            LEFT JOIN empresas e ON u.empresa_id = e.id
            WHERE u.username = %s
        """, (username,))
        user = cursor.fetchone()
        
        if not user or not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            registrar_intento_fallido(username)
            return jsonify({'error': 'Credenciales inválidas'}), 401

        limpiar_intentos_fallidos(username)
        empresa_id = user.get('empresa_id')
        
        if user['role'] != 'super_admin':
            if not empresa_id:
                cursor.execute("SELECT id FROM empresas LIMIT 1")
                empresa = cursor.fetchone()
                if empresa:
                    empresa_id = empresa['id']
                else:
                    return jsonify({'error': 'No hay empresas registradas.'}), 403
            
            if not user.get('empresa_activa', True):
                return jsonify({'error': 'Empresa desactivada'}), 403

        token_data = {
            'user_id': user['id'],
            'role': user['role'],
            'username': user['username'],
            'empresa_id': empresa_id,
            'exp': datetime.utcnow() + app.config['JWT_ACCESS_TOKEN_EXPIRES']
        }
        token = jwt.encode(token_data, app.config['SECRET_KEY'], algorithm='HS256')
        
        return jsonify({
            'token': token,
            'role': user['role'],
            'username': user['username'],
            'empresa_id': empresa_id
        }), 200
        
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/check-session', methods=['GET'])
@token_required
def check_session():
    return jsonify({
        'logged_in': True,
        'role': request.role,
        'username': request.username,
        'empresa_id': request.empresa_id,
        'user_id': request.user_id
    }), 200

@app.route('/api/logout', methods=['POST'])
@token_required
def logout():
    return jsonify({'status': 'OK'}), 200

# ========== REGISTRO DE EMPRESA ==========
@app.route('/api/registro-empresa', methods=['POST'])
def registro_empresa():
    data = request.json
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO empresas (nombre, rif, correo, telefono, direccion, tasa_cambio, activa)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
        """, (data['nombre_establecimiento'], data['rif'], data['correo'], data['telefono'], data.get('direccion', ''), 544.58))
        empresa_id = cursor.lastrowid
        
        cursor.execute("""
            INSERT INTO clientes (rif, nombre, empresa_id)
            VALUES ('J-00000000-0', 'Cliente General', %s)
        """, (empresa_id,))
        
        if not columna_existe(conn, 'empresas', 'permite_reiniciar_historial'):
            cursor.execute("ALTER TABLE empresas ADD COLUMN permite_reiniciar_historial BOOLEAN DEFAULT FALSE")
        
        verificar_y_crear_tabla_alertas()
        verificar_y_crear_tabla_disponibilidad()
        verificar_empresa_id_en_tablas()
        
        hashed = bcrypt.hashpw(data['contrasena'].encode('utf-8'), bcrypt.gensalt())
        cursor.execute("""
            INSERT INTO usuarios (username, password_hash, email, role, empresa_id)
            VALUES (%s, %s, %s, 'admin', %s)
        """, (data['usuario'], hashed, data['correo'], empresa_id))
        conn.commit()
        return jsonify({'status': 'OK', 'empresa_id': empresa_id}), 201
    except mysql.connector.IntegrityError as e:
        conn.rollback()
        return jsonify({'error': 'El usuario o RIF ya existe'}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== EMPRESA ==========
@app.route('/api/empresa', methods=['GET'])
@requiere_rol('cajero')
def obtener_empresa():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        tiene_columna = columna_existe(conn, 'empresas', 'permite_reiniciar_historial')
        tiene_tasa_auto = columna_existe(conn, 'empresas', 'tasa_auto')
        
        campos = "nombre, rif, correo, telefono, direccion, tasa_cambio, logo_url"
        if tiene_columna:
            campos += ", permite_reiniciar_historial"
        if tiene_tasa_auto:
            campos += ", tasa_auto"
        cursor.execute(f"SELECT {campos} FROM empresas WHERE id = %s", (empresa_id,))
        
        empresa = cursor.fetchone()
        if not empresa:
            return jsonify({'error': 'Empresa no encontrada'}), 404
            
        if not tiene_columna:
            empresa['permite_reiniciar_historial'] = False
        if not tiene_tasa_auto:
            empresa['tasa_auto'] = True
        else:
            empresa['tasa_auto'] = bool(empresa['tasa_auto'])
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)
        
    if empresa and empresa['tasa_cambio']:
        empresa['tasa_cambio'] = float(empresa['tasa_cambio'])
    return jsonify(empresa), 200

@app.route('/api/empresa', methods=['PUT'])
@requiere_rol('admin')
def actualizar_empresa():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if not columna_existe(conn, 'empresas', 'tasa_auto'):
            cursor.execute("ALTER TABLE empresas ADD COLUMN tasa_auto TINYINT(1) NOT NULL DEFAULT 1")

        campos_sql = "nombre=%s, rif=%s, correo=%s, telefono=%s, direccion=%s, tasa_cambio=%s"
        valores = [
            data.get('nombre', ''), 
            data.get('rif', ''), 
            data.get('correo', ''), 
            data.get('telefono', ''), 
            data.get('direccion', ''), 
            data.get('tasa_cambio', 544.58), 
        ]
        if 'tasa_auto' in data:
            campos_sql += ", tasa_auto=%s"
            valores.append(1 if data.get('tasa_auto') else 0)
        valores.append(empresa_id)

        cursor.execute(f"UPDATE empresas SET {campos_sql} WHERE id=%s", tuple(valores))
        conn.commit()
        
        if cursor.rowcount == 0:
            return jsonify({'error': 'Empresa no encontrada'}), 404
            
        return jsonify({'status': 'OK', 'mensaje': 'Datos actualizados correctamente'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/tasa/actualizar-ahora', methods=['POST'])
@requiere_rol('admin')
def actualizar_tasa_ahora():
    """Consulta la tasa BCV en el momento y la aplica a la empresa actual,
    sin importar si tiene activada la actualización automática o no."""
    empresa_id = request.empresa_id
    tasa, fecha_pub = obtener_tasa_bcv()
    if not tasa:
        return jsonify({'error': 'No se pudo consultar la tasa BCV en este momento. Intenta de nuevo en unos minutos.'}), 502

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE empresas SET tasa_cambio = %s WHERE id = %s", (tasa, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK', 'tasa': tasa, 'fecha_publicacion': fecha_pub}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/empresa/logo', methods=['POST'])
@requiere_rol('admin')
def subir_logo():
    empresa_id = request.empresa_id
    if 'logo' not in request.files:
        return jsonify({'error': 'No se envió archivo'}), 400
    file = request.files['logo']
    if file.filename == '':
        return jsonify({'error': 'Nombre vacío'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Formato no permitido'}), 400
    extension = file.filename.rsplit('.', 1)[1].lower()
    filename = f"empresa_{empresa_id}.{extension}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    logo_url = f"/static/logos/{filename}"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE empresas SET logo_url = %s WHERE id = %s", (logo_url, empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK', 'logo_url': logo_url}), 200

@app.route('/api/empresa/logo', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_logo():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT logo_url FROM empresas WHERE id = %s", (empresa_id,))
    empresa = cursor.fetchone()
    if empresa and empresa['logo_url']:
        filepath = os.path.join(app.root_path, empresa['logo_url'].lstrip('/'))
        if os.path.exists(filepath):
            os.remove(filepath)
        cursor.execute("UPDATE empresas SET logo_url = NULL WHERE id = %s", (empresa_id,))
        conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

@app.route('/api/tasa', methods=['GET'])
@requiere_rol('cajero')
def obtener_tasa():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
    tasa = cursor.fetchone()
    safe_close_conn(conn, cursor)
    return jsonify({'tasa': float(tasa['tasa_cambio']) if tasa else 544.58}), 200

@app.route('/api/tasa', methods=['POST'])
@requiere_rol('admin')
def guardar_tasa():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE empresas SET tasa_cambio = %s WHERE id = %s", (data['tasa'], empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

# ========== USUARIOS ==========
@app.route('/api/usuarios', methods=['GET'])
@requiere_rol('admin')
def listar_usuarios():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role, email, telefono FROM usuarios WHERE empresa_id = %s", (empresa_id,))
    usuarios = cursor.fetchall()
    safe_close_conn(conn, cursor)
    return jsonify(usuarios), 200

@app.route('/api/usuarios', methods=['POST'])
@requiere_rol('admin')
def crear_usuario():
    data = request.json
    empresa_id = request.empresa_id
    hashed = bcrypt.hashpw(data['contrasena'].encode('utf-8'), bcrypt.gensalt())
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO usuarios (username, password_hash, email, role, empresa_id, telefono)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (data['usuario'], hashed, data.get('correo', ''), data.get('role', 'cajero'), empresa_id, data.get('telefono', '')))
        conn.commit()
        return jsonify({'status': 'OK'}), 201
    except mysql.connector.IntegrityError:
        return jsonify({'error': 'El nombre de usuario ya existe'}), 400
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/usuarios/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_usuario(id):
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    updates = []
    params = []
    if 'username' in data and data['username']:
        updates.append("username = %s")
        params.append(data['username'])
    if 'password' in data and data['password']:
        hashed = bcrypt.hashpw(data['password'].encode('utf-8'), bcrypt.gensalt())
        updates.append("password_hash = %s")
        params.append(hashed)
    if 'email' in data and data['email']:
        updates.append("email = %s")
        params.append(data['email'])
    if 'telefono' in data and data['telefono']:
        updates.append("telefono = %s")
        params.append(data['telefono'])
    if not updates:
        return jsonify({'error': 'No se proporcionaron datos'}), 400
    params.append(id)
    params.append(empresa_id)
    cursor.execute(f"UPDATE usuarios SET {', '.join(updates)} WHERE id = %s AND empresa_id = %s", params)
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

@app.route('/api/usuarios/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_usuario(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM usuarios WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

# ========== CLIENTES ==========
@app.route('/api/clientes', methods=['GET'])
@requiere_rol('cajero')
def obtener_clientes():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, nombre, rif, telefono, direccion, email FROM clientes WHERE empresa_id = %s ORDER BY nombre", (empresa_id,))
    clientes = cursor.fetchall()
    safe_close_conn(conn, cursor)
    return jsonify(clientes), 200

@app.route('/api/clientes', methods=['POST'])
@requiere_rol('cajero')
def agregar_cliente():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO clientes (rif, nombre, telefono, direccion, email, empresa_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                nombre = VALUES(nombre), telefono = VALUES(telefono),
                direccion = VALUES(direccion), email = VALUES(email)
        """, (data['rif'], data['nombre'], data.get('telefono', ''), data.get('direccion', ''), data.get('email', ''), empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except mysql.connector.Error as err:
        return jsonify({'error': str(err)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/clientes/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_cliente(id):
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE clientes 
        SET rif=%s, nombre=%s, telefono=%s, direccion=%s, email=%s
        WHERE id=%s AND empresa_id=%s
    """, (data['rif'], data['nombre'], data.get('telefono', ''), data.get('direccion', ''), data.get('email', ''), id, empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

@app.route('/api/clientes/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_cliente(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM facturas_cabecera WHERE cliente_id = %s AND empresa_id = %s", (id, empresa_id))
    total = cursor.fetchone()[0]
    if total > 0:
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'Cliente tiene facturas asociadas'}), 400
    cursor.execute("DELETE FROM clientes WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

# ========== PRODUCTOS (OPTIMIZADO CON CACHÉ) ==========
@app.route('/api/productos', methods=['GET'])
@requiere_rol('cajero')
def obtener_productos():
    try:
        productos = get_productos_cached(request.empresa_id)
        return jsonify(productos), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/productos', methods=['POST'])
@requiere_rol('admin')
def agregar_producto():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        nuevo_stock = float(data.get('stock', 0))
        costo = float(data.get('costo', 0))
        venta = float(data.get('venta', 0))
        tipo_producto = data.get('tipo_producto', 'normal')
        if tipo_producto in ['receta', 'kit_hijo']:
            nuevo_stock = 0
        if nuevo_stock < 0 or costo < 0 or venta < 0:
            return jsonify({'error': 'Valores no negativos'}), 400
        cursor.execute("SELECT codigo, descripcion, existencia, iva FROM productos WHERE codigo = %s AND empresa_id = %s", (data['codigo'], empresa_id))
        existe = cursor.fetchone()
        iva = data.get('iva', 16.0)
        nota = data.get('nota', '')
        if existe:
            anterior_stock = existe['existencia']
            cursor.execute("""
                UPDATE productos 
                SET descripcion=%s, categoria=%s, precio_compra=%s, precio_venta=%s, existencia=%s, iva=%s, unidad_medida=%s, tipo_producto=%s
                WHERE codigo=%s AND empresa_id=%s
            """, (data['nombre'], data['categoria'], costo, venta, nuevo_stock, iva, data.get('unidad_medida', 'unidad'), tipo_producto, data['codigo'], empresa_id))
            registrar_historial_inventario(cursor, data['codigo'], data['nombre'], 'modificacion',
                                           anterior_stock, nuevo_stock, f"Actualizado: costo={costo}, venta={venta}, iva={iva}% | Nota: {nota}")
        else:
            cursor.execute("""
                INSERT INTO productos (codigo, descripcion, categoria, precio_compra, precio_venta, existencia, iva, unidad_medida, tipo_producto, empresa_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (data['codigo'], data['nombre'], data['categoria'], costo, venta, nuevo_stock, iva, data.get('unidad_medida', 'unidad'), tipo_producto, empresa_id))
            registrar_historial_inventario(cursor, data['codigo'], data['nombre'], 'creacion', 0, nuevo_stock, f'Creado | Nota: {nota}')
        conn.commit()
        invalidar_cache_productos(empresa_id)
        if nuevo_stock < 5 and tipo_producto not in ['receta', 'kit_hijo']:
            crear_alerta(empresa_id, 'stock_bajo', f"Producto {data['codigo']} stock bajo: {nuevo_stock}")
        return jsonify({'status': 'OK'}), 200
    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/productos/<codigo>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_producto(codigo):
    data = request.json or {}
    nota = data.get('nota', '')
    empresa_id = request.empresa_id
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("SELECT descripcion, existencia FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
        prod = cursor.fetchone()
        if not prod:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Producto no encontrado'}), 404
        
        cursor.execute("""
            SELECT COUNT(*) as total 
            FROM facturas_detalle fd
            JOIN facturas_cabecera fc ON fd.factura_numero = fc.numero
            WHERE fd.producto_codigo = %s AND fc.estado != 'anulada' AND fc.empresa_id = %s
        """, (codigo, empresa_id))
        result = cursor.fetchone()
        if result and result['total'] > 0:
            safe_close_conn(conn, cursor)
            return jsonify({
                'error': f'No se puede eliminar el producto. Está asociado a {result["total"]} factura(s) activas.'
            }), 400
        
        cursor.execute("""
            SELECT COUNT(*) as total 
            FROM recetas_detalle 
            WHERE producto_codigo = %s AND empresa_id = %s
        """, (codigo, empresa_id))
        result = cursor.fetchone()
        if result and result['total'] > 0:
            safe_close_conn(conn, cursor)
            return jsonify({
                'error': f'No se puede eliminar el producto. Está asociado a {result["total"]} receta(s).'
            }), 400
        
        cursor.execute("""
            SELECT COUNT(*) as total 
            FROM kits 
            WHERE producto_padre_codigo = %s AND empresa_id = %s
        """, (codigo, empresa_id))
        result = cursor.fetchone()
        if result and result['total'] > 0:
            safe_close_conn(conn, cursor)
            return jsonify({
                'error': f'No se puede eliminar el producto. Es padre de {result["total"]} kit(s).'
            }), 400
        
        cursor.execute("""
            SELECT COUNT(*) as total 
            FROM kit_detalle 
            WHERE producto_hijo_codigo = %s AND empresa_id = %s
        """, (codigo, empresa_id))
        result = cursor.fetchone()
        if result and result['total'] > 0:
            safe_close_conn(conn, cursor)
            return jsonify({
                'error': f'No se puede eliminar el producto. Es hijo de {result["total"]} kit(s).'
            }), 400
        
        registrar_historial_inventario(
            cursor, 
            codigo, 
            prod['descripcion'], 
            'eliminacion',
            float(prod['existencia']), 
            0, 
            f'Eliminado por {request.username} | Nota: {nota}'
        )
        
        cursor.execute("DELETE FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
        conn.commit()
        invalidar_cache_productos(empresa_id)
        
        crear_alerta(empresa_id, 'producto_eliminado', f"Producto {codigo} - {prod['descripcion']} eliminado por {request.username}")
        
        return jsonify({
            'status': 'OK', 
            'mensaje': f'Producto {codigo} eliminado exitosamente'
        }), 200
        
    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err)}), 500
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== MOVIMIENTOS INVENTARIO ==========
@app.route('/api/movimiento-inventario', methods=['POST'])
@requiere_rol('admin')
def registrar_movimiento_inventario():
    data = request.json
    codigo = data.get('codigo')
    tipo = data.get('tipo')
    cantidad = float(data.get('cantidad', 0))
    nota = data.get('nota', '')
    empresa_id = request.empresa_id
    if tipo not in ['ingreso', 'reduccion']:
        return jsonify({'error': 'Tipo inválido'}), 400
    if cantidad <= 0:
        return jsonify({'error': 'Cantidad >0'}), 400
    if not nota:
        return jsonify({'error': 'Nota requerida'}), 400
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT descripcion, existencia FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
    prod = cursor.fetchone()
    if not prod:
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'Producto no encontrado'}), 404
    stock_anterior = float(prod['existencia'])
    if tipo == 'ingreso':
        nuevo_stock = stock_anterior + cantidad
        tipo_mov = 'ingreso'
    else:
        nuevo_stock = stock_anterior - cantidad
        if nuevo_stock < 0:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Stock negativo'}), 400
        tipo_mov = 'reduccion'
    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, codigo, empresa_id))
    registrar_historial_inventario(cursor, codigo, prod['descripcion'], tipo_mov, stock_anterior, nuevo_stock, nota)
    conn.commit()
    invalidar_cache_productos(empresa_id)
    safe_close_conn(conn, cursor)
    if nuevo_stock < 5:
        crear_alerta(empresa_id, 'stock_bajo', f"Producto {codigo} stock {nuevo_stock}")
    return jsonify({'status': 'OK', 'nuevo_stock': nuevo_stock}), 200

# ========== CATEGORÍAS ==========
@app.route('/api/categorias', methods=['GET'])
@requiere_rol('cajero')
def listar_categorias():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, nombre, descripcion, fecha_creacion, activa 
        FROM categorias 
        WHERE empresa_id = %s AND activa = 1
        ORDER BY nombre
    """, (empresa_id,))
    categorias = cursor.fetchall()
    safe_close_conn(conn, cursor)
    return jsonify(categorias), 200

@app.route('/api/categorias', methods=['POST'])
@requiere_rol('admin')
def crear_categoria():
    data = request.json
    empresa_id = request.empresa_id
    nombre = data.get('nombre', '').strip().lower()
    descripcion = data.get('descripcion', '')
    
    if not nombre:
        return jsonify({'error': 'El nombre de la categoría es requerido'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO categorias (nombre, descripcion, empresa_id)
            VALUES (%s, %s, %s)
        """, (nombre, descripcion, empresa_id))
        conn.commit()
        categoria_id = cursor.lastrowid
        crear_alerta(empresa_id, 'categoria', f"Nueva categoría '{nombre}' creada")
        return jsonify({'status': 'OK', 'id': categoria_id, 'nombre': nombre}), 201
    except mysql.connector.IntegrityError:
        return jsonify({'error': 'La categoría ya existe'}), 400
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/categorias/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_categoria(id):
    data = request.json
    empresa_id = request.empresa_id
    nombre = data.get('nombre', '').strip().lower()
    descripcion = data.get('descripcion', '')
    activa = data.get('activa', True)
    
    if not nombre:
        return jsonify({'error': 'El nombre de la categoría es requerido'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM categorias WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        if not cursor.fetchone():
            return jsonify({'error': 'Categoría no encontrada'}), 404
        
        cursor.execute("""
            UPDATE categorias 
            SET nombre = %s, descripcion = %s, activa = %s
            WHERE id = %s AND empresa_id = %s
        """, (nombre, descripcion, activa, id, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except mysql.connector.IntegrityError:
        return jsonify({'error': 'La categoría ya existe'}), 400
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/categorias/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_categoria(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT nombre FROM categorias WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        categoria = cursor.fetchone()
        if not categoria:
            return jsonify({'error': 'Categoría no encontrada'}), 404
        
        cursor.execute("SELECT COUNT(*) as total FROM productos WHERE categoria = %s AND empresa_id = %s", (categoria['nombre'], empresa_id))
        result = cursor.fetchone()
        if result and result['total'] > 0:
            cursor.execute("UPDATE categorias SET activa = 0 WHERE id = %s AND empresa_id = %s", (id, empresa_id))
            conn.commit()
            return jsonify({'status': 'OK', 'mensaje': 'Categoría desactivada (tiene productos asociados)'}), 200
        
        cursor.execute("DELETE FROM categorias WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK', 'mensaje': 'Categoría eliminada'}), 200
    finally:
        safe_close_conn(conn, cursor)

# ========== RECETAS ==========
@app.route('/api/recetas', methods=['GET'])
@requiere_rol('cajero')
def listar_recetas():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM recetas WHERE empresa_id = %s ORDER BY nombre", (empresa_id,))
    recetas = cursor.fetchall()
    for r in recetas:
        r['precio_venta'] = float(r['precio_venta'])
    safe_close_conn(conn, cursor)
    return jsonify(recetas), 200

@app.route('/api/recetas', methods=['POST'])
@requiere_rol('admin')
def crear_receta():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO recetas (codigo, nombre, descripcion, precio_venta, tiempo_preparacion, disponible, empresa_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (data['codigo'], data['nombre'], data.get('descripcion', ''), data['precio_venta'], data.get('tiempo_preparacion', 0), data.get('disponible', True), empresa_id))
        receta_id = cursor.lastrowid
        for ing in data.get('ingredientes', []):
            cursor.execute("""
                INSERT INTO recetas_detalle (receta_id, producto_codigo, cantidad_necesaria, empresa_id)
                VALUES (%s, %s, %s, %s)
            """, (receta_id, ing['codigo'], ing['cantidad'], empresa_id))
        conn.commit()
        return jsonify({'status': 'OK', 'id': receta_id}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/recetas/<int:id>', methods=['GET'])
@requiere_rol('cajero')
def obtener_receta(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM recetas WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    receta = cursor.fetchone()
    if not receta:
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'Receta no encontrada'}), 404
    receta['precio_venta'] = float(receta['precio_venta'])
    cursor.execute("""
        SELECT p.codigo, p.descripcion as nombre, rd.cantidad_necesaria
        FROM recetas_detalle rd
        JOIN productos p ON rd.producto_codigo = p.codigo AND rd.empresa_id = p.empresa_id
        WHERE rd.receta_id = %s AND rd.empresa_id = %s
    """, (id, empresa_id))
    ingredientes = cursor.fetchall()
    for i in ingredientes:
        i['cantidad_necesaria'] = float(i['cantidad_necesaria'])
    receta['ingredientes'] = ingredientes
    safe_close_conn(conn, cursor)
    return jsonify(receta), 200

@app.route('/api/recetas/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_receta(id):
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE recetas SET nombre=%s, descripcion=%s, precio_venta=%s, tiempo_preparacion=%s, disponible=%s
            WHERE id=%s AND empresa_id=%s
        """, (data['nombre'], data.get('descripcion', ''), data['precio_venta'], data.get('tiempo_preparacion', 0), data.get('disponible', True), id, empresa_id))
        cursor.execute("DELETE FROM recetas_detalle WHERE receta_id = %s AND empresa_id = %s", (id, empresa_id))
        for ing in data.get('ingredientes', []):
            cursor.execute("""
                INSERT INTO recetas_detalle (receta_id, producto_codigo, cantidad_necesaria, empresa_id)
                VALUES (%s, %s, %s, %s)
            """, (id, ing['codigo'], ing['cantidad'], empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/recetas/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_receta(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM recetas_detalle WHERE receta_id = %s AND empresa_id = %s", (id, empresa_id))
        cursor.execute("DELETE FROM recetas WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== KITS ==========
@app.route('/api/kits', methods=['GET'])
@requiere_rol('admin')
def listar_kits():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT k.id, k.nombre, k.producto_padre_codigo, p.descripcion as padre_nombre
        FROM kits k
        JOIN productos p ON k.producto_padre_codigo = p.codigo AND k.empresa_id = p.empresa_id
        WHERE k.empresa_id = %s
    """, (empresa_id,))
    kits = cursor.fetchall()
    for kit in kits:
        cursor.execute("""
            SELECT kd.id, kd.producto_hijo_codigo, p.descripcion as hijo_nombre, kd.cantidad_estimada
            FROM kit_detalle kd
            JOIN productos p ON kd.producto_hijo_codigo = p.codigo AND p.empresa_id = %s
            WHERE kd.kit_id = %s
        """, (empresa_id, kit['id']))
        detalle = cursor.fetchall()
        for d in detalle:
            d['cantidad_estimada'] = float(d['cantidad_estimada'])
        kit['detalle'] = detalle
    safe_close_conn(conn, cursor)
    return jsonify(kits), 200

@app.route('/api/kits', methods=['POST'])
@requiere_rol('admin')
def crear_kit():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO kits (nombre, producto_padre_codigo, empresa_id)
            VALUES (%s, %s, %s)
        """, (data['nombre'], data['producto_padre_codigo'], empresa_id))
        kit_id = cursor.lastrowid
        for hijo in data['detalle']:
            cursor.execute("""
                INSERT INTO kit_detalle (kit_id, producto_hijo_codigo, cantidad_estimada, empresa_id)
                VALUES (%s, %s, %s, %s)
            """, (kit_id, hijo['codigo'], hijo['cantidad'], empresa_id))
        conn.commit()
        return jsonify({'status': 'OK', 'id': kit_id}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/kits/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_kit(id):
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE kits 
            SET nombre = %s, producto_padre_codigo = %s
            WHERE id = %s AND empresa_id = %s
        """, (data['nombre'], data['producto_padre_codigo'], id, empresa_id))
        cursor.execute("DELETE FROM kit_detalle WHERE kit_id = %s AND empresa_id = %s", (id, empresa_id))
        for hijo in data['detalle']:
            cursor.execute("""
                INSERT INTO kit_detalle (kit_id, producto_hijo_codigo, cantidad_estimada, empresa_id)
                VALUES (%s, %s, %s, %s)
            """, (id, hijo['codigo'], hijo['cantidad'], empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/kits/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_kit(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM kit_detalle WHERE kit_id = %s AND empresa_id = %s", (id, empresa_id))
        cursor.execute("DELETE FROM kits WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== DESPIECE ==========
@app.route('/api/despiece', methods=['POST'])
@requiere_rol('admin')
def realizar_despiece():
    data = request.json
    kit_id = data.get('kit_id')
    cantidad_padre = float(data.get('cantidad', 0))
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT producto_padre_codigo FROM kits WHERE id = %s AND empresa_id = %s", (kit_id, empresa_id))
        kit = cursor.fetchone()
        if not kit:
            return jsonify({'error': 'Kit no encontrado'}), 404
        padre_codigo = kit['producto_padre_codigo']
        cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (padre_codigo, empresa_id))
        padre = cursor.fetchone()
        if not padre or padre['existencia'] < cantidad_padre:
            return jsonify({'error': f'Stock insuficiente de {padre_codigo}'}), 400
        cursor.execute("SELECT producto_hijo_codigo, cantidad_estimada FROM kit_detalle WHERE kit_id = %s AND empresa_id = %s", (kit_id, empresa_id))
        hijos = cursor.fetchall()
        if not hijos:
            return jsonify({'error': 'Kit sin hijos'}), 400
        for hijo in hijos:
            factor = float(hijo['cantidad_estimada'])
            cantidad_hijo = factor * cantidad_padre
            cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (hijo['producto_hijo_codigo'], empresa_id))
            hijo_actual = cursor.fetchone()
            if hijo_actual:
                nuevo_stock = float(hijo_actual['existencia']) + cantidad_hijo
                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, hijo['producto_hijo_codigo'], empresa_id))
                registrar_historial_inventario(cursor, hijo['producto_hijo_codigo'], hijo_actual['descripcion'], 'despiece',
                                               float(hijo_actual['existencia']), nuevo_stock, f"Despiece desde {padre_codigo}")
        nuevo_stock_padre = float(padre['existencia']) - cantidad_padre
        cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock_padre, padre_codigo, empresa_id))
        registrar_historial_inventario(cursor, padre_codigo, padre['descripcion'], 'despiece',
                                       float(padre['existencia']), nuevo_stock_padre, f"Despiece kit {kit_id}")
        conn.commit()
        invalidar_cache_productos(empresa_id)
        return jsonify({'status': 'OK', 'mensaje': f'Despiece realizado. Nuevo stock padre: {nuevo_stock_padre}'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/despiece-selectivo', methods=['POST'])
@requiere_rol('admin')
def realizar_despiece_selectivo():
    data = request.json
    kit_id = data.get('kit_id')
    hijos_procesar = data.get('hijos', [])
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if not kit_id or not hijos_procesar:
            return jsonify({'error': 'Datos incompletos'}), 400
        cursor.execute("SELECT producto_padre_codigo FROM kits WHERE id = %s AND empresa_id = %s", (kit_id, empresa_id))
        kit = cursor.fetchone()
        if not kit:
            return jsonify({'error': 'Kit no encontrado'}), 404
        padre_codigo = kit['producto_padre_codigo']
        cursor.execute("SELECT producto_hijo_codigo, cantidad_estimada FROM kit_detalle WHERE kit_id = %s AND empresa_id = %s", (kit_id, empresa_id))
        factores = {row['producto_hijo_codigo']: float(row['cantidad_estimada']) for row in cursor.fetchall()}
        for h in hijos_procesar:
            if h['codigo'] not in factores:
                return jsonify({'error': f'Producto {h["codigo"]} no pertenece al kit'}), 400
        padre_necesario = sum(h['cantidad'] / factores[h['codigo']] for h in hijos_procesar)
        cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (padre_codigo, empresa_id))
        padre = cursor.fetchone()
        if not padre or padre['existencia'] < padre_necesario:
            return jsonify({'error': f'Stock insuficiente del padre. Necesario: {padre_necesario}'}), 400
        nuevo_stock_padre = float(padre['existencia']) - padre_necesario
        cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock_padre, padre_codigo, empresa_id))
        registrar_historial_inventario(cursor, padre_codigo, padre['descripcion'], 'despiece',
                                       float(padre['existencia']), nuevo_stock_padre, "Despiece selectivo")
        for h in hijos_procesar:
            cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (h['codigo'], empresa_id))
            hijo_actual = cursor.fetchone()
            if hijo_actual:
                nuevo_stock = float(hijo_actual['existencia']) + h['cantidad']
                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, h['codigo'], empresa_id))
                registrar_historial_inventario(cursor, h['codigo'], hijo_actual['descripcion'], 'despiece',
                                               float(hijo_actual['existencia']), nuevo_stock, f"Selectivo desde {padre_codigo}")
        conn.commit()
        invalidar_cache_productos(empresa_id)
        return jsonify({'status': 'OK', 'mensaje': f'Despiece selectivo realizado. Padre descontado: {padre_necesario}'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== CAJA ==========
@app.route('/api/mi-caja', methods=['GET'])
@requiere_rol('cajero')
def obtener_mi_caja():
    usuario_id = request.user_id
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, fecha_apertura, fecha_cierre, estado, total_usd, total_bs
        FROM caja_sesion
        WHERE empresa_id = %s AND usuario_id = %s AND estado = 'abierta'
        ORDER BY fecha_apertura DESC LIMIT 1
    """, (empresa_id, usuario_id))
    caja = cursor.fetchone()
    safe_close_conn(conn, cursor)
    if caja:
        caja['fecha_apertura'] = caja['fecha_apertura'].strftime('%Y-%m-%d %H:%M:%S') if caja['fecha_apertura'] else None
        if caja.get('fecha_cierre'):
            caja['fecha_cierre'] = caja['fecha_cierre'].strftime('%Y-%m-%d %H:%M:%S')
    else:
        caja = {'estado': 'cerrada'}
    return jsonify(caja), 200

@app.route('/api/abrir-caja', methods=['POST'])
@requiere_rol('cajero')
def abrir_mi_caja():
    usuario_id = request.user_id
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM caja_sesion WHERE empresa_id = %s AND usuario_id = %s AND estado = 'abierta'", (empresa_id, usuario_id))
    if cursor.fetchone():
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'Ya tienes una caja abierta'}), 400
    now = ahora_venezuela()
    cursor.execute("""
        INSERT INTO caja_sesion (empresa_id, usuario_id, fecha_apertura, estado)
        VALUES (%s, %s, %s, 'abierta')
    """, (empresa_id, usuario_id, now))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK', 'mensaje': 'Caja abierta'}), 200

@app.route('/api/cerrar-caja', methods=['POST'])
@requiere_rol('cajero')
def cerrar_mi_caja():
    usuario_id = request.user_id
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id FROM caja_sesion
        WHERE empresa_id = %s AND usuario_id = %s AND estado = 'abierta'
        ORDER BY fecha_apertura DESC LIMIT 1
    """, (empresa_id, usuario_id))
    caja = cursor.fetchone()
    if not caja:
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'No hay caja abierta'}), 400
    caja_id = caja['id']

    cursor.execute("""
        SELECT fc.total_usd, fc.subtotal_usd, fc.iva_usd, fc.monto_servicio_usd
        FROM facturas_cabecera fc
        WHERE fc.caja_sesion_id = %s AND fc.estado = 'activa' 
          AND fc.metodo_pago NOT IN ('Casa', 'Credito')
    """, (caja_id,))
    facturas = cursor.fetchall()
    num_transacciones = len(facturas)
    total_ventas_usd = sum(float(f['total_usd'] or 0) for f in facturas)
    total_servicio_usd = sum(float(f['monto_servicio_usd'] or 0) for f in facturas)
    base_imponible_usd = sum(float(f['subtotal_usd'] or 0) for f in facturas)
    iva_total_usd = sum(float(f['iva_usd'] or 0) for f in facturas)

    cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
    tasa_row = cursor.fetchone()
    tasa = float(tasa_row['tasa_cambio']) if tasa_row else 544.58
    total_ventas_bs = total_ventas_usd * tasa
    total_servicio_bs = total_servicio_usd * tasa
    base_imponible_bs = base_imponible_usd * tasa
    iva_total_bs = iva_total_usd * tasa

    cursor.execute("""
        SELECT fp.metodo_pago, SUM(fp.monto_usd) as total_usd, SUM(fp.monto_bs) as total_bs
        FROM facturas_pagos fp
        JOIN facturas_cabecera fc ON fp.factura_numero = fc.numero
        WHERE fc.caja_sesion_id = %s AND fc.estado = 'activa' 
          AND fc.metodo_pago NOT IN ('Casa', 'Credito')
          AND fp.es_administracion = 0
        GROUP BY fp.metodo_pago
    """, (caja_id,))
    pagos_db = cursor.fetchall()
    pagos_dict = {}
    total_cobrado_usd = 0.0
    total_cobrado_bs = 0.0
    for p in pagos_db:
        metodo = p['metodo_pago']
        monto_usd = float(p['total_usd'] or 0)
        monto_bs = float(p['total_bs'] or 0)
        pagos_dict[metodo] = {'usd': monto_usd, 'bs': monto_bs}
        total_cobrado_usd += monto_usd
        total_cobrado_bs += monto_bs
    for m in ['Efectivo', 'Divisa', 'Pago Movil', 'Biopago', 'Transferencia', 'Punto de Venta', 'Vale', 'Cashea']:
        if m not in pagos_dict:
            pagos_dict[m] = {'usd': 0.0, 'bs': 0.0}

    cursor.execute("""
        SELECT SUM(fp.monto_usd) as total_admin_usd, SUM(fp.monto_bs) as total_admin_bs
        FROM facturas_pagos fp
        JOIN facturas_cabecera fc ON fp.factura_numero = fc.numero
        WHERE fc.caja_sesion_id = %s AND fc.estado = 'activa' AND fp.es_administracion = 1
    """, (caja_id,))
    admin_row = cursor.fetchone()
    total_admin_usd = float(admin_row['total_admin_usd'] or 0)
    total_admin_bs = float(admin_row['total_admin_bs'] or 0)

    cursor.execute("""
        SELECT SUM(fc.subtotal_usd) as total_casa_usd,
               SUM(fc.subtotal_usd * fc.tasa_cambio) as total_casa_bs
        FROM facturas_cabecera fc
        WHERE fc.caja_sesion_id = %s AND fc.estado = 'activa' AND fc.metodo_pago = 'Casa'
    """, (caja_id,))
    casa_row = cursor.fetchone()
    total_casa_usd = float(casa_row['total_casa_usd'] or 0)
    total_casa_bs = float(casa_row['total_casa_bs'] or 0)

    cursor.execute("""
        SELECT SUM(fc.subtotal_usd) as total_credito_usd,
               SUM(fc.subtotal_usd * fc.tasa_cambio) as total_credito_bs
        FROM facturas_cabecera fc
        WHERE fc.caja_sesion_id = %s AND fc.estado = 'activa' AND fc.metodo_pago = 'Credito'
    """, (caja_id,))
    credito_row = cursor.fetchone()
    total_credito_usd = float(credito_row['total_credito_usd'] or 0)
    total_credito_bs = float(credito_row['total_credito_bs'] or 0)

    cursor.execute("SELECT ultimo_reporte_z_empresa FROM empresas WHERE id = %s", (empresa_id,))
    row = cursor.fetchone()
    if row and row.get('ultimo_reporte_z_empresa') is not None:
        nuevo_numero = row['ultimo_reporte_z_empresa'] + 1
        cursor.execute("UPDATE empresas SET ultimo_reporte_z_empresa = %s WHERE id = %s", (nuevo_numero, empresa_id))
    else:
        if not columna_existe(conn, 'empresas', 'ultimo_reporte_z_empresa'):
            cursor.execute("ALTER TABLE empresas ADD COLUMN ultimo_reporte_z_empresa INT DEFAULT 0")
        cursor.execute("UPDATE empresas SET ultimo_reporte_z_empresa = 1 WHERE id = %s", (empresa_id,))
        nuevo_numero = 1

    num_reporte = f"EMP-{str(empresa_id).zfill(3)}-Z-{str(nuevo_numero).zfill(6)}"

    cursor.execute("SELECT nombre, rif, direccion FROM empresas WHERE id = %s", (empresa_id,))
    empresa = cursor.fetchone()
    ahora = ahora_venezuela()

    datos_json = json.dumps({
        'fecha_hora': ahora.strftime('%Y-%m-%d %H:%M:%S'),
        'num_reporte': num_reporte,
        'num_transacciones': num_transacciones,
        'total_ventas_usd': total_ventas_usd,
        'total_ventas_bs': total_ventas_bs,
        'total_servicio_usd': total_servicio_usd,
        'total_servicio_bs': total_servicio_bs,
        'total_cobrado_usd': total_cobrado_usd,
        'total_cobrado_bs': total_cobrado_bs,
        'base_imponible_bs': base_imponible_bs,
        'iva_total_bs': iva_total_bs,
        'ventas_exentas': 0.0,
        'pagos': pagos_dict,
        'tasa': tasa,
        'empresa': empresa,
        'observaciones': {
            'pagos_administracion': {'usd': total_admin_usd, 'bs': total_admin_bs},
            'gastos_internos': {'usd': total_casa_usd, 'bs': total_casa_bs},
            'credito_fiado': {'usd': total_credito_usd, 'bs': total_credito_bs}
        }
    })

    cursor.execute("""
        INSERT INTO historial_cierres (fecha_cierre, usuario_id, total_usd, total_bs, datos, empresa_id, numero_reporte_empresa)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (ahora, usuario_id, total_cobrado_usd, total_cobrado_bs, datos_json, empresa_id, num_reporte))

    cursor.execute("""
        UPDATE caja_sesion SET estado = 'cerrada', fecha_cierre = %s, total_usd = %s, total_bs = %s
        WHERE id = %s
    """, (ahora, total_cobrado_usd, total_cobrado_bs, caja_id))
    conn.commit()
    safe_close_conn(conn, cursor)

    return jsonify({
        'status': 'OK',
        'mensaje': 'Caja cerrada correctamente',
        'reporte': {
            'empresa': empresa,
            'fecha': ahora.strftime('%Y-%m-%d %H:%M:%S'),
            'num_reporte': num_reporte,
            'num_transacciones': num_transacciones,
            'total_ventas_usd': total_ventas_usd,
            'total_ventas_bs': total_ventas_bs,
            'total_servicio_usd': total_servicio_usd,
            'total_servicio_bs': total_servicio_bs,
            'total_cobrado_usd': total_cobrado_usd,
            'total_cobrado_bs': total_cobrado_bs,
            'base_imponible_bs': base_imponible_bs,
            'iva_total_bs': iva_total_bs,
            'pagos': pagos_dict,
            'tasa': tasa,
            'observaciones': {
                'pagos_administracion': {'usd': total_admin_usd, 'bs': total_admin_bs},
                'gastos_internos': {'usd': total_casa_usd, 'bs': total_casa_bs},
                'credito_fiado': {'usd': total_credito_usd, 'bs': total_credito_bs}
            }
        }
    }), 200

@app.route('/api/cierre-general', methods=['POST'])
@requiere_rol('admin')
def cierre_general():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, usuario_id FROM caja_sesion WHERE empresa_id = %s AND estado = 'abierta'", (empresa_id,))
    cajas = cursor.fetchall()
    if not cajas:
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'No hay cajas abiertas'}), 400
    ahora = ahora_venezuela()
    for c in cajas:
        caja_id = c['id']
        usuario_id = c['usuario_id']
        cursor.execute("""
            SELECT SUM(total_usd) as total_usd, SUM(monto_bs) as total_bs
            FROM facturas_cabecera
            WHERE caja_sesion_id = %s AND estado = 'activa' AND metodo_pago NOT IN ('Casa', 'Credito')
        """, (caja_id,))
        ventas = cursor.fetchone()
        total_usd = float(ventas['total_usd'] or 0)
        total_bs = float(ventas['total_bs'] or 0)
        datos_json = json.dumps({'total_usd': total_usd, 'total_bs': total_bs, 'fecha_hora': ahora.strftime('%Y-%m-%d %H:%M:%S')})
        cursor.execute("""
            INSERT INTO historial_cierres (fecha_cierre, usuario_id, total_usd, total_bs, datos, empresa_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (ahora, usuario_id, total_usd, total_bs, datos_json, empresa_id))
        cursor.execute("""
            UPDATE caja_sesion SET estado = 'cerrada', fecha_cierre = %s, total_usd = %s, total_bs = %s
            WHERE id = %s
        """, (ahora, total_usd, total_bs, caja_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK', 'mensaje': 'Cierre general completado'}), 200

# ========== FACTURAS ==========
@app.route('/api/facturas', methods=['POST'])
@requiere_rol('cajero')
def guardar_factura():
    data = request.json
    empresa_id = request.empresa_id
    usuario_id = request.user_id
    reserva_id = data.get('reserva_id')
    
    conn = None
    cursor = None
    
    if reserva_id:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT r.*, c.nombre as cliente_nombre, c.rif as cliente_rif,
                       h.numero as habitacion_numero, h.codigo_producto
                FROM reservas r
                LEFT JOIN clientes c ON r.cliente_id = c.id
                LEFT JOIN habitaciones h ON r.habitacion_id = h.id
                WHERE r.id = %s AND r.empresa_id = %s AND r.estado = 'check_out'
            """, (reserva_id, empresa_id))
            reserva = cursor.fetchone()
            if not reserva:
                safe_close_conn(conn, cursor)
                return jsonify({'error': 'Reserva no encontrada o no está en check-out'}), 404
            
            if not data.get('cliente_id') and reserva['cliente_id']:
                data['cliente_id'] = reserva['cliente_id']
            
            if not data.get('articulos') or len(data['articulos']) == 0:
                cursor.execute("SELECT codigo FROM productos WHERE descripcion = 'Hospedaje' AND empresa_id = %s", (empresa_id,))
                prod = cursor.fetchone()
                if not prod:
                    cursor.execute("""
                        INSERT INTO productos (codigo, descripcion, categoria, precio_compra, precio_venta, existencia, iva, unidad_medida, tipo_producto, empresa_id)
                        VALUES ('HOSPEDAJE', 'Hospedaje', 'servicios', 0, %s, 999999, 16, 'unidad', 'normal', %s)
                    """, (reserva['total_usd'], empresa_id))
                    codigo_producto = 'HOSPEDAJE'
                else:
                    codigo_producto = prod['codigo']
                
                data['articulos'] = [{
                    'producto_id': codigo_producto,
                    'cantidad': 1,
                    'descuento': reserva['abono_usd'] or 0,
                    'nota_descuento': f'Abono de reserva #{reserva_id}'
                }]
                
                cursor.execute("""
                    SELECT rs.*, s.nombre as servicio_nombre
                    FROM reservas_servicios rs
                    JOIN servicios_adicionales s ON rs.servicio_id = s.id
                    WHERE rs.reserva_id = %s
                """, (reserva_id,))
                servicios = cursor.fetchall()
                for s in servicios:
                    codigo_servicio = f"SERV_{s['servicio_id']}"
                    cursor.execute("SELECT codigo FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo_servicio, empresa_id))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO productos (codigo, descripcion, categoria, precio_compra, precio_venta, existencia, iva, unidad_medida, tipo_producto, empresa_id)
                            VALUES (%s, %s, 'servicios', 0, %s, 999999, 16, 'unidad', 'normal', %s)
                        """, (codigo_servicio, s['servicio_nombre'], s['total'], empresa_id))
                    
                    data['articulos'].append({
                        'producto_id': codigo_servicio,
                        'cantidad': 1,
                        'descuento': 0,
                        'nota_descuento': f'Servicio: {s["servicio_nombre"]}'
                    })
            
            cursor.execute("""
                UPDATE reservas 
                SET estado = 'facturada', 
                    notas_internas = CONCAT(COALESCE(notas_internas, ''), '\n', %s, ' - Facturada')
                WHERE id = %s AND empresa_id = %s
            """, (ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), reserva_id, empresa_id))
            conn.commit()
            safe_close_conn(conn, cursor)
        except Exception as e:
            safe_close_conn(conn, cursor)
            return jsonify({'error': str(e)}), 500
    
    articulos = data.get('articulos', [])
    if not articulos:
        return jsonify({'error': 'El carrito está vacío'}), 400
    
    if not conn:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    else:
        cursor = conn.cursor(dictionary=True)
    
    try:
        cursor_caja = conn.cursor(dictionary=True)
        cursor_caja.execute("""
            SELECT id FROM caja_sesion
            WHERE empresa_id = %s AND usuario_id = %s AND estado = 'abierta'
            ORDER BY fecha_apertura DESC LIMIT 1
        """, (empresa_id, usuario_id))
        caja = cursor_caja.fetchone()
        cursor_caja.close()
        if not caja:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Debes abrir tu caja antes de facturar'}), 403
        caja_sesion_id = caja['id']
        
        cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
        tasa_row = cursor.fetchone()
        tasa = float(tasa_row['tasa_cambio']) if tasa_row else 544.58
        moneda = data.get('moneda', 'Bs')
        pagos = data.get('pagos', [])
        metodo = data.get('metodo_pago', 'Efectivo')
        
        notas_pago = [p.get('nota', '').strip() for p in pagos if p.get('nota', '').strip()]
        nota_pago_texto = ' / '.join(notas_pago)
        
        es_casa = (metodo == 'Casa')
        es_credito = (metodo == 'Credito')
        
        subtotal_usd = Decimal('0')
        iva_total_usd = Decimal('0')
        productos_detalle = []
        codigos_productos = [art['producto_id'] for art in articulos]
        placeholders = ','.join(['%s'] * len(codigos_productos))
        cursor.execute(f"""
            SELECT codigo, descripcion, precio_venta, iva, tipo_producto, existencia
            FROM productos
            WHERE codigo IN ({placeholders}) AND empresa_id = %s
        """, codigos_productos + [empresa_id])
        productos_dict = {p['codigo']: p for p in cursor.fetchall()}
        recetas_ids = []
        for art in articulos:
            prod = productos_dict.get(art['producto_id'])
            if prod and prod['tipo_producto'] == 'receta':
                recetas_ids.append(art['producto_id'])
        recetas_ingredientes = {}
        if recetas_ids:
            cursor.execute(f"""
                SELECT r.codigo, rd.producto_codigo, rd.cantidad_necesaria
                FROM recetas r
                JOIN recetas_detalle rd ON r.id = rd.receta_id
                WHERE r.codigo IN ({','.join(['%s']*len(recetas_ids))}) AND r.empresa_id = %s
            """, recetas_ids + [empresa_id])
            for row in cursor.fetchall():
                recetas_ingredientes.setdefault(row['codigo'], []).append({
                    'codigo': row['producto_codigo'],
                    'cantidad': Decimal(str(row['cantidad_necesaria']))
                })
        for art in articulos:
            codigo = art['producto_id']
            cantidad = Decimal(str(art['cantidad']))
            descuento = Decimal(str(art.get('descuento', 0)))
            nota_desc = art.get('nota_descuento', '')
            prod = productos_dict.get(codigo)
            if not prod:
                return jsonify({'error': f'Producto no encontrado: {codigo}'}), 400
            precio_unitario = Decimal(str(prod['precio_venta']))
            iva_porcentaje = Decimal(str(prod['iva']))
            tipo = prod['tipo_producto']
            stock_actual = Decimal(str(prod['existencia']))
            
            if codigo.startswith('HAB_'):
                if stock_actual < 1:
                    return jsonify({'error': f'La habitación {codigo} no está disponible. Stock: {stock_actual}'}), 400
            
            if tipo == 'receta':
                ingredientes = recetas_ingredientes.get(codigo, [])
                for ing in ingredientes:
                    needed = ing['cantidad'] * cantidad
                    cursor.execute("SELECT existencia FROM productos WHERE codigo = %s AND empresa_id = %s", (ing['codigo'], empresa_id))
                    ing_stock = Decimal(str(cursor.fetchone()['existencia']))
                    if ing_stock < needed:
                        return jsonify({'error': f'Stock insuficiente del ingrediente {ing["codigo"]} para la receta {codigo}'}), 400
            else:
                if stock_actual < cantidad:
                    return jsonify({'error': f'Stock insuficiente de {codigo}. Disponible: {stock_actual}'}), 400
            base = precio_unitario / (Decimal('1') + iva_porcentaje / Decimal('100'))
            iva_unitario = precio_unitario - base
            subtotal_sin_iva = base * cantidad
            subtotal_con_iva = precio_unitario * cantidad
            if descuento > 0:
                if descuento > subtotal_con_iva:
                    return jsonify({'error': f'El descuento no puede ser mayor al subtotal del producto {codigo}'}), 400
                factor = (subtotal_con_iva - descuento) / subtotal_con_iva
                subtotal_sin_iva *= factor
                subtotal_con_iva -= descuento
                iva_unitario = (iva_unitario * cantidad) * factor / cantidad if cantidad > 0 else Decimal('0')
            subtotal_usd += subtotal_con_iva
            iva_total_usd += iva_unitario * cantidad
            productos_detalle.append({
                'codigo': codigo, 'cantidad': cantidad, 'precio_unitario': precio_unitario,
                'iva_unitario': iva_unitario, 'descuento': descuento, 'nota_desc': nota_desc,
                'subtotal_sin_iva': subtotal_sin_iva, 'subtotal_con_iva': subtotal_con_iva, 'tipo': tipo
            })
        total_sin_servicio = subtotal_usd
        porcentaje_servicio = Decimal(str(data.get('porcentaje_servicio', 0)))
        monto_servicio = total_sin_servicio * porcentaje_servicio / Decimal('100')
        
        if es_casa or es_credito:
            total_usd = Decimal('0')
            total_bs = Decimal('0')
            monto_servicio = Decimal('0')
        else:
            total_usd = total_sin_servicio + monto_servicio
            total_bs = total_usd * Decimal(str(tasa))
        
        total_pagado_usd = Decimal('0')
        for p in pagos:
            if p.get('metodo_pago') == 'Casa' or p.get('es_administracion'):
                continue
            monto = Decimal(str(p.get('monto', 0)))
            moneda_pago = p.get('moneda', 'USD')
            if moneda_pago == 'USD':
                total_pagado_usd += monto
            else:
                total_pagado_usd += monto / Decimal(str(tasa))
        tolerancia = Decimal('0.05')
        if metodo == 'Cashea':
            cashea_inicial = Decimal(str(data.get('cashea_inicial', 0)))
            if abs(total_pagado_usd - cashea_inicial) > tolerancia:
                return jsonify({'error': 'El monto pagado no coincide con la inicial de Cashea'}), 400
        elif metodo == 'Casa' or metodo == 'Credito':
            pass
        else:
            if abs(total_pagado_usd - total_usd) > tolerancia:
                return jsonify({'error': f'El monto pagado ({total_pagado_usd}) no coincide con el total ({total_usd})'}), 400
        referencia = data.get('referencia', '')
        extras = None
        if metodo == 'Cashea':
            extras = json.dumps({'inicial': float(data.get('cashea_inicial', 0)), 'cuotas': int(data.get('cashea_cuotas', 0))})
            referencia = ''
        elif metodo == 'Casa':
            extras = json.dumps({'nota': data.get('nota_casa', 'Gasto interno')})
            referencia = ''
        elif metodo == 'Credito':
            extras = json.dumps({'tipo': 'credito'})
            referencia = ''
        
        hotel_data = data.get('hotel')
        if hotel_data:
            hotel_json = {
                'fecha_entrada': hotel_data.get('fecha_entrada'),
                'fecha_salida': hotel_data.get('fecha_salida'),
                'hora_entrada': hotel_data.get('hora_entrada'),
                'hora_salida': hotel_data.get('hora_salida'),
                'nota': hotel_data.get('nota', '')
            }
            if extras:
                try:
                    extras_obj = json.loads(extras)
                    extras_obj['hotel'] = hotel_json
                    extras = json.dumps(extras_obj)
                except:
                    extras = json.dumps({'hotel': hotel_json})
            else:
                extras = json.dumps({'hotel': hotel_json})
        
        cliente_id = data.get('cliente_id')
        if not cliente_id:
            cursor.execute("SELECT id FROM clientes WHERE empresa_id = %s LIMIT 1", (empresa_id,))
            row = cursor.fetchone()
            cliente_id = row['id'] if row else None
            if not cliente_id:
                cursor.execute("INSERT INTO clientes (rif, nombre, empresa_id) VALUES ('J-00000000-0', 'Cliente General', %s)", (empresa_id,))
                cliente_id = cursor.lastrowid
        
        cursor.execute("SELECT COALESCE(MAX(numero_factura_empresa), 0) + 1 FROM facturas_cabecera WHERE empresa_id = %s", (empresa_id,))
        result = cursor.fetchone()
        numero_factura_empresa = result['COALESCE(MAX(numero_factura_empresa), 0) + 1'] if result else 1
        
        cursor.execute("""
            INSERT INTO facturas_cabecera
            (fecha, cliente_id, usuario_id, caja_sesion_id, subtotal_usd, iva_usd, total_usd, tasa_cambio,
             metodo_pago, referencia, extras, moneda, monto_bs, estado, empresa_id,
             porcentaje_servicio, monto_servicio_usd, tipo_credito, numero_factura_empresa)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'activa', %s, %s, %s, %s, %s)
        """, (ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), cliente_id, usuario_id, caja_sesion_id, float(subtotal_usd - iva_total_usd), float(iva_total_usd),
              float(total_usd), tasa, metodo, referencia, extras, moneda, float(total_bs), empresa_id,
              float(porcentaje_servicio), float(monto_servicio), 1 if es_credito else 0, numero_factura_empresa))
        factura_id = cursor.lastrowid
        
        for det in productos_detalle:
            if det['tipo'] == 'receta':
                ingredientes = recetas_ingredientes.get(det['codigo'], [])
                for ing in ingredientes:
                    needed = ing['cantidad'] * det['cantidad']
                    cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (ing['codigo'], empresa_id))
                    ing_data = cursor.fetchone()
                    if not ing_data:
                        raise Exception(f"Ingrediente {ing['codigo']} no encontrado para la receta {det['codigo']}")
                    stock_ant = Decimal(str(ing_data['existencia']))
                    nuevo_stock = stock_ant - needed
                    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nuevo_stock), ing['codigo'], empresa_id))
                    nota_producto = det.get('nota_descuento', '') or det.get('nota_desc', '')
                    nota_completa = f"Factura #{numero_factura_empresa} - {'Gasto/Crédito' if (es_casa or es_credito) else 'Receta ' + det['codigo']}"
                    if nota_producto:
                        nota_completa += f" | Nota producto: {nota_producto}"
                    if nota_pago_texto:
                        nota_completa += f" | Nota pago: {nota_pago_texto}"
                    registrar_historial_inventario(cursor, ing['codigo'], ing_data['descripcion'], 
                                                   'reduccion' if (es_casa or es_credito) else 'venta', 
                                                   float(stock_ant), float(nuevo_stock), 
                                                   nota_completa)
            else:
                cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (det['codigo'], empresa_id))
                prod_data = cursor.fetchone()
                if not prod_data:
                    raise Exception(f"Producto {det['codigo']} no encontrado")
                stock_ant = Decimal(str(prod_data['existencia']))
                
                if det['codigo'].startswith('HAB_'):
                    nuevo_stock = Decimal('0')
                else:
                    nuevo_stock = stock_ant - det['cantidad']
                
                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nuevo_stock), det['codigo'], empresa_id))
                nota_producto = det.get('nota_descuento', '') or det.get('nota_desc', '')
                nota_completa = f"Factura #{numero_factura_empresa} {'Gasto/Crédito' if (es_casa or es_credito) else ''}"
                if nota_producto:
                    nota_completa += f" | Nota producto: {nota_producto}"
                if nota_pago_texto:
                    nota_completa += f" | Nota pago: {nota_pago_texto}"
                registrar_historial_inventario(cursor, det['codigo'], prod_data['descripcion'], 
                                               'reduccion' if (es_casa or es_credito) else 'venta', 
                                               float(stock_ant), float(nuevo_stock), 
                                               nota_completa)
            cursor.execute("""
                INSERT INTO facturas_detalle (factura_numero, producto_codigo, cantidad, precio_unitario, iva_unitario,
                    descuento, nota_descuento, subtotal_sin_iva, subtotal_con_iva, empresa_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (factura_id, det['codigo'], float(det['cantidad']), float(det['precio_unitario']), float(det['iva_unitario']),
                  float(det['descuento']), det['nota_desc'], float(det['subtotal_sin_iva']), float(det['subtotal_con_iva']), empresa_id))
        
        habitaciones_en_carrito = data.get('habitaciones_en_carrito', [])
        hotel_data = data.get('hotel')
        
        print(f"🔍 Procesando habitaciones: {habitaciones_en_carrito}")
        print(f"🔍 Hotel data: {hotel_data}")
        
        if habitaciones_en_carrito and len(habitaciones_en_carrito) > 0:
            for hab_info in habitaciones_en_carrito:
                codigo = hab_info.get('codigo')
                habitacion_id = hab_info.get('habitacion_id')
                numero = hab_info.get('numero', 'N/A')
                
                print(f"🏠 Procesando habitación ID: {habitacion_id}, Código: {codigo}, Número: {numero}")
                
                if habitacion_id:
                    cursor.execute("""
                        SELECT id, numero, codigo_producto FROM habitaciones 
                        WHERE id = %s AND empresa_id = %s
                    """, (habitacion_id, empresa_id))
                    habitacion = cursor.fetchone()
                    
                    if habitacion:
                        fecha_entrada = hotel_data.get('fecha_entrada') if hotel_data else None
                        fecha_salida = hotel_data.get('fecha_salida') if hotel_data else None
                        hora_entrada = hotel_data.get('hora_entrada', '15:00') if hotel_data else '15:00'
                        hora_salida = hotel_data.get('hora_salida', '12:00') if hotel_data else '12:00'
                        
                        print(f"✅ Habitación encontrada: {habitacion['numero']} - Actualizando a OCUPADA")
                        
                        cursor.execute("""
                            UPDATE habitaciones 
                            SET estado = 'ocupada',
                                fecha_entrada_ultima = %s,
                                fecha_salida_ultima = %s,
                                hora_entrada_ultima = %s,
                                hora_salida_ultima = %s,
                                observaciones = CONCAT(COALESCE(observaciones, ''), '\n', %s, ' - FACTURADA - Entrada: ', %s, ' Salida: ', %s)
                            WHERE id = %s AND empresa_id = %s
                        """, (
                            fecha_entrada, fecha_salida, 
                            hora_entrada, hora_salida,
                            ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'),
                            fecha_entrada or 'N/A', 
                            fecha_salida or 'N/A',
                            habitacion['id'], empresa_id
                        ))
                        
                        if habitacion.get('codigo_producto'):
                            cursor.execute("""
                                UPDATE productos 
                                SET existencia = 0 
                                WHERE codigo = %s AND empresa_id = %s
                            """, (habitacion['codigo_producto'], empresa_id))
                            print(f"✅ Stock de {habitacion['codigo_producto']} → 0")
                        
                        if fecha_entrada and fecha_salida:
                            cursor.execute("""
                                INSERT INTO disponibilidad_habitaciones (habitacion_id, fecha, estado, motivo, reserva_id, empresa_id)
                                SELECT %s, fecha_generada, 'no_disponible', 'Facturación directa', NULL, %s
                                FROM (
                                    SELECT DATE_ADD(%s, INTERVAL seq.seq DAY) as fecha_generada
                                    FROM (
                                        SELECT a.i + b.i * 10 + c.i * 100 as seq
                                        FROM (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a
                                        CROSS JOIN (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b
                                        CROSS JOIN (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) c
                                    ) seq
                                    WHERE DATE_ADD(%s, INTERVAL seq.seq DAY) < %s
                                ) fechas
                                ON DUPLICATE KEY UPDATE
                                estado = 'no_disponible'
                            """, (habitacion['id'], empresa_id, fecha_entrada, fecha_entrada, fecha_salida))
                            print(f"✅ Fechas bloqueadas en disponibilidad para habitación {habitacion['numero']} (solo período de estadía)")
                        
                        crear_alerta(empresa_id, 'habitacion_facturada', 
                                   f"🏠 Habitación {habitacion['numero']} - OCUPADA - Entrada: {fecha_entrada or 'N/A'} Salida: {fecha_salida or 'N/A'}")
                    else:
                        print(f"⚠️ No se encontró habitación con ID: {habitacion_id}")
                else:
                    print(f"⚠️ No hay habitacion_id para: {codigo}")
        
        elif hotel_data:
            for item in articulos:
                codigo_producto = item.get('producto_id', '')
                if codigo_producto.startswith('HAB_'):
                    print(f"🔍 Fallback: Buscando habitación por código: {codigo_producto}")
                    
                    cursor.execute("""
                        SELECT id, numero, codigo_producto FROM habitaciones 
                        WHERE codigo_producto = %s AND empresa_id = %s
                    """, (codigo_producto, empresa_id))
                    habitacion = cursor.fetchone()
                    
                    if habitacion:
                        fecha_entrada = hotel_data.get('fecha_entrada')
                        fecha_salida = hotel_data.get('fecha_salida')
                        hora_entrada = hotel_data.get('hora_entrada', '15:00')
                        hora_salida = hotel_data.get('hora_salida', '12:00')
                        
                        print(f"✅ Fallback: Habitación {habitacion['numero']} encontrada - Actualizando a OCUPADA")
                        
                        cursor.execute("""
                            UPDATE habitaciones 
                            SET estado = 'ocupada',
                                fecha_entrada_ultima = %s,
                                fecha_salida_ultima = %s,
                                hora_entrada_ultima = %s,
                                hora_salida_ultima = %s,
                                observaciones = CONCAT(COALESCE(observaciones, ''), '\n', %s, ' - FACTURADA - Entrada: ', %s, ' Salida: ', %s)
                            WHERE id = %s AND empresa_id = %s
                        """, (
                            fecha_entrada, fecha_salida, 
                            hora_entrada, hora_salida,
                            ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'),
                            fecha_entrada, fecha_salida,
                            habitacion['id'], empresa_id
                        ))
                        
                        if habitacion.get('codigo_producto'):
                            cursor.execute("""
                                UPDATE productos 
                                SET existencia = 0 
                                WHERE codigo = %s AND empresa_id = %s
                            """, (codigo_producto, empresa_id))
                            print(f"✅ Stock de {codigo_producto} → 0")
                        
                        if fecha_entrada and fecha_salida:
                            cursor.execute("""
                                INSERT INTO disponibilidad_habitaciones (habitacion_id, fecha, estado, motivo, reserva_id, empresa_id)
                                SELECT %s, fecha_generada, 'no_disponible', 'Facturación directa', NULL, %s
                                FROM (
                                    SELECT DATE_ADD(%s, INTERVAL seq.seq DAY) as fecha_generada
                                    FROM (
                                        SELECT a.i + b.i * 10 + c.i * 100 as seq
                                        FROM (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a
                                        CROSS JOIN (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b
                                        CROSS JOIN (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) c
                                    ) seq
                                    WHERE DATE_ADD(%s, INTERVAL seq.seq DAY) < %s
                                ) fechas
                                ON DUPLICATE KEY UPDATE
                                estado = 'no_disponible'
                            """, (habitacion['id'], empresa_id, fecha_entrada, fecha_entrada, fecha_salida))
                            print(f"✅ Fechas bloqueadas en disponibilidad para habitación {habitacion['numero']} (solo período de estadía)")
                        
                        crear_alerta(empresa_id, 'habitacion_facturada', 
                                   f"🏠 Habitación {habitacion['numero']} - OCUPADA - Entrada: {fecha_entrada} Salida: {fecha_salida}")
        
        for p in pagos:
            if p.get('metodo_pago') == 'Casa':
                monto_usd = 0
                monto_bs = 0
            else:
                monto_usd = float(p.get('monto', 0))
                moneda_pago = p.get('moneda', 'USD')
                if moneda_pago == 'USD':
                    monto_bs = monto_usd * tasa
                else:
                    monto_bs = monto_usd
                    monto_usd = monto_bs / tasa
            es_admin = p.get('es_administracion', False)
            cursor.execute("""
                INSERT INTO facturas_pagos (factura_numero, metodo_pago, monto_usd, monto_bs, referencia, nota, es_administracion)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (factura_id, p['metodo_pago'], monto_usd, monto_bs, p.get('referencia', ''), p.get('nota', ''), 1 if es_admin else 0))
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'factura', f"Factura #{numero_factura_empresa} creada por {request.username}" + 
                    (" (Gasto interno)" if es_casa else " (Crédito/Fiado)" if es_credito else ""))
        return jsonify({'status': 'OK', 'factura_id': factura_id, 'numero_factura': numero_factura_empresa}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/facturas', methods=['GET'])
@requiere_rol('cajero')
def listar_facturas():
    empresa_id = request.empresa_id
    search = request.args.get('search', '')
    fecha_desde = request.args.get('fecha_desde', '')
    fecha_hasta = request.args.get('fecha_hasta', '')
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    
    if limit > 500:
        limit = 500
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        count_query = """
            SELECT COUNT(*) as total
            FROM facturas_cabecera fc
            LEFT JOIN clientes c ON fc.cliente_id = c.id
            WHERE fc.empresa_id = %s
        """
        count_params = [empresa_id]
        
        if search:
            count_query += " AND (fc.numero_factura_empresa LIKE %s OR c.nombre LIKE %s)"
            count_params.extend([f'%{search}%', f'%{search}%'])
        if fecha_desde:
            count_query += " AND DATE(fc.fecha) >= %s"
            count_params.append(fecha_desde)
        if fecha_hasta:
            count_query += " AND DATE(fc.fecha) <= %s"
            count_params.append(fecha_hasta)
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        query = """
            SELECT fc.numero AS id, fc.numero_factura_empresa AS numero_factura, 
                   fc.fecha, COALESCE(c.nombre, 'Cliente General') AS cliente_nombre,
                   fc.tasa_cambio, fc.total_usd, fc.monto_bs, fc.metodo_pago, fc.referencia, fc.estado,
                   u.username as cajero,
                   fc.porcentaje_servicio, fc.monto_servicio_usd,
                   CASE WHEN fc.metodo_pago = 'Casa' THEN '🏠 Gasto interno' 
                        WHEN fc.metodo_pago = 'Credito' THEN '💳 Crédito/Fiado'
                        ELSE 'Venta' END as tipo_factura,
                   fc.extras
            FROM facturas_cabecera fc
            LEFT JOIN clientes c ON fc.cliente_id = c.id
            LEFT JOIN usuarios u ON fc.usuario_id = u.id
            WHERE fc.empresa_id = %s
        """
        params = [empresa_id]
        
        if search:
            query += " AND (fc.numero_factura_empresa LIKE %s OR c.nombre LIKE %s)"
            params.extend([f'%{search}%', f'%{search}%'])
        if fecha_desde:
            query += " AND DATE(fc.fecha) >= %s"
            params.append(fecha_desde)
        if fecha_hasta:
            query += " AND DATE(fc.fecha) <= %s"
            params.append(fecha_hasta)
        
        query += " ORDER BY fc.fecha DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        
        cursor.execute(query, params)
        facturas = cursor.fetchall()
        
        for f in facturas:
            if f['fecha']:
                f['fecha'] = f['fecha'].strftime('%Y-%m-%d %H:%M:%S')
            f['tasa_cambio'] = float(f['tasa_cambio'] or 0)
            f['total_usd'] = float(f['total_usd'] or 0)
            f['monto_bs'] = float(f['monto_bs'] or 0)
            f['monto_servicio_usd'] = float(f['monto_servicio_usd'] or 0)
            f['porcentaje_servicio'] = float(f['porcentaje_servicio'] or 0)
        
        safe_close_conn(conn, cursor)
        return jsonify({
            'data': facturas,
            'pagination': {
                'total': total,
                'limit': limit,
                'offset': offset
            }
        }), 200
        
    except Exception as e:
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/facturas/<int:id>', methods=['GET'])
@requiere_rol('cajero')
def detalle_factura(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT fc.numero, fc.numero_factura_empresa, fc.fecha, fc.subtotal_usd, fc.iva_usd, fc.total_usd, fc.tasa_cambio,
               fc.metodo_pago, fc.referencia, fc.extras, fc.moneda, fc.monto_bs, fc.estado,
               fc.porcentaje_servicio, fc.monto_servicio_usd, fc.tipo_credito,
               c.nombre AS cliente_nombre, c.rif AS cliente_rif, c.telefono AS cliente_telefono,
               c.direccion AS cliente_direccion, u.username as cajero
        FROM facturas_cabecera fc
        LEFT JOIN clientes c ON fc.cliente_id = c.id
        LEFT JOIN usuarios u ON fc.usuario_id = u.id
        WHERE fc.numero = %s AND fc.empresa_id = %s
    """, (id, empresa_id))
    factura = cursor.fetchone()
    if not factura:
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'Factura no encontrada'}), 404
    
    cursor.execute("SELECT nombre, rif, direccion, telefono FROM empresas WHERE id = %s", (empresa_id,))
    factura['empresa'] = cursor.fetchone() or {}
    
    if factura.get('extras'):
        try:
            extras = json.loads(factura['extras'])
            factura['hotel'] = extras.get('hotel')
        except:
            factura['hotel'] = None
    
    cursor.execute("""
        SELECT p.descripcion AS nombre, fd.cantidad, fd.precio_unitario, fd.iva_unitario,
               fd.descuento, fd.nota_descuento, fd.subtotal_sin_iva, fd.subtotal_con_iva
        FROM facturas_detalle fd
        JOIN productos p ON fd.producto_codigo = p.codigo AND p.empresa_id = %s
        WHERE fd.factura_numero = %s
    """, (empresa_id, id))
    productos = cursor.fetchall()
    for p in productos:
        for key in ['precio_unitario', 'iva_unitario', 'descuento', 'subtotal_sin_iva', 'subtotal_con_iva']:
            if p[key] is not None:
                p[key] = float(p[key])
    cursor.execute("SELECT metodo_pago, monto_usd, monto_bs, referencia, nota, es_administracion FROM facturas_pagos WHERE factura_numero = %s", (id,))
    pagos = cursor.fetchall()
    for p in pagos:
        p['monto_usd'] = float(p['monto_usd'] or 0)
        p['monto_bs'] = float(p['monto_bs'] or 0)
        p['es_administracion'] = bool(p['es_administracion'] or 0)
    safe_close_conn(conn, cursor)
    return jsonify({'factura': factura, 'productos': productos, 'pagos': pagos}), 200

@app.route('/api/facturas/anular/<int:id>', methods=['POST'])
@requiere_rol('admin')
def anular_factura(id):
    data = request.json
    motivo = data.get('motivo', '').strip()
    if not motivo:
        return jsonify({'error': 'Debes proporcionar un motivo para anular la factura'}), 400
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        tiene_numero_factura = columna_existe(conn, 'facturas_cabecera', 'numero_factura_empresa')
        
        if tiene_numero_factura:
            cursor.execute("SELECT estado, numero_factura_empresa FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        else:
            cursor.execute("SELECT estado FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        
        factura = cursor.fetchone()
        if not factura or factura['estado'] == 'anulada':
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Factura no encontrada o ya anulada'}), 400
        
        numero_factura = factura.get('numero_factura_empresa', id) if tiene_numero_factura else id
        
        cursor.execute("SELECT producto_codigo, cantidad FROM facturas_detalle WHERE factura_numero = %s", (id,))
        detalles = cursor.fetchall()
        for det in detalles:
            codigo = det['producto_codigo']
            cantidad = Decimal(str(det['cantidad']))
            cursor.execute("SELECT tipo_producto FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
            result = cursor.fetchone()
            if not result:
                continue
            tipo = result['tipo_producto']
            if tipo == 'receta':
                cursor.execute("""
                    SELECT rd.producto_codigo, rd.cantidad_necesaria
                    FROM recetas r
                    JOIN recetas_detalle rd ON r.id = rd.receta_id
                    WHERE r.codigo = %s AND r.empresa_id = %s
                """, (codigo, empresa_id))
                ingredientes = cursor.fetchall()
                for ing in ingredientes:
                    needed = Decimal(str(ing['cantidad_necesaria'])) * cantidad
                    cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (ing['producto_codigo'], empresa_id))
                    prod = cursor.fetchone()
                    if prod:
                        nueva_cantidad = Decimal(str(prod['existencia'])) + needed
                        cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nueva_cantidad), ing['producto_codigo'], empresa_id))
                        registrar_historial_inventario(cursor, ing['producto_codigo'], prod['descripcion'], 'anulacion',
                                                       float(prod['existencia']), float(nueva_cantidad), f"Anulación factura #{numero_factura} - Receta {codigo} - Motivo: {motivo}")
            else:
                cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
                prod = cursor.fetchone()
                if prod:
                    nueva_cantidad = Decimal(str(prod['existencia'])) + cantidad
                    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nueva_cantidad), codigo, empresa_id))
                    registrar_historial_inventario(cursor, codigo, prod['descripcion'], 'anulacion',
                                                   float(prod['existencia']), float(nueva_cantidad), f"Anulación factura #{numero_factura} - Motivo: {motivo}")
        cursor.execute("UPDATE facturas_cabecera SET estado = 'anulada', motivo_anulacion = %s WHERE numero = %s AND empresa_id = %s", (motivo, id, empresa_id))
        conn.commit()
        invalidar_cache_productos(empresa_id)
        return jsonify({'status': 'OK', 'mensaje': 'Factura anulada y stock restaurado'}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/facturas/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_factura(id):
    data = request.json
    motivo = data.get('motivo', '').strip()
    if not motivo:
        return jsonify({'error': 'Debes proporcionar un motivo para eliminar la factura'}), 400
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        tiene_numero_factura = columna_existe(conn, 'facturas_cabecera', 'numero_factura_empresa')
        
        if tiene_numero_factura:
            cursor.execute("SELECT estado, numero_factura_empresa FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        else:
            cursor.execute("SELECT estado FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        
        factura = cursor.fetchone()
        if not factura:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Factura no encontrada'}), 404
            
        numero_factura = factura.get('numero_factura_empresa', id) if tiene_numero_factura else id
            
        if factura['estado'] == 'activa':
            cursor.execute("SELECT producto_codigo, cantidad FROM facturas_detalle WHERE factura_numero = %s", (id,))
            detalles = cursor.fetchall()
            for det in detalles:
                codigo = det['producto_codigo']
                cantidad = Decimal(str(det['cantidad']))
                cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
                prod = cursor.fetchone()
                if prod:
                    nueva_cantidad = Decimal(str(prod['existencia'])) + cantidad
                    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nueva_cantidad), codigo, empresa_id))
                    registrar_historial_inventario(cursor, codigo, prod['descripcion'], 'eliminacion_factura',
                                                   float(prod['existencia']), float(nueva_cantidad), f"Eliminación física factura #{numero_factura} - Motivo: {motivo}")
        
        cursor.execute("DELETE FROM facturas_detalle WHERE factura_numero = %s", (id,))
        cursor.execute("DELETE FROM facturas_pagos WHERE factura_numero = %s", (id,))
        cursor.execute("DELETE FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        invalidar_cache_productos(empresa_id)
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== ALERTAS ==========
@app.route('/api/alertas', methods=['GET'])
@requiere_rol('cajero')
def obtener_alertas():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        verificar_y_crear_tabla_alertas()
        cursor.execute("SELECT * FROM alertas WHERE empresa_id = %s AND leida = 0 ORDER BY fecha DESC", (empresa_id,))
        alertas = cursor.fetchall()
        safe_close_conn(conn, cursor)
        return jsonify(alertas), 200
    except Exception as e:
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/alertas/marcar-leida/<int:id>', methods=['POST'])
@requiere_rol('cajero')
def marcar_alerta_leida(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        verificar_y_crear_tabla_alertas()
        cursor.execute("UPDATE alertas SET leida = 1 WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

# ========== REPORTE X ==========
@app.route('/api/estadisticas/balance', methods=['GET'])
@requiere_rol('cajero')
def estadisticas_balance():
    empresa_id = request.empresa_id
    periodo = request.args.get('periodo', 'dia')
    try:
        offset = int(request.args.get('offset', 0))
    except ValueError:
        offset = 0

    hoy = ahora_venezuela().date()

    def rango_periodo(periodo, offset):
        if periodo == 'dia':
            dia = hoy - timedelta(days=offset)
            return dia, dia, dia.strftime('%d/%m/%Y')
        elif periodo == 'semana':
            inicio_semana_actual = hoy - timedelta(days=hoy.weekday())
            desde = inicio_semana_actual - timedelta(weeks=offset)
            hasta = desde + timedelta(days=6)
            return desde, hasta, f"{desde.strftime('%d/%m')} al {hasta.strftime('%d/%m/%Y')}"
        elif periodo == 'mes':
            mes_idx = (hoy.year * 12 + (hoy.month - 1)) - offset
            anio_calc, mes_calc = divmod(mes_idx, 12)
            mes_calc += 1
            desde = date(anio_calc, mes_calc, 1)
            ultimo_dia = calendar.monthrange(anio_calc, mes_calc)[1]
            hasta = date(anio_calc, mes_calc, ultimo_dia)
            meses_es = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre']
            return desde, hasta, f"{meses_es[mes_calc-1]} {anio_calc}"
        elif periodo == 'anio':
            anio_calc = hoy.year - offset
            desde = date(anio_calc, 1, 1)
            hasta = date(anio_calc, 12, 31)
            return desde, hasta, str(anio_calc)
        else:
            raise ValueError('periodo inválido')

    try:
        desde_actual, hasta_actual, label_actual = rango_periodo(periodo, offset)
        desde_anterior, hasta_anterior, label_anterior = rango_periodo(periodo, offset + 1)
    except ValueError:
        return jsonify({'error': "El parámetro 'periodo' debe ser: dia, semana, mes o anio"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        def totales_rango(desde, hasta):
            cursor.execute("""
                SELECT COALESCE(SUM(total_usd), 0) as total_usd, COUNT(*) as cantidad
                FROM facturas_cabecera
                WHERE DATE(fecha) BETWEEN %s AND %s
                AND estado = 'activa'
                AND metodo_pago NOT IN ('Casa', 'Credito')
                AND empresa_id = %s
            """, (desde, hasta, empresa_id))
            row = cursor.fetchone()
            return float(row['total_usd'] or 0), int(row['cantidad'] or 0)

        total_actual, cantidad_actual = totales_rango(desde_actual, hasta_actual)
        total_anterior, cantidad_anterior = totales_rango(desde_anterior, hasta_anterior)

        if total_anterior > 0:
            variacion_pct = round(((total_actual - total_anterior) / total_anterior) * 100, 1)
        elif total_actual > 0:
            variacion_pct = 100.0
        else:
            variacion_pct = 0.0

        return jsonify({
            'periodo': periodo,
            'offset': offset,
            'actual': {
                'desde': desde_actual.strftime('%Y-%m-%d'), 'hasta': hasta_actual.strftime('%Y-%m-%d'),
                'label': label_actual, 'total_usd': total_actual, 'cantidad_facturas': cantidad_actual
            },
            'anterior': {
                'desde': desde_anterior.strftime('%Y-%m-%d'), 'hasta': hasta_anterior.strftime('%Y-%m-%d'),
                'label': label_anterior, 'total_usd': total_anterior, 'cantidad_facturas': cantidad_anterior
            },
            'variacion_pct': variacion_pct
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)


@requiere_rol('cajero')
def reporte_detallado():
    empresa_id = request.empresa_id
    usuario_id = request.user_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT fc.numero AS factura_id, fc.numero_factura_empresa AS numero_factura, fc.fecha, c.nombre AS cliente, fc.metodo_pago, fc.referencia, fc.moneda,
               fc.porcentaje_servicio, fc.monto_servicio_usd,
               p.descripcion AS producto, fd.cantidad, fd.precio_unitario, fd.iva_unitario, fd.descuento, fd.nota_descuento,
               fd.subtotal_sin_iva, fd.subtotal_con_iva
        FROM facturas_cabecera fc
        LEFT JOIN clientes c ON fc.cliente_id = c.id
        JOIN facturas_detalle fd ON fc.numero = fd.factura_numero
        JOIN productos p ON fd.producto_codigo = p.codigo AND p.empresa_id = fc.empresa_id
        WHERE DATE(fc.fecha) = CURDATE() AND fc.estado = 'activa' AND fc.metodo_pago NOT IN ('Casa', 'Credito')
          AND fc.empresa_id = %s AND fc.usuario_id = %s
        ORDER BY fc.numero, p.descripcion
    """, (empresa_id, usuario_id))
    detalles = cursor.fetchall()
    for d in detalles:
        for key in ['precio_unitario', 'iva_unitario', 'descuento', 'subtotal_sin_iva', 'subtotal_con_iva', 'monto_servicio_usd']:
            if d[key] is not None:
                d[key] = float(d[key])
    cursor.execute("SELECT nombre, rif, correo, telefono, direccion, tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
    empresa = cursor.fetchone()
    tasa = float(empresa['tasa_cambio']) if empresa else 544.58
    total_usd = sum(d['subtotal_con_iva'] for d in detalles) if detalles else 0
    total_servicio = sum(d['monto_servicio_usd'] for d in detalles) if detalles else 0
    safe_close_conn(conn, cursor)
    return jsonify({
        'fecha_hora': ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'),
        'empresa': empresa,
        'tasa_cambio': tasa,
        'detalles': detalles,
        'total_usd': total_usd,
        'total_servicio_usd': total_servicio,
        'total_bs': (total_usd + total_servicio) * tasa
    }), 200

# ========== TOP PRODUCTOS ==========
@app.route('/api/top-productos', methods=['GET'])
@requiere_rol('cajero')
def top_productos():
    empresa_id = request.empresa_id
    periodo = request.args.get('periodo', 'semana')
    orden = request.args.get('orden', 'cantidad')
    limite = 5
    if periodo == 'hoy':
        fecha_inicio = ahora_venezuela().strftime('%Y-%m-%d')
        fecha_fin = fecha_inicio
    elif periodo == 'semana':
        fecha_inicio = (ahora_venezuela() - timedelta(days=7)).strftime('%Y-%m-%d')
        fecha_fin = ahora_venezuela().strftime('%Y-%m-%d')
    elif periodo == 'mes':
        fecha_inicio = (ahora_venezuela() - timedelta(days=30)).strftime('%Y-%m-%d')
        fecha_fin = ahora_venezuela().strftime('%Y-%m-%d')
    else:
        fecha_inicio = (ahora_venezuela() - timedelta(days=7)).strftime('%Y-%m-%d')
        fecha_fin = ahora_venezuela().strftime('%Y-%m-%d')
    if orden == 'cantidad':
        order_field = 'SUM(fd.cantidad) DESC'
        select_extra = 'SUM(fd.cantidad) as total_cantidad, SUM(fd.subtotal_con_iva) as total_usd'
    else:
        order_field = 'SUM(fd.subtotal_con_iva) DESC'
        select_extra = 'SUM(fd.subtotal_con_iva) as total_usd, SUM(fd.cantidad) as total_cantidad'
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    query = f"""
        SELECT p.descripcion AS nombre, {select_extra}
        FROM facturas_detalle fd
        JOIN facturas_cabecera fc ON fd.factura_numero = fc.numero
        JOIN productos p ON fd.producto_codigo = p.codigo AND p.empresa_id = fc.empresa_id
        WHERE DATE(fc.fecha) BETWEEN %s AND %s
          AND fc.estado = 'activa'
          AND fc.metodo_pago NOT IN ('Casa', 'Credito')
          AND fc.empresa_id = %s
        GROUP BY p.codigo, p.descripcion
        ORDER BY {order_field}
        LIMIT %s
    """
    cursor.execute(query, (fecha_inicio, fecha_fin, empresa_id, limite))
    resultados = cursor.fetchall()
    safe_close_conn(conn, cursor)
    for r in resultados:
        r['total_usd'] = float(r['total_usd']) if 'total_usd' in r else 0
        r['total_cantidad'] = int(r['total_cantidad']) if 'total_cantidad' in r else 0
    return jsonify(resultados), 200

# ========== HISTORIAL CIERRES ==========
@app.route('/api/historial-cierres', methods=['GET'])
@requiere_rol('cajero')
def historial_cierres():
    empresa_id = request.empresa_id
    user_role = request.role
    user_id = request.user_id
    usuario_id = request.args.get('usuario_id', type=int)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    if user_role == 'admin':
        if usuario_id:
            cursor.execute("""
                SELECT h.id, h.fecha_cierre, h.usuario_id, u.username,
                       h.total_usd, h.total_bs, h.datos, h.numero_reporte_empresa
                FROM historial_cierres h
                JOIN usuarios u ON h.usuario_id = u.id
                WHERE h.empresa_id = %s AND h.usuario_id = %s
                ORDER BY h.fecha_cierre DESC
            """, (empresa_id, usuario_id))
        else:
            cursor.execute("""
                SELECT h.id, h.fecha_cierre, h.usuario_id, u.username,
                       h.total_usd, h.total_bs, h.datos, h.numero_reporte_empresa
                FROM historial_cierres h
                JOIN usuarios u ON h.usuario_id = u.id
                WHERE h.empresa_id = %s
                ORDER BY h.fecha_cierre DESC
            """, (empresa_id,))
    else:
        cursor.execute("""
            SELECT h.id, h.fecha_cierre, h.usuario_id, u.username,
                   h.total_usd, h.total_bs, h.datos, h.numero_reporte_empresa
            FROM historial_cierres h
            JOIN usuarios u ON h.usuario_id = u.id
            WHERE h.empresa_id = %s AND h.usuario_id = %s
            ORDER BY h.fecha_cierre DESC
        """, (empresa_id, user_id))
    registros = cursor.fetchall()
    for r in registros:
        if r['fecha_cierre'] and hasattr(r['fecha_cierre'], 'strftime'):
            r['fecha_cierre'] = r['fecha_cierre'].strftime('%Y-%m-%d %H:%M:%S')
        r['datos'] = json.loads(r['datos'])
        r['total_usd'] = float(r['total_usd'] or 0)
        r['total_bs'] = float(r['total_bs'] or 0)
    safe_close_conn(conn, cursor)
    return jsonify(registros), 200

# ========== HISTORIAL INVENTARIO ==========
@app.route('/api/historial-inventario', methods=['GET'])
@requiere_rol('admin')
def obtener_historial_inventario():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, fecha, usuario, producto_codigo, producto_descripcion, tipo,
               cantidad_anterior, cantidad_nueva, nota
        FROM historial_inventario
        WHERE empresa_id = %s
        ORDER BY fecha DESC
    """, (empresa_id,))
    registros = cursor.fetchall()
    for r in registros:
        if r['fecha'] and hasattr(r['fecha'], 'strftime'):
            r['fecha'] = r['fecha'].strftime('%Y-%m-%d %H:%M:%S')
        r['cantidad_anterior'] = float(r['cantidad_anterior'] or 0)
        r['cantidad_nueva'] = float(r['cantidad_nueva'] or 0)
    safe_close_conn(conn, cursor)
    return jsonify(registros), 200

# ========== REINICIAR HISTORIAL DE INVENTARIO ==========
@app.route('/api/reiniciar-historial-inventario', methods=['POST'])
@requiere_rol('admin')
def reiniciar_historial_inventario():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    tiene_columna = columna_existe(conn, 'empresas', 'permite_reiniciar_historial')
    
    if not tiene_columna:
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'El Super Admin ha deshabilitado esta acción para tu empresa'}), 403
    
    cursor.execute("SELECT permite_reiniciar_historial FROM empresas WHERE id = %s", (empresa_id,))
    row = cursor.fetchone()
    if not row or not row.get('permite_reiniciar_historial', False):
        safe_close_conn(conn, cursor)
        return jsonify({'error': 'El Super Admin ha deshabilitado esta acción para tu empresa'}), 403
    
    try:
        cursor.execute("DELETE FROM historial_inventario WHERE empresa_id = %s", (empresa_id,))
        conn.commit()
        return jsonify({'status': 'OK', 'mensaje': 'Historial de inventario reiniciado correctamente'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== GRÁFICOS ==========
@app.route('/api/sales-stats', methods=['GET'])
@requiere_rol('cajero')
def sales_stats():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT DATE(fecha) as dia, SUM(monto_bs) as total_bs
        FROM facturas_cabecera
        WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) AND estado = 'activa' 
          AND metodo_pago NOT IN ('Casa', 'Credito')
          AND empresa_id = %s
        GROUP BY DATE(fecha)
        ORDER BY dia
    """, (empresa_id,))
    diarias = cursor.fetchall()
    for d in diarias:
        d['total_bs'] = float(d['total_bs'] or 0)
    cursor.execute("""
        SELECT metodo_pago, SUM(total_usd) as total_usd
        FROM facturas_cabecera
        WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND estado = 'activa'
          AND metodo_pago NOT IN ('Casa', 'Credito')
          AND empresa_id = %s
        GROUP BY metodo_pago
    """, (empresa_id,))
    por_metodo = cursor.fetchall()
    for p in por_metodo:
        p['total_usd'] = float(p['total_usd'] or 0)
    safe_close_conn(conn, cursor)
    return jsonify({'diarias': diarias, 'por_metodo': por_metodo}), 200

# ========== EXPORTACIONES ==========
def exportar_a_csv(consulta_sql, params, nombre_archivo):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(consulta_sql, params)
        datos = cursor.fetchall()
        columnas = [desc[0] for desc in cursor.description] if datos else []
        buffer = io.StringIO()
        escritor_csv = csv.writer(buffer)
        if columnas:
            escritor_csv.writerow(columnas)
        escritor_csv.writerows(datos)
        response = make_response(buffer.getvalue())
        response.headers['Content-Disposition'] = f'attachment; filename={nombre_archivo}.csv'
        response.headers['Content-type'] = 'text/csv'
        return response
    except Exception as err:
        return jsonify({'error': str(err)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/exportar/inventario', methods=['GET'])
@requiere_rol('admin')
def exportar_inventario():
    empresa_id = request.empresa_id
    consulta = """
        SELECT p.codigo AS 'CÓDIGO', p.descripcion AS 'DESCRIPCIÓN', p.categoria AS 'CATEGORÍA',
               p.precio_compra AS 'COSTO', p.precio_venta AS 'VENTA', p.iva AS 'IVA%',
               (p.precio_venta - p.precio_compra) AS 'GANANCIA', p.existencia AS 'STOCK',
               p.unidad_medida AS 'UNIDAD', p.tipo_producto AS 'TIPO'
        FROM productos p
        WHERE p.empresa_id = %s
        ORDER BY p.codigo
    """
    return exportar_a_csv(consulta, (empresa_id,), 'inventario_completo')

@app.route('/api/exportar/historial-inventario', methods=['GET'])
@requiere_rol('admin')
def exportar_historial_inventario():
    empresa_id = request.empresa_id
    consulta = """
        SELECT fecha, usuario, producto_codigo, producto_descripcion, tipo,
               cantidad_anterior, cantidad_nueva, nota
        FROM historial_inventario
        WHERE empresa_id = %s
        ORDER BY fecha DESC
    """
    return exportar_a_csv(consulta, (empresa_id,), 'historial_inventario')

@app.route('/api/exportar/facturas', methods=['GET'])
@requiere_rol('admin')
def exportar_facturas():
    empresa_id = request.empresa_id
    consulta = """
        SELECT fc.numero AS id, fc.numero_factura_empresa AS numero, fc.fecha, c.nombre, fc.moneda, fc.total_usd, fc.monto_bs,
               fc.metodo_pago, fc.referencia, fc.estado, u.username as cajero,
               fc.porcentaje_servicio, fc.monto_servicio_usd,
               CASE WHEN fc.metodo_pago = 'Casa' THEN 'Gasto interno' 
                    WHEN fc.metodo_pago = 'Credito' THEN 'Crédito/Fiado'
                    ELSE 'Venta' END as tipo
        FROM facturas_cabecera fc
        LEFT JOIN clientes c ON fc.cliente_id = c.id
        LEFT JOIN usuarios u ON fc.usuario_id = u.id
        WHERE fc.empresa_id = %s
        ORDER BY fc.numero DESC
    """
    return exportar_a_csv(consulta, (empresa_id,), 'historial_facturas')

# ========== PROVEEDORES ==========
@app.route('/api/proveedores', methods=['GET'])
@requiere_rol('admin')
def listar_proveedores():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM proveedores WHERE empresa_id = %s", (empresa_id,))
    proveedores = cursor.fetchall()
    safe_close_conn(conn, cursor)
    return jsonify(proveedores), 200

@app.route('/api/proveedores', methods=['POST'])
@requiere_rol('admin')
def crear_proveedor():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO proveedores (nombre, rif, telefono, email, direccion, empresa_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (data['nombre'], data.get('rif', ''), data.get('telefono', ''), data.get('email', ''), data.get('direccion', ''), empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 201

@app.route('/api/proveedores/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_proveedor(id):
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE proveedores 
        SET nombre=%s, rif=%s, telefono=%s, email=%s, direccion=%s
        WHERE id=%s AND empresa_id=%s
    """, (data['nombre'], data.get('rif', ''), data.get('telefono', ''), data.get('email', ''), data.get('direccion', ''), id, empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

@app.route('/api/proveedores/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_proveedor(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM proveedores WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

# ========== ÓRDENES DE COMPRA ==========
@app.route('/api/ordenes-compra', methods=['GET'])
@requiere_rol('admin')
def listar_ordenes_compra():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'ordenes_compra'
        """)
        columnas_oc = [row['COLUMN_NAME'] for row in cursor.fetchall()]
        
        select_fields = """
            oc.id, oc.proveedor_id, oc.fecha, oc.estado, oc.total_usd, 
            oc.empresa_id, p.nombre as proveedor_nombre
        """
        
        if 'fecha_entrega_estimada' in columnas_oc:
            select_fields += ", oc.fecha_entrega_estimada"
        else:
            select_fields += ", NULL as fecha_entrega_estimada"
            
        if 'fecha_recepcion' in columnas_oc:
            select_fields += ", oc.fecha_recepcion"
        else:
            select_fields += ", NULL as fecha_recepcion"
            
        if 'notas' in columnas_oc:
            select_fields += ", oc.notas"
        else:
            select_fields += ", NULL as notas"
        
        cursor.execute(f"""
            SELECT {select_fields}
            FROM ordenes_compra oc
            JOIN proveedores p ON oc.proveedor_id = p.id
            WHERE oc.empresa_id = %s
            ORDER BY oc.fecha DESC
        """, (empresa_id,))
        ordenes = cursor.fetchall()
        
        cursor.execute("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'ordenes_detalle'
        """)
        columnas_od = [row['COLUMN_NAME'] for row in cursor.fetchall()]
        
        for orden in ordenes:
            detalle_fields = """
                od.id, od.producto_codigo, od.cantidad, od.precio_unitario, od.subtotal,
                COALESCE(pr.descripcion, od.producto_codigo) as producto_nombre
            """
            
            if 'cantidad_recibida' in columnas_od:
                detalle_fields += ", od.cantidad_recibida"
                detalle_fields += """,
                    CASE 
                        WHEN od.cantidad_recibida IS NULL THEN 'pendiente'
                        WHEN od.cantidad_recibida >= od.cantidad THEN 'recibido'
                        WHEN od.cantidad_recibida > 0 THEN 'parcial'
                        ELSE 'faltante'
                    END as estado_recepcion
                """
            else:
                detalle_fields += ", NULL as cantidad_recibida, 'pendiente' as estado_recepcion"
                
            if 'es_producto_nuevo' in columnas_od:
                detalle_fields += ", od.es_producto_nuevo"
            else:
                detalle_fields += ", 0 as es_producto_nuevo"
                
            if 'producto_nuevo_nombre' in columnas_od:
                detalle_fields += ", od.producto_nuevo_nombre"
            else:
                detalle_fields += ", NULL as producto_nuevo_nombre"
                
            if 'producto_nuevo_categoria' in columnas_od:
                detalle_fields += ", od.producto_nuevo_categoria"
            else:
                detalle_fields += ", NULL as producto_nuevo_categoria"
                
            if 'producto_nuevo_unidad' in columnas_od:
                detalle_fields += ", od.producto_nuevo_unidad"
            else:
                detalle_fields += ", NULL as producto_nuevo_unidad"
                
            if 'observaciones' in columnas_od:
                detalle_fields += ", od.observaciones"
            else:
                detalle_fields += ", NULL as observaciones"
                
            if 'estado_detalle' in columnas_od:
                detalle_fields += ", od.estado_detalle"
            else:
                detalle_fields += ", 'pendiente' as estado_detalle"
            
            cursor.execute(f"""
                SELECT {detalle_fields}
                FROM ordenes_detalle od
                LEFT JOIN productos pr ON od.producto_codigo = pr.codigo AND pr.empresa_id = %s
                WHERE od.orden_id = %s
            """, (empresa_id, orden['id']))
            detalle = cursor.fetchall()
            
            for item in detalle:
                for key in ['precio_unitario', 'cantidad', 'subtotal', 'cantidad_recibida']:
                    if key in item and item[key] is not None:
                        try:
                            item[key] = float(item[key])
                        except (ValueError, TypeError):
                            item[key] = 0
            
            orden['detalle'] = detalle
            
        safe_close_conn(conn, cursor)
        return jsonify(ordenes), 200
    except Exception as e:
        safe_close_conn(conn, cursor)
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/ordenes-compra', methods=['POST'])
@requiere_rol('admin')
def crear_orden_compra():
    data = request.json
    empresa_id = request.empresa_id
    if not empresa_id:
        return jsonify({'error': 'Usuario sin empresa'}), 400

    proveedor_id = data.get('proveedor_id')
    detalle = data.get('detalle', [])
    total_usd = data.get('total_usd', 0)
    fecha_entrega = data.get('fecha_entrega_estimada')
    notas = data.get('notas', '')

    if not proveedor_id:
        return jsonify({'error': 'Proveedor requerido'}), 400
    if not detalle:
        return jsonify({'error': 'Detalle vacío'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            INSERT INTO ordenes_compra (proveedor_id, fecha, estado, total_usd, empresa_id, fecha_entrega_estimada, notas)
            VALUES (%s, %s, 'pendiente', %s, %s, %s, %s)
        """, (proveedor_id, ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), total_usd, empresa_id, fecha_entrega, notas))
        orden_id = cursor.lastrowid

        for item in detalle:
            codigo = item.get('codigo')
            cantidad = float(item.get('cantidad', 0))
            precio = float(item.get('precio', 0))
            es_nuevo = item.get('es_producto_nuevo', False)
            nombre_nuevo = item.get('nombre_nuevo', '')
            categoria_nueva = item.get('categoria_nueva', '')
            unidad_nueva = item.get('unidad_nueva', '')
            
            if not codigo or cantidad <= 0 or precio <= 0:
                conn.rollback()
                safe_close_conn(conn, cursor)
                return jsonify({'error': f'Producto inválido: {codigo}'}), 400

            cursor.execute("""
                INSERT INTO ordenes_detalle (
                    orden_id, producto_codigo, cantidad, precio_unitario, subtotal, empresa_id,
                    es_producto_nuevo, producto_nuevo_nombre, producto_nuevo_categoria, producto_nuevo_unidad
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (orden_id, codigo, cantidad, precio, cantidad * precio, empresa_id, 
                  1 if es_nuevo else 0, nombre_nuevo, categoria_nueva, unidad_nueva))

        conn.commit()
        crear_alerta(empresa_id, 'orden_compra', f"Orden de compra #{orden_id} creada.")
        return jsonify({'status': 'OK', 'id': orden_id}), 201
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/ordenes-compra/<int:id>/recibir', methods=['POST'])
@requiere_rol('admin')
def recibir_orden_compra(id):
    data = request.json
    empresa_id = request.empresa_id
    usuario_id = request.user_id
    productos = data.get('productos', [])
    observaciones = data.get('observaciones', '')
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT estado FROM ordenes_compra WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        orden = cursor.fetchone()
        if not orden:
            return jsonify({'error': 'Orden no encontrada'}), 404
        if orden['estado'] == 'cancelada':
            return jsonify({'error': 'La orden está cancelada'}), 400

        for item in productos:
            detalle_id = item.get('id')
            cantidad_recibida = float(item.get('cantidad_recibida', 0))
            estado_detalle = item.get('estado_detalle', 'pendiente')
            
            cursor.execute("""
                SELECT producto_codigo, cantidad, es_producto_nuevo, 
                       producto_nuevo_nombre, producto_nuevo_categoria, producto_nuevo_unidad,
                       precio_unitario
                FROM ordenes_detalle 
                WHERE id = %s AND orden_id = %s AND empresa_id = %s
            """, (detalle_id, id, empresa_id))
            detalle = cursor.fetchone()
            if not detalle:
                continue
                
            cursor.execute("""
                UPDATE ordenes_detalle 
                SET cantidad_recibida = %s, estado_detalle = %s
                WHERE id = %s
            """, (cantidad_recibida, estado_detalle, detalle_id))
            
            if detalle['es_producto_nuevo'] and cantidad_recibida > 0:
                codigo_nuevo = detalle['producto_codigo']
                cursor.execute("SELECT codigo FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo_nuevo, empresa_id))
                if not cursor.fetchone():
                    nombre = detalle['producto_nuevo_nombre'] or codigo_nuevo
                    categoria = detalle['producto_nuevo_categoria'] or 'otros'
                    unidad = detalle['producto_nuevo_unidad'] or 'unidad'
                    precio = detalle['precio_unitario']
                    
                    cursor.execute("""
                        INSERT INTO productos (codigo, descripcion, categoria, precio_compra, precio_venta, existencia, iva, unidad_medida, tipo_producto, empresa_id)
                        VALUES (%s, %s, %s, %s, %s, %s, 16, %s, 'normal', %s)
                    """, (codigo_nuevo, nombre, categoria, precio, precio * 1.3, cantidad_recibida, unidad, empresa_id))
                    
                    registrar_historial_inventario(
                        cursor, codigo_nuevo, nombre, 'ingreso_compra', 
                        0, cantidad_recibida, f"Creación desde orden #{id}"
                    )
            
            elif not detalle['es_producto_nuevo'] and cantidad_recibida > 0:
                codigo = detalle['producto_codigo']
                cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
                prod = cursor.fetchone()
                if prod:
                    stock_anterior = float(prod['existencia'] or 0)
                    nuevo_stock = stock_anterior + cantidad_recibida
                    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, codigo, empresa_id))
                    registrar_historial_inventario(
                        cursor, codigo, prod['descripcion'], 'ingreso_compra',
                        stock_anterior, nuevo_stock, f"Recepción orden #{id}"
                    )

        cursor.execute("""
            INSERT INTO recepciones_ordenes (orden_id, usuario_id, observaciones, empresa_id)
            VALUES (%s, %s, %s, %s)
        """, (id, usuario_id, observaciones, empresa_id))
        
        cursor.execute("""
            UPDATE ordenes_compra 
            SET estado = 'recibida', fecha_recepcion = %s
            WHERE id = %s
        """, (ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'recepcion', f"Orden #{id} recibida correctamente")
        return jsonify({'status': 'OK', 'mensaje': 'Orden recibida correctamente'}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/ordenes-compra/<int:id>/estado', methods=['PUT'])
@requiere_rol('admin')
def cambiar_estado_orden(id):
    data = request.json
    empresa_id = request.empresa_id
    nuevo_estado = data.get('estado')
    motivo = data.get('motivo', '')
    
    estados_validos = ['pendiente', 'enviada', 'parcial', 'recibida', 'cancelada']
    if nuevo_estado not in estados_validos:
        return jsonify({'error': 'Estado inválido'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT estado FROM ordenes_compra 
            WHERE id = %s AND empresa_id = %s
        """, (id, empresa_id))
        orden = cursor.fetchone()
        if not orden:
            return jsonify({'error': 'Orden no encontrada'}), 404
        
        estado_actual = orden['estado']
        
        if nuevo_estado == 'cancelada' and estado_actual != 'cancelada':
            cursor.execute("""
                SELECT producto_codigo, cantidad_recibida, es_producto_nuevo
                FROM ordenes_detalle 
                WHERE orden_id = %s AND cantidad_recibida > 0
            """, (id,))
            detalles_recibidos = cursor.fetchall()
            
            if detalles_recibidos:
                for det in detalles_recibidos:
                    cantidad_recibida = float(det['cantidad_recibida'] or 0)
                    if cantidad_recibida <= 0:
                        continue
                        
                    if det['es_producto_nuevo']:
                        cursor.execute("""
                            DELETE FROM productos 
                            WHERE codigo = %s AND empresa_id = %s
                        """, (det['producto_codigo'], empresa_id))
                        print(f"✅ Producto nuevo {det['producto_codigo']} eliminado por cancelación de orden")
                    else:
                        cursor.execute("""
                            SELECT existencia, descripcion 
                            FROM productos 
                            WHERE codigo = %s AND empresa_id = %s
                        """, (det['producto_codigo'], empresa_id))
                        prod = cursor.fetchone()
                        if prod:
                            stock_actual = float(prod['existencia'] or 0)
                            nuevo_stock = stock_actual - cantidad_recibida
                            
                            if nuevo_stock < 0:
                                nuevo_stock = 0
                            
                            cursor.execute("""
                                UPDATE productos 
                                SET existencia = %s 
                                WHERE codigo = %s AND empresa_id = %s
                            """, (nuevo_stock, det['producto_codigo'], empresa_id))
                            
                            registrar_historial_inventario(
                                cursor, 
                                det['producto_codigo'], 
                                prod['descripcion'], 
                                'cancelacion_orden',
                                stock_actual, 
                                nuevo_stock, 
                                f"Cancelación de orden #{id} - Stock revertido"
                            )
                            print(f"✅ Stock revertido para {det['producto_codigo']}: {stock_actual} → {nuevo_stock}")
            
            cursor.execute("""
                UPDATE ordenes_detalle 
                SET estado_detalle = 'cancelado' 
                WHERE orden_id = %s
            """, (id,))
        
        elif nuevo_estado == 'recibida' and estado_actual != 'recibida':
            cursor.execute("""
                SELECT id, producto_codigo, cantidad, cantidad_recibida, es_producto_nuevo,
                       producto_nuevo_nombre, producto_nuevo_categoria, producto_nuevo_unidad,
                       precio_unitario
                FROM ordenes_detalle 
                WHERE orden_id = %s AND (cantidad_recibida IS NULL OR cantidad_recibida < cantidad)
            """, (id,))
            pendientes = cursor.fetchall()
            
            if pendientes:
                nuevo_estado = 'parcial'
                
                for det in pendientes:
                    cantidad_pendiente = float(det['cantidad']) - float(det['cantidad_recibida'] or 0)
                    if cantidad_pendiente > 0:
                        if det['es_producto_nuevo']:
                            codigo_nuevo = det['producto_codigo']
                            cursor.execute("SELECT codigo FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo_nuevo, empresa_id))
                            if not cursor.fetchone():
                                nombre = det['producto_nuevo_nombre'] or codigo_nuevo
                                categoria = det['producto_nuevo_categoria'] or 'otros'
                                unidad = det['producto_nuevo_unidad'] or 'unidad'
                                precio = det['precio_unitario']
                                
                                cursor.execute("""
                                    INSERT INTO productos (codigo, descripcion, categoria, precio_compra, precio_venta, existencia, iva, unidad_medida, tipo_producto, empresa_id)
                                    VALUES (%s, %s, %s, %s, %s, %s, 16, %s, 'normal', %s)
                                """, (codigo_nuevo, nombre, categoria, precio, precio * 1.3, cantidad_pendiente, unidad, empresa_id))
                        else:
                            codigo = det['producto_codigo']
                            cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
                            prod = cursor.fetchone()
                            if prod:
                                stock_actual = float(prod['existencia'] or 0)
                                nuevo_stock = stock_actual + cantidad_pendiente
                                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, codigo, empresa_id))
                        
                        cursor.execute("""
                            UPDATE ordenes_detalle 
                            SET cantidad_recibida = COALESCE(cantidad_recibida, 0) + %s,
                                estado_detalle = 'recibido'
                            WHERE id = %s
                        """, (cantidad_pendiente, det['id']))

        cursor.execute("""
            UPDATE ordenes_compra 
            SET estado = %s, 
                notas = CONCAT(COALESCE(notas, ''), '\n', %s, ' - Estado cambiado de "', %s, '" a "', %s, '" por ', %s)
            WHERE id = %s AND empresa_id = %s
        """, (nuevo_estado, ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), estado_actual, nuevo_estado, request.username, id, empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'orden_estado', f"Orden #{id} cambió de '{estado_actual}' a '{nuevo_estado}'")
        
        return jsonify({'status': 'OK', 'mensaje': f'Estado cambiado a {nuevo_estado}'}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/ordenes-compra/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_orden_compra(id):
    data = request.json
    motivo = data.get('motivo', '')
    empresa_id = request.empresa_id
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT estado FROM ordenes_compra WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        orden = cursor.fetchone()
        if not orden:
            return jsonify({'error': 'Orden no encontrada'}), 404
            
        if orden['estado'] == 'recibida':
            cursor.execute("""
                SELECT producto_codigo, cantidad_recibida, es_producto_nuevo
                FROM ordenes_detalle 
                WHERE orden_id = %s AND cantidad_recibida > 0
            """, (id,))
            detalles = cursor.fetchall()
            for det in detalles:
                if det['es_producto_nuevo']:
                    cursor.execute("DELETE FROM productos WHERE codigo = %s AND empresa_id = %s", (det['producto_codigo'], empresa_id))
                else:
                    cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (det['producto_codigo'], empresa_id))
                    prod = cursor.fetchone()
                    if prod:
                        nuevo_stock = float(prod['existencia']) - float(det['cantidad_recibida'])
                        cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, det['producto_codigo'], empresa_id))
                        registrar_historial_inventario(
                            cursor, det['producto_codigo'], prod['descripcion'], 'eliminacion_factura',
                            float(prod['existencia']), nuevo_stock, f"Eliminación orden #{id} - Motivo: {motivo}"
                        )
        
        cursor.execute("DELETE FROM ordenes_detalle WHERE orden_id = %s", (id,))
        cursor.execute("DELETE FROM recepciones_ordenes WHERE orden_id = %s", (id,))
        cursor.execute("DELETE FROM ordenes_compra WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        invalidar_cache_productos(empresa_id)
        return jsonify({'status': 'OK', 'mensaje': 'Orden eliminada correctamente'}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== DISPONIBILIDAD DE HABITACIONES ==========

@app.route('/api/disponibilidad/<int:habitacion_id>', methods=['GET'])
@requiere_rol('cajero')
def obtener_disponibilidad_habitacion(habitacion_id):
    """Obtiene las fechas no disponibles para una habitación"""
    empresa_id = request.empresa_id
    fecha_inicio = request.args.get('fecha_inicio')
    fecha_fin = request.args.get('fecha_fin')
    
    if not fecha_inicio:
        fecha_inicio = ahora_venezuela().strftime('%Y-%m-%d')
    if not fecha_fin:
        fecha_fin = (ahora_venezuela() + timedelta(days=90)).strftime('%Y-%m-%d')
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'disponibilidad_habitaciones'
        """)
        if cursor.fetchone()['COUNT(*)'] == 0:
            verificar_y_crear_tabla_disponibilidad()
            safe_close_conn(conn, cursor)
            return jsonify({'bloqueos': [], 'reservas': []}), 200
        
        cursor.execute("""
            SELECT fecha, estado, motivo, reserva_id
            FROM disponibilidad_habitaciones
            WHERE habitacion_id = %s 
            AND empresa_id = %s
            AND fecha BETWEEN %s AND %s
            ORDER BY fecha
        """, (habitacion_id, empresa_id, fecha_inicio, fecha_fin))
        
        disponibles = cursor.fetchall()
        
        cursor.execute("""
            SELECT fecha_entrada as fecha_inicio, fecha_salida as fecha_fin, estado
            FROM reservas
            WHERE habitacion_id = %s 
            AND empresa_id = %s
            AND estado IN ('pendiente', 'confirmada', 'abonada', 'check_in')
            AND fecha_salida >= %s
            AND fecha_entrada <= %s
        """, (habitacion_id, empresa_id, fecha_inicio, fecha_fin))
        
        reservas = cursor.fetchall()
        
        for b in disponibles:
            if b.get('fecha') is not None:
                b['fecha'] = b['fecha'].strftime('%Y-%m-%d') if hasattr(b['fecha'], 'strftime') else str(b['fecha'])
        for r in reservas:
            if r.get('fecha_inicio') is not None:
                r['fecha_inicio'] = r['fecha_inicio'].strftime('%Y-%m-%d') if hasattr(r['fecha_inicio'], 'strftime') else str(r['fecha_inicio'])
            if r.get('fecha_fin') is not None:
                r['fecha_fin'] = r['fecha_fin'].strftime('%Y-%m-%d') if hasattr(r['fecha_fin'], 'strftime') else str(r['fecha_fin'])
        
        safe_close_conn(conn, cursor)
        return jsonify({
            'bloqueos': disponibles,
            'reservas': reservas
        }), 200
    except Exception as e:
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/disponibilidad/<int:habitacion_id>/bloquear', methods=['POST'])
@requiere_rol('admin')
def bloquear_fecha_habitacion(habitacion_id):
    """Bloquea una fecha específica para una habitación"""
    data = request.json
    empresa_id = request.empresa_id
    fecha = data.get('fecha')
    motivo = data.get('motivo', 'Bloqueo manual')
    
    if not fecha:
        return jsonify({'error': 'Fecha requerida'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'disponibilidad_habitaciones'
        """)
        if cursor.fetchone()['COUNT(*)'] == 0:
            verificar_y_crear_tabla_disponibilidad()
        
        cursor.execute("""
            INSERT INTO disponibilidad_habitaciones (habitacion_id, fecha, estado, motivo, empresa_id)
            VALUES (%s, %s, 'no_disponible', %s, %s)
            ON DUPLICATE KEY UPDATE
            estado = 'no_disponible', motivo = %s
        """, (habitacion_id, fecha, motivo, empresa_id, motivo))
        conn.commit()
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK', 'mensaje': f'Fecha {fecha} bloqueada'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/disponibilidad/<int:habitacion_id>/liberar', methods=['POST'])
@requiere_rol('admin')
def liberar_fecha_habitacion(habitacion_id):
    """Libera una fecha específica para una habitación"""
    data = request.json
    empresa_id = request.empresa_id
    fecha = data.get('fecha')
    
    if not fecha:
        return jsonify({'error': 'Fecha requerida'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            DELETE FROM disponibilidad_habitaciones
            WHERE habitacion_id = %s AND fecha = %s AND empresa_id = %s
        """, (habitacion_id, fecha, empresa_id))
        conn.commit()
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK', 'mensaje': f'Fecha {fecha} liberada'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/disponibilidad/<int:habitacion_id>/verificar', methods=['POST'])
@requiere_rol('cajero')
def verificar_disponibilidad_habitacion(habitacion_id):
    """Verifica si una habitación está disponible para un rango de fechas"""
    data = request.json
    empresa_id = request.empresa_id
    fecha_entrada = data.get('fecha_entrada')
    fecha_salida = data.get('fecha_salida')
    
    if not fecha_entrada or not fecha_salida:
        return jsonify({'error': 'Fechas requeridas'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'disponibilidad_habitaciones'
        """)
        if cursor.fetchone()['COUNT(*)'] == 0:
            verificar_y_crear_tabla_disponibilidad()
            safe_close_conn(conn, cursor)
            return jsonify({'disponible': True, 'bloqueadas': 0, 'reservas': 0}), 200
        
        cursor.execute("""
            SELECT COUNT(*) as total
            FROM disponibilidad_habitaciones
            WHERE habitacion_id = %s 
            AND empresa_id = %s
            AND fecha >= %s AND fecha < %s
            AND estado = 'no_disponible'
        """, (habitacion_id, empresa_id, fecha_entrada, fecha_salida))
        
        bloqueadas = cursor.fetchone()['total']
        
        cursor.execute("""
            SELECT COUNT(*) as total
            FROM reservas
            WHERE habitacion_id = %s 
            AND empresa_id = %s
            AND estado IN ('pendiente', 'confirmada', 'abonada', 'check_in')
            AND fecha_entrada < %s AND fecha_salida > %s
        """, (habitacion_id, empresa_id, fecha_salida, fecha_entrada))
        
        reservas = cursor.fetchone()['total']
        
        disponible = (bloqueadas == 0 and reservas == 0)
        
        safe_close_conn(conn, cursor)
        return jsonify({
            'disponible': disponible,
            'bloqueadas': bloqueadas,
            'reservas': reservas
        }), 200
    except Exception as e:
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/disponibilidad/auto-bloquear-reserva', methods=['POST'])
@requiere_rol('cajero')
def auto_bloquear_reserva():
    """Bloquea automáticamente las fechas de una reserva"""
    data = request.json
    reserva_id = data.get('reserva_id')
    empresa_id = request.empresa_id
    
    if not reserva_id:
        return jsonify({'error': 'Reserva ID requerido'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'disponibilidad_habitaciones'
        """)
        if cursor.fetchone()['COUNT(*)'] == 0:
            verificar_y_crear_tabla_disponibilidad()
        
        cursor.execute("""
            SELECT habitacion_id, fecha_entrada, fecha_salida
            FROM reservas
            WHERE id = %s AND empresa_id = %s
        """, (reserva_id, empresa_id))
        reserva = cursor.fetchone()
        
        if not reserva:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Reserva no encontrada'}), 404
        
        fecha_actual = datetime.strptime(reserva['fecha_entrada'], '%Y-%m-%d')
        fecha_salida = datetime.strptime(reserva['fecha_salida'], '%Y-%m-%d')
        
        while fecha_actual <= fecha_salida:
            cursor.execute("""
                INSERT INTO disponibilidad_habitaciones (habitacion_id, fecha, estado, motivo, reserva_id, empresa_id)
                VALUES (%s, %s, 'no_disponible', 'Reserva confirmada', %s, %s)
                ON DUPLICATE KEY UPDATE
                estado = 'no_disponible', reserva_id = %s
            """, (reserva['habitacion_id'], fecha_actual.strftime('%Y-%m-%d'), reserva_id, empresa_id, reserva_id))
            fecha_actual += timedelta(days=1)
        
        conn.commit()
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK', 'mensaje': 'Fechas bloqueadas automáticamente'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/disponibilidad/auto-liberar-reserva', methods=['POST'])
@requiere_rol('cajero')
def auto_liberar_reserva():
    """Libera automáticamente las fechas de una reserva cancelada"""
    data = request.json
    reserva_id = data.get('reserva_id')
    empresa_id = request.empresa_id
    
    if not reserva_id:
        return jsonify({'error': 'Reserva ID requerido'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            DELETE FROM disponibilidad_habitaciones
            WHERE reserva_id = %s AND empresa_id = %s
        """, (reserva_id, empresa_id))
        conn.commit()
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK', 'mensaje': 'Fechas liberadas'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

# ========== HOTELERÍA ==========

# ---------- HABITACIONES ----------
@app.route('/api/habitaciones', methods=['GET'])
@requiere_rol('cajero')
def listar_habitaciones():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, numero, nombre, tipo, piso, capacidad, 
                   precio_noche, precio_dia, descripcion, observaciones,
                   estado, activa, codigo_producto,
                   fecha_entrada_ultima, fecha_salida_ultima,
                   hora_entrada_ultima, hora_salida_ultima
            FROM habitaciones 
            WHERE empresa_id = %s AND activa = 1
            ORDER BY numero
        """, (empresa_id,))
        habitaciones = cursor.fetchall()
        
        for h in habitaciones:
            if h.get('precio_noche') is not None:
                h['precio_noche'] = float(h['precio_noche'])
            if h.get('precio_dia') is not None:
                h['precio_dia'] = float(h['precio_dia'])
            if h.get('fecha_entrada_ultima') and isinstance(h['fecha_entrada_ultima'], date):
                h['fecha_entrada_ultima'] = h['fecha_entrada_ultima'].strftime('%Y-%m-%d')
            if h.get('fecha_salida_ultima') and isinstance(h['fecha_salida_ultima'], date):
                h['fecha_salida_ultima'] = h['fecha_salida_ultima'].strftime('%Y-%m-%d')
            if h.get('hora_entrada_ultima') is not None:
                h['hora_entrada_ultima'] = formatear_hora(h['hora_entrada_ultima'])
            if h.get('hora_salida_ultima') is not None:
                h['hora_salida_ultima'] = formatear_hora(h['hora_salida_ultima'])
        
        safe_close_conn(conn, cursor)
        return jsonify(habitaciones), 200
    except Exception as e:
        safe_close_conn(conn, cursor)
        print(f"❌ Error en listar_habitaciones: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/habitaciones', methods=['POST'])
@requiere_rol('admin')
def crear_habitacion():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        codigo_producto = data.get('codigo_producto', f"HAB_{data['numero']}")
        
        cursor.execute("""
            INSERT INTO habitaciones (
                numero, nombre, tipo, piso, capacidad, 
                precio_noche, precio_dia, descripcion, observaciones, 
                codigo_producto, empresa_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data['numero'], data.get('nombre', ''), data.get('tipo', 'Estandar'),
            data.get('piso', 1), data.get('capacidad', 2),
            data.get('precio_noche', 0), data.get('precio_dia', 0),
            data.get('descripcion', ''), data.get('observaciones', ''),
            codigo_producto, empresa_id
        ))
        habitacion_id = cursor.lastrowid
        
        cursor.execute("SELECT codigo FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo_producto, empresa_id))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO productos (codigo, descripcion, categoria, precio_compra, precio_venta, existencia, iva, unidad_medida, tipo_producto, empresa_id)
                VALUES (%s, %s, 'habitacion', 0, %s, 1, 16, 'noche', 'normal', %s)
            """, (codigo_producto, f"Habitación {data['numero']} - {data.get('tipo', 'Estandar')}", data.get('precio_noche', 0), empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'habitacion', f"Habitación {data['numero']} creada con código {codigo_producto}")
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK', 'id': habitacion_id}), 201
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/habitaciones/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_habitacion(id):
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE habitaciones 
            SET numero=%s, nombre=%s, tipo=%s, piso=%s, capacidad=%s,
                precio_noche=%s, precio_dia=%s, descripcion=%s, observaciones=%s,
                estado=%s, codigo_producto=%s
            WHERE id=%s AND empresa_id=%s
        """, (
            data['numero'], data.get('nombre', ''), data.get('tipo', 'Estandar'),
            data.get('piso', 1), data.get('capacidad', 2),
            data.get('precio_noche', 0), data.get('precio_dia', 0),
            data.get('descripcion', ''), data.get('observaciones', ''),
            data.get('estado', 'disponible'), data.get('codigo_producto', f"HAB_{data['numero']}"),
            id, empresa_id
        ))
        conn.commit()
        invalidar_cache_productos(empresa_id)
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/habitaciones/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_habitacion(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM habitaciones WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        if not cursor.fetchone():
            return jsonify({'error': 'Habitación no encontrada'}), 404
        
        cursor.execute("DELETE FROM habitaciones WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        invalidar_cache_productos(empresa_id)
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/habitaciones/<int:id>/estado', methods=['PUT'])
@requiere_rol('admin')
def cambiar_estado_habitacion(id):
    data = request.json
    empresa_id = request.empresa_id
    nuevo_estado = data.get('estado')
    motivo = data.get('motivo', '')
    usuario_id = request.user_id
    
    estados_validos = ['disponible', 'ocupada', 'sucia', 'reservada', 'mantenimiento', 'danada']
    if nuevo_estado not in estados_validos:
        return jsonify({'error': 'Estado inválido'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT estado, codigo_producto, numero FROM habitaciones WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        habitacion = cursor.fetchone()
        if not habitacion:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Habitación no encontrada'}), 404
        
        estado_anterior = habitacion['estado']
        codigo_producto = habitacion.get('codigo_producto')
        
        if codigo_producto:
            if nuevo_estado == 'disponible':
                cursor.execute("""
                    UPDATE productos 
                    SET existencia = 1 
                    WHERE codigo = %s AND empresa_id = %s
                """, (codigo_producto, empresa_id))
                crear_alerta(empresa_id, 'habitacion_limpia', 
                           f"🧹 Habitación {habitacion['numero']} - DISPONIBLE (stock restaurado a 1)")
            elif nuevo_estado in ['sucia', 'ocupada', 'reservada', 'mantenimiento', 'danada']:
                cursor.execute("""
                    UPDATE productos 
                    SET existencia = 0 
                    WHERE codigo = %s AND empresa_id = %s
                """, (codigo_producto, empresa_id))
        
        cursor.execute("""
            UPDATE habitaciones 
            SET estado = %s, 
                observaciones = CONCAT(COALESCE(observaciones, ''), '\n', %s, ' - Estado cambiado de ', %s, ' a ', %s, ' por ', %s)
            WHERE id = %s AND empresa_id = %s
        """, (nuevo_estado, ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), estado_anterior, nuevo_estado, request.username, id, empresa_id))
        
        cursor.execute("""
            INSERT INTO historial_habitaciones (habitacion_id, estado_anterior, estado_nuevo, usuario_id, motivo, empresa_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (id, estado_anterior, nuevo_estado, usuario_id, motivo, empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'habitacion_estado', f"Habitación {habitacion['numero']} cambió de '{estado_anterior}' a '{nuevo_estado}'")
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/habitaciones/<int:id>/limpiar', methods=['POST'])
@requiere_rol('admin')
def limpiar_habitacion(id):
    """Cambia una habitación de SUCIA a DISPONIBLE y restaura stock"""
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("SELECT estado, codigo_producto, numero FROM habitaciones WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        habitacion = cursor.fetchone()
        
        if not habitacion:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Habitación no encontrada'}), 404
        
        if habitacion['estado'] != 'sucia':
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'La habitación no está en estado SUCIA'}), 400
        
        cursor.execute("""
            UPDATE habitaciones 
            SET estado = 'disponible',
                observaciones = CONCAT(COALESCE(observaciones, ''), '\n', %s, ' - Limpieza completada - Habitación disponible')
            WHERE id = %s AND empresa_id = %s
        """, (ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), id, empresa_id))
        
        if habitacion.get('codigo_producto'):
            cursor.execute("""
                UPDATE productos 
                SET existencia = 1 
                WHERE codigo = %s AND empresa_id = %s
            """, (habitacion['codigo_producto'], empresa_id))
            print(f"✅ Stock restaurado para {habitacion['codigo_producto']} → 1")
        
        cursor.execute("""
            DELETE FROM disponibilidad_habitaciones
            WHERE habitacion_id = %s AND empresa_id = %s
        """, (id, empresa_id))
        print(f"✅ Fechas liberadas para habitación {habitacion['numero']}")
        
        cursor.execute("""
            INSERT INTO historial_habitaciones (habitacion_id, estado_anterior, estado_nuevo, usuario_id, motivo, empresa_id)
            VALUES (%s, 'sucia', 'disponible', %s, 'Limpieza completada', %s)
        """, (id, request.user_id, empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'habitacion_limpia', f"🧹 Habitación {habitacion['numero']} - Limpieza completada - DISPONIBLE (stock=1)")
        
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK', 'mensaje': 'Habitación limpia y disponible'}), 200
        
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/habitaciones/<int:id>/actualizar-stock', methods=['POST'])
@requiere_rol('admin')
def actualizar_stock_habitacion(id):
    """Fuerza la actualización del stock de una habitación basado en su estado"""
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("SELECT estado, codigo_producto, numero FROM habitaciones WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        habitacion = cursor.fetchone()
        
        if not habitacion:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'Habitación no encontrada'}), 404
        
        codigo_producto = habitacion.get('codigo_producto')
        if not codigo_producto:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'La habitación no tiene código de producto'}), 400
        
        if habitacion['estado'] == 'disponible':
            nuevo_stock = 1
        else:
            nuevo_stock = 0
        
        cursor.execute("""
            UPDATE productos 
            SET existencia = %s 
            WHERE codigo = %s AND empresa_id = %s
        """, (nuevo_stock, codigo_producto, empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        
        safe_close_conn(conn, cursor)
        return jsonify({
            'status': 'OK',
            'habitacion': habitacion['numero'],
            'estado': habitacion['estado'],
            'stock_actualizado': nuevo_stock
        }), 200
        
    except Exception as e:
        conn.rollback()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

# ---------- RESERVAS ----------
@app.route('/api/reservas', methods=['GET'])
@requiere_rol('cajero')
def listar_reservas():
    empresa_id = request.empresa_id
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'reservas'
        """)
        if cursor.fetchone()['COUNT(*)'] == 0:
            safe_close_conn(conn, cursor)
            return jsonify([]), 200
        
        cursor.execute("""
            SELECT 
                r.id, 
                r.habitacion_id, 
                r.cliente_id, 
                r.usuario_id,
                r.fecha_reserva, 
                r.fecha_entrada, 
                r.fecha_salida,
                r.hora_entrada, 
                r.hora_salida,
                r.noches, 
                r.total_usd, 
                r.abono_usd, 
                r.saldo_pendiente,
                r.estado, 
                r.tipo, 
                r.observaciones, 
                r.notas_internas,
                r.fecha_creacion, 
                r.fecha_actualizacion,
                r.descuento, 
                r.motivo_descuento,
                h.numero as habitacion_numero, 
                h.tipo as habitacion_tipo,
                c.nombre as cliente_nombre, 
                c.rif as cliente_rif,
                u.username as usuario_creador
            FROM reservas r
            LEFT JOIN habitaciones h ON r.habitacion_id = h.id
            LEFT JOIN clientes c ON r.cliente_id = c.id
            LEFT JOIN usuarios u ON r.usuario_id = u.id
            WHERE r.empresa_id = %s
            ORDER BY r.fecha_entrada DESC
        """, (empresa_id,))
        
        reservas = cursor.fetchall()
        
        for r in reservas:
            for key, value in list(r.items()):
                if value is None:
                    continue
                elif isinstance(value, datetime):
                    r[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                elif isinstance(value, date):
                    r[key] = value.strftime('%Y-%m-%d')
                elif isinstance(value, Decimal):
                    r[key] = float(value)
                elif isinstance(value, timedelta):
                    r[key] = formatear_hora(value)
                elif hasattr(value, 'strftime') and hasattr(value, 'hour'):
                    try:
                        r[key] = value.strftime('%H:%M:%S')
                    except:
                        pass
        
        safe_close_conn(conn, cursor)
        return jsonify(reservas), 200
        
    except Exception as e:
        print(f"❌ ERROR en listar_reservas: {type(e).__name__}: {str(e)}")
        traceback.print_exc()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/reservas', methods=['POST'])
@requiere_rol('cajero')
def crear_reserva_con_pagos():
    data = request.json
    empresa_id = request.empresa_id
    usuario_id = request.user_id
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT COUNT(*) 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'reservas'
        """)
        if cursor.fetchone()['COUNT(*)'] == 0:
            safe_close_conn(conn, cursor)
            return jsonify({'error': 'La tabla reservas no existe. Ejecuta el script SQL de hotelería.'}), 500
        
        fecha_entrada = datetime.strptime(data['fecha_entrada'], '%Y-%m-%d').date()
        fecha_salida = datetime.strptime(data['fecha_salida'], '%Y-%m-%d').date()
        noches = (fecha_salida - fecha_entrada).days
        
        if noches <= 0:
            return jsonify({'error': 'La fecha de salida debe ser posterior a la entrada'}), 400
        
        cursor.execute("SELECT precio_noche FROM habitaciones WHERE id = %s AND empresa_id = %s", (data['habitacion_id'], empresa_id))
        habitacion = cursor.fetchone()
        if not habitacion:
            return jsonify({'error': 'Habitación no encontrada'}), 404
        
        bloqueos_en_rango = 0
        if tabla_existe(conn, 'disponibilidad_habitaciones'):
            cursor.execute("""
                SELECT COUNT(*) as total FROM disponibilidad_habitaciones
                WHERE habitacion_id = %s AND empresa_id = %s
                AND estado = 'no_disponible'
                AND fecha >= %s AND fecha < %s
            """, (data['habitacion_id'], empresa_id, fecha_entrada, fecha_salida))
            bloqueos_en_rango = cursor.fetchone()['total']
        
        cursor.execute("""
            SELECT COUNT(*) as total FROM reservas
            WHERE habitacion_id = %s AND empresa_id = %s
            AND estado IN ('pendiente', 'confirmada', 'abonada', 'check_in')
            AND fecha_entrada < %s AND fecha_salida > %s
        """, (data['habitacion_id'], empresa_id, fecha_salida, fecha_entrada))
        reservas_en_rango = cursor.fetchone()['total']
        
        if bloqueos_en_rango > 0 or reservas_en_rango > 0:
            safe_close_conn(conn, cursor)
            return jsonify({
                'error': f"La habitación no está disponible entre {data['fecha_entrada']} y {data['fecha_salida']} "
                         f"({bloqueos_en_rango} bloqueo(s), {reservas_en_rango} reserva(s) existente(s))."
            }), 400
        
        precio_noche = float(habitacion['precio_noche'] or 0)
        total_usd = precio_noche * noches
        
        descuento = float(data.get('descuento', 0))
        motivo_descuento = data.get('motivo_descuento', '')
        total_usd = max(0, total_usd - descuento)
        
        cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
        tasa_row = cursor.fetchone()
        tasa = float(tasa_row['tasa_cambio']) if tasa_row else 544.58
        
        pagos = data.get('pagos', [])
        abono_total = 0
        for p in pagos:
            monto = float(p.get('monto', 0))
            moneda = p.get('moneda', 'USD')
            if moneda == 'USD':
                abono_total += monto
            else:
                abono_total += monto / tasa
        
        saldo_pendiente = total_usd - abono_total
        
        if saldo_pendiente <= 0.01 and total_usd > 0:
            estado = 'confirmada'
        elif abono_total > 0:
            estado = 'abonada'
        else:
            estado = 'pendiente'
        
        cursor.execute("""
            INSERT INTO reservas (
                habitacion_id, cliente_id, fecha_entrada, fecha_salida,
                hora_entrada, hora_salida, noches, total_usd,
                abono_usd, saldo_pendiente, observaciones, notas_internas,
                estado, tipo, empresa_id, usuario_id, descuento, motivo_descuento
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data['habitacion_id'], data['cliente_id'], fecha_entrada, fecha_salida,
            data.get('hora_entrada', '15:00:00'), data.get('hora_salida', '12:00:00'),
            noches, total_usd, abono_total, saldo_pendiente,
            data.get('observaciones', ''), data.get('notas_internas', ''),
            estado, data.get('tipo', 'reserva'), empresa_id, usuario_id,
            descuento, motivo_descuento
        ))
        reserva_id = cursor.lastrowid
        
        for p in pagos:
            monto = float(p.get('monto', 0))
            moneda = p.get('moneda', 'USD')
            
            if monto > 0:
                if moneda == 'USD':
                    monto_usd = monto
                    monto_bs = monto * tasa
                else:
                    monto_bs = monto
                    monto_usd = monto / tasa
                
                cursor.execute("""
                    INSERT INTO reservas_pagos (reserva_id, metodo_pago, monto_usd, monto_bs, moneda, referencia, usuario_id, empresa_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (reserva_id, p.get('metodo_pago', 'Efectivo'), monto_usd, monto_bs, moneda, p.get('referencia', ''), usuario_id, empresa_id))
        
        for servicio in data.get('servicios', []):
            cursor.execute("""
                INSERT INTO reservas_servicios (reserva_id, servicio_id, cantidad, precio_unitario, total, fecha_servicio, observaciones, empresa_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                reserva_id, servicio['servicio_id'], servicio.get('cantidad', 1),
                servicio.get('precio_unitario', 0),
                servicio.get('cantidad', 1) * servicio.get('precio_unitario', 0),
                servicio.get('fecha_servicio'), servicio.get('observaciones', ''), empresa_id
            ))
        
        hoy = date.today()
        if estado in ['confirmada', 'abonada'] and fecha_entrada <= hoy < fecha_salida:
            cursor.execute("""
                UPDATE habitaciones 
                SET estado = 'reservada' 
                WHERE id = %s AND empresa_id = %s AND estado = 'disponible'
            """, (data['habitacion_id'], empresa_id))
        
        cursor.execute("""
            INSERT INTO disponibilidad_habitaciones (habitacion_id, fecha, estado, motivo, reserva_id, empresa_id)
            SELECT %s, fecha_generada, 'no_disponible', 'Reserva confirmada', %s, %s
            FROM (
                SELECT DATE_ADD(%s, INTERVAL seq.seq DAY) as fecha_generada
                FROM (
                    SELECT a.i + b.i * 10 + c.i * 100 as seq
                    FROM (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a
                    CROSS JOIN (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b
                    CROSS JOIN (SELECT 0 as i UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) c
                ) seq
                WHERE DATE_ADD(%s, INTERVAL seq.seq DAY) < %s
            ) fechas
            ON DUPLICATE KEY UPDATE
            estado = 'no_disponible', reserva_id = %s
        """, (
            data['habitacion_id'],
            reserva_id,
            empresa_id,
            fecha_entrada,
            fecha_entrada,
            fecha_salida,
            reserva_id
        ))
        
        conn.commit()
        crear_alerta(empresa_id, 'nueva_reserva', f"Nueva reserva #{reserva_id} creada por {request.username}")
        safe_close_conn(conn, cursor)
        return jsonify({'status': 'OK', 'id': reserva_id, 'saldo_pendiente': saldo_pendiente}), 201
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/reservas/<int:id>/abono', methods=['POST'])
@requiere_rol('cajero')
def registrar_abono_reserva(id):
    data = request.json
    empresa_id = request.empresa_id
    usuario_id = request.user_id
    monto = float(data.get('monto', 0))
    metodo_pago = data.get('metodo_pago', 'Efectivo')
    referencia = data.get('referencia', '')
    moneda = data.get('moneda', 'USD')
    
    if monto <= 0:
        return jsonify({'error': 'El monto debe ser mayor a 0'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM reservas WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        reserva = cursor.fetchone()
        if not reserva:
            return jsonify({'error': 'Reserva no encontrada'}), 404
        
        if reserva['estado'] in ['cancelada', 'facturada']:
            return jsonify({'error': f'No se pueden registrar abonos en reservas {reserva["estado"]}'}), 400
        
        cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
        tasa_row = cursor.fetchone()
        tasa = float(tasa_row['tasa_cambio']) if tasa_row else 544.58
        
        if moneda == 'USD':
            monto_usd = monto
            monto_bs = monto * tasa
        else:
            monto_bs = monto
            monto_usd = monto / tasa
        
        nuevo_abono = float(reserva['abono_usd'] or 0) + monto_usd
        nuevo_saldo = float(reserva['total_usd'] or 0) - nuevo_abono
        
        if nuevo_saldo <= 0.01:
            nuevo_estado = 'confirmada'
        elif nuevo_abono > 0:
            nuevo_estado = 'abonada'
        else:
            nuevo_estado = reserva['estado']
        
        cursor.execute("""
            UPDATE reservas 
            SET abono_usd = %s, saldo_pendiente = %s, estado = %s,
                notas_internas = CONCAT(COALESCE(notas_internas, ''), '\n', %s, ' - Abono de $', %s, ' (', %s, ') registrado')
            WHERE id = %s AND empresa_id = %s
        """, (nuevo_abono, nuevo_saldo, nuevo_estado, ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), monto_usd, metodo_pago, id, empresa_id))
        
        cursor.execute("""
            INSERT INTO reservas_pagos (reserva_id, metodo_pago, monto_usd, monto_bs, moneda, referencia, usuario_id, empresa_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (id, metodo_pago, monto_usd, monto_bs, moneda, referencia, usuario_id, empresa_id))
        
        conn.commit()
        crear_alerta(empresa_id, 'abono', f"Abono de ${monto_usd} registrado para reserva #{id} - Saldo: ${nuevo_saldo}")
        return jsonify({'status': 'OK', 'abono': nuevo_abono, 'saldo': nuevo_saldo}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/reservas/<int:id>/facturar', methods=['GET'])
@requiere_rol('cajero')
def obtener_reserva_para_facturar(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT r.*, 
                   h.numero as habitacion_numero, h.tipo as habitacion_tipo,
                   c.nombre as cliente_nombre, c.rif as cliente_rif,
                   c.id as cliente_id
            FROM reservas r
            LEFT JOIN habitaciones h ON r.habitacion_id = h.id
            LEFT JOIN clientes c ON r.cliente_id = c.id
            WHERE r.id = %s AND r.empresa_id = %s
        """, (id, empresa_id))
        reserva = cursor.fetchone()
        if not reserva:
            return jsonify({'error': 'Reserva no encontrada'}), 404
        
        if float(reserva.get('saldo_pendiente', 0)) > 0.01:
            return jsonify({'error': f'La reserva tiene saldo pendiente de ${reserva["saldo_pendiente"]}. Debe estar completamente pagada.'}), 400
        
        if reserva['estado'] in ['cancelada', 'facturada']:
            return jsonify({'error': f'La reserva está {reserva["estado"]}'}), 400
        
        cursor.execute("""
            SELECT rs.*, s.nombre as servicio_nombre
            FROM reservas_servicios rs
            JOIN servicios_adicionales s ON rs.servicio_id = s.id
            WHERE rs.reserva_id = %s
        """, (id,))
        servicios = cursor.fetchall()
        reserva['servicios'] = servicios
        
        cursor.execute("""
            SELECT metodo_pago, monto_usd, monto_bs, moneda, referencia, fecha
            FROM reservas_pagos
            WHERE reserva_id = %s
            ORDER BY fecha
        """, (id,))
        pagos = cursor.fetchall()
        for p in pagos:
            p['monto_usd'] = float(p.get('monto_usd') or 0)
            p['monto_bs'] = float(p.get('monto_bs') or 0)
        reserva['pagos'] = pagos
        
        safe_close_conn(conn, cursor)
        return jsonify(reserva), 200
    except Exception as e:
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

@app.route('/api/reservas/<int:id>/check-in', methods=['POST'])
@requiere_rol('cajero')
def check_in_reserva(id):
    data = request.json
    empresa_id = request.empresa_id
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT r.*, h.estado as habitacion_estado, h.codigo_producto
            FROM reservas r
            JOIN habitaciones h ON r.habitacion_id = h.id
            WHERE r.id = %s AND r.empresa_id = %s
        """, (id, empresa_id))
        reserva = cursor.fetchone()
        if not reserva:
            return jsonify({'error': 'Reserva no encontrada'}), 404
        
        if reserva['estado'] == 'check_in':
            return jsonify({'error': 'Ya se hizo check-in'}), 400
        
        if reserva['estado'] == 'check_out':
            return jsonify({'error': 'La reserva ya fue finalizada'}), 400
        
        cursor.execute("""
            UPDATE reservas 
            SET estado = 'check_in', hora_entrada = %s,
                notas_internas = CONCAT(COALESCE(notas_internas, ''), '\n', %s, ' - Check-in realizado')
            WHERE id = %s AND empresa_id = %s
        """, (data.get('hora_entrada', ahora_venezuela().strftime('%H:%M:%S')), ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), id, empresa_id))
        
        cursor.execute("""
            UPDATE habitaciones 
            SET estado = 'ocupada' 
            WHERE id = %s AND empresa_id = %s
        """, (reserva['habitacion_id'], empresa_id))
        
        if reserva.get('codigo_producto'):
            cursor.execute("""
                UPDATE productos 
                SET existencia = 0 
                WHERE codigo = %s AND empresa_id = %s
            """, (reserva['codigo_producto'], empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'check_in', f"Check-in realizado para reserva #{id}")
        return jsonify({'status': 'OK', 'mensaje': 'Check-in realizado correctamente'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/reservas/<int:id>/check-out', methods=['POST'])
@requiere_rol('cajero')
def check_out_reserva(id):
    data = request.json
    empresa_id = request.empresa_id
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT r.*, h.estado as habitacion_estado, h.codigo_producto
            FROM reservas r
            JOIN habitaciones h ON r.habitacion_id = h.id
            WHERE r.id = %s AND r.empresa_id = %s
        """, (id, empresa_id))
        reserva = cursor.fetchone()
        if not reserva:
            return jsonify({'error': 'Reserva no encontrada'}), 404
        
        if reserva['estado'] == 'check_out':
            return jsonify({'error': 'Ya se hizo check-out'}), 400
        
        if reserva['estado'] not in ['check_in', 'confirmada']:
            return jsonify({'error': 'La reserva no está activa'}), 400
        
        if reserva.get('codigo_producto'):
            cursor.execute("""
                UPDATE productos 
                SET existencia = 1 
                WHERE codigo = %s AND empresa_id = %s
            """, (reserva['codigo_producto'], empresa_id))
        
        cursor.execute("""
            UPDATE reservas 
            SET estado = 'check_out', hora_salida = %s,
                notas_internas = CONCAT(COALESCE(notas_internas, ''), '\n', %s, ' - Check-out realizado')
            WHERE id = %s AND empresa_id = %s
        """, (data.get('hora_salida', ahora_venezuela().strftime('%H:%M:%S')), ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), id, empresa_id))
        
        cursor.execute("""
            UPDATE habitaciones 
            SET estado = 'sucia',
                observaciones = CONCAT(COALESCE(observaciones, ''), '\n', %s, ' - Check-out automático')
            WHERE id = %s AND empresa_id = %s
        """, (ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), reserva['habitacion_id'], empresa_id))
        
        cursor.execute("""
            INSERT INTO historial_habitaciones (habitacion_id, estado_anterior, estado_nuevo, usuario_id, motivo, empresa_id)
            VALUES (%s, 'ocupada', 'sucia', %s, 'Check-out automático - Habitación sucia', %s)
        """, (reserva['habitacion_id'], request.user_id, empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'check_out', f"Check-out realizado para reserva #{id}")
        return jsonify({'status': 'OK', 'mensaje': 'Check-out realizado correctamente'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/reservas/<int:id>/estado', methods=['PUT'])
@requiere_rol('admin')
def cambiar_estado_reserva(id):
    data = request.json
    empresa_id = request.empresa_id
    nuevo_estado = data.get('estado')
    motivo = data.get('motivo', '')
    
    estados_validos = ['pendiente', 'confirmada', 'abonada', 'check_in', 'check_out', 'cancelada', 'no_show', 'facturada']
    if nuevo_estado not in estados_validos:
        return jsonify({'error': 'Estado inválido'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM reservas WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        reserva = cursor.fetchone()
        if not reserva:
            return jsonify({'error': 'Reserva no encontrada'}), 404
        
        if nuevo_estado == 'cancelada' and reserva['estado'] != 'cancelada':
            cursor.execute("""
                UPDATE habitaciones 
                SET estado = 'disponible' 
                WHERE id = %s AND empresa_id = %s
            """, (reserva['habitacion_id'], empresa_id))
            
            if reserva.get('codigo_producto'):
                cursor.execute("""
                    UPDATE productos 
                    SET existencia = 1 
                    WHERE codigo = %s AND empresa_id = %s
                """, (reserva['codigo_producto'], empresa_id))
            
            cursor.execute("""
                DELETE FROM disponibilidad_habitaciones
                WHERE reserva_id = %s AND empresa_id = %s
            """, (id, empresa_id))
        
        cursor.execute("""
            UPDATE reservas 
            SET estado = %s,
                notas_internas = CONCAT(COALESCE(notas_internas, ''), '\n', %s, ' - Estado cambiado a ', %s, ' - Motivo: ', %s)
            WHERE id = %s AND empresa_id = %s
        """, (nuevo_estado, ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), nuevo_estado, motivo, id, empresa_id))
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        crear_alerta(empresa_id, 'reserva_estado', f"Reserva #{id} cambió a '{nuevo_estado}'")
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ---------- SERVICIOS ADICIONALES ----------
@app.route('/api/servicios', methods=['GET'])
@requiere_rol('cajero')
def listar_servicios():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT * FROM servicios_adicionales 
        WHERE empresa_id = %s AND activo = 1
        ORDER BY nombre
    """, (empresa_id,))
    servicios = cursor.fetchall()
    safe_close_conn(conn, cursor)
    return jsonify(servicios), 200

@app.route('/api/servicios', methods=['POST'])
@requiere_rol('admin')
def crear_servicio():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO servicios_adicionales (nombre, descripcion, precio_usd, tipo, empresa_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (data['nombre'], data.get('descripcion', ''), data.get('precio_usd', 0), data.get('tipo', 'servicio'), empresa_id))
        conn.commit()
        return jsonify({'status': 'OK', 'id': cursor.lastrowid}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/servicios/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_servicio(id):
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE servicios_adicionales 
            SET nombre=%s, descripcion=%s, precio_usd=%s, tipo=%s, activo=%s
            WHERE id=%s AND empresa_id=%s
        """, (
            data['nombre'], data.get('descripcion', ''),
            data.get('precio_usd', 0), data.get('tipo', 'servicio'),
            data.get('activo', True), id, empresa_id
        ))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/servicios/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_servicio(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM servicios_adicionales WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        if not cursor.fetchone():
            return jsonify({'error': 'Servicio no encontrado'}), 404
        
        cursor.execute("DELETE FROM servicios_adicionales WHERE id = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ---------- REPORTE DE OCUPACIÓN ----------
@app.route('/api/reporte-ocupacion', methods=['GET'])
@requiere_rol('cajero')
def reporte_ocupacion():
    empresa_id = request.empresa_id
    fecha = request.args.get('fecha', ahora_venezuela().strftime('%Y-%m-%d'))
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT 
                estado,
                COUNT(*) as total
            FROM habitaciones
            WHERE empresa_id = %s AND activa = 1
            GROUP BY estado
        """, (empresa_id,))
        estados = {row['estado']: row['total'] for row in cursor.fetchall()}
        
        cursor.execute("""
            SELECT 
                COUNT(*) as ocupadas,
                (SELECT COUNT(*) FROM habitaciones WHERE empresa_id = %s AND activa = 1) as total_habitaciones
            FROM habitaciones
            WHERE empresa_id = %s AND estado = 'ocupada'
        """, (empresa_id, empresa_id))
        ocupacion = cursor.fetchone()
        
        cursor.execute("""
            SELECT 
                COUNT(*) as total_reservas,
                SUM(total_usd) as ingresos_totales
            FROM reservas
            WHERE empresa_id = %s 
            AND (fecha_entrada <= %s AND fecha_salida >= %s)
            AND estado IN ('confirmada', 'abonada', 'check_in')
        """, (empresa_id, fecha, fecha))
        reservas_hoy = cursor.fetchone()
        
        fecha_fin = (datetime.strptime(fecha, '%Y-%m-%d') + timedelta(days=7)).strftime('%Y-%m-%d')
        cursor.execute("""
            SELECT 
                fecha_entrada,
                COUNT(*) as reservas
            FROM reservas
            WHERE empresa_id = %s 
            AND fecha_entrada BETWEEN %s AND %s
            AND estado IN ('pendiente', 'confirmada', 'abonada')
            GROUP BY fecha_entrada
            ORDER BY fecha_entrada
        """, (empresa_id, fecha, fecha_fin))
        proximas_reservas = cursor.fetchall()
        
        safe_close_conn(conn, cursor)
        
        return jsonify({
            'estados': estados,
            'ocupacion': {
                'ocupadas': ocupacion['ocupadas'] if ocupacion else 0,
                'total': ocupacion['total_habitaciones'] if ocupacion else 0,
                'porcentaje': round((ocupacion['ocupadas'] / ocupacion['total_habitaciones']) * 100, 2) if ocupacion and ocupacion['total_habitaciones'] > 0 else 0
            },
            'reservas_hoy': {
                'total': reservas_hoy['total_reservas'] if reservas_hoy else 0,
                'ingresos': reservas_hoy['ingresos_totales'] if reservas_hoy else 0
            },
            'proximas_reservas': proximas_reservas
        }), 200
    except Exception as e:
        safe_close_conn(conn, cursor)
        return jsonify({'error': str(e)}), 500

# ========== VERIFICAR HABITACIONES VENCIDAS MANUAL ==========
@app.route('/api/verificar-vencidas', methods=['POST'])
@requiere_rol('admin')
def verificar_habitaciones_vencidas_manual():
    """Endpoint manual para verificar habitaciones vencidas"""
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        ahora_str = ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute("""
            SELECT id, numero, codigo_producto, fecha_salida_ultima, hora_salida_ultima
            FROM habitaciones 
            WHERE estado = 'ocupada' 
            AND fecha_salida_ultima IS NOT NULL
            AND TIMESTAMP(fecha_salida_ultima, COALESCE(hora_salida_ultima, '12:00:00')) <= %s
            AND empresa_id = %s
        """, (ahora_str, empresa_id))
        
        habitaciones_vencidas = cursor.fetchall()
        
        resultados = []
        for hab in habitaciones_vencidas:
            cursor.execute("""
                UPDATE habitaciones 
                SET estado = 'sucia',
                    observaciones = CONCAT(COALESCE(observaciones, ''), '\n', %s, ' - Check-out automático por vencimiento de estadía')
                WHERE id = %s AND empresa_id = %s
            """, (ahora_venezuela().strftime('%Y-%m-%d %H:%M:%S'), hab['id'], empresa_id))
            
            if hab.get('codigo_producto'):
                cursor.execute("""
                    UPDATE productos 
                    SET existencia = 0 
                    WHERE codigo = %s AND empresa_id = %s
                """, (hab['codigo_producto'], empresa_id))
                print(f"✅ Stock de {hab['codigo_producto']} → 0 (habitación sucia)")
            
            cursor.execute("""
                DELETE FROM disponibilidad_habitaciones
                WHERE habitacion_id = %s AND empresa_id = %s
            """, (hab['id'], empresa_id))
            
            cursor.execute("""
                INSERT INTO historial_habitaciones (habitacion_id, estado_anterior, estado_nuevo, usuario_id, motivo, empresa_id)
                VALUES (%s, 'ocupada', 'sucia', %s, 'Check-out automático por vencimiento', %s)
            """, (hab['id'], request.user_id, empresa_id))
            
            crear_alerta(empresa_id, 'habitacion_vencida', 
                       f"🔄 Habitación {hab['numero']} - Check-out automático por vencimiento - SUCIA (stock=0)")
            
            resultados.append({
                'id': hab['id'],
                'numero': hab['numero'],
                'estado_anterior': 'ocupada',
                'estado_nuevo': 'sucia',
                'fecha_salida': hab['fecha_salida_ultima']
            })
        
        conn.commit()
        invalidar_cache_productos(empresa_id)
        
        return jsonify({
            'status': 'OK',
            'mensaje': f'Se verificaron {len(resultados)} habitaciones vencidas',
            'habitaciones': resultados
        }), 200
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error en verificación manual: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== SUPER ADMIN ==========
@app.route('/api/super/empresas', methods=['GET'])
@requiere_super_admin
def super_listar_empresas():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    tiene_columna = columna_existe(conn, 'empresas', 'permite_reiniciar_historial')
    
    if tiene_columna:
        cursor.execute("""
            SELECT e.id, e.nombre, e.rif, e.correo, e.telefono, e.direccion, e.tasa_cambio, e.activa, e.ultimo_reporte_z, e.permite_reiniciar_historial,
                   MAX(CASE WHEN u.role = 'admin' THEN u.username END) as admin_username,
                   MAX(CASE WHEN u.role = 'admin' THEN u.email END) as admin_email
            FROM empresas e
            LEFT JOIN usuarios u ON u.empresa_id = e.id
            GROUP BY e.id
            ORDER BY e.id
        """)
    else:
        cursor.execute("""
            SELECT e.id, e.nombre, e.rif, e.correo, e.telefono, e.direccion, e.tasa_cambio, e.activa, e.ultimo_reporte_z,
                   MAX(CASE WHEN u.role = 'admin' THEN u.username END) as admin_username,
                   MAX(CASE WHEN u.role = 'admin' THEN u.email END) as admin_email
            FROM empresas e
            LEFT JOIN usuarios u ON u.empresa_id = e.id
            GROUP BY e.id
            ORDER BY e.id
        """)
    
    empresas = cursor.fetchall()
    if not tiene_columna:
        for emp in empresas:
            emp['permite_reiniciar_historial'] = False
    
    safe_close_conn(conn, cursor)
    return jsonify(empresas), 200

@app.route('/api/super/empresas/<int:id>', methods=['PUT'])
@requiere_super_admin
def super_editar_empresa(id):
    data = request.json
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        tiene_columna = columna_existe(conn, 'empresas', 'permite_reiniciar_historial')
        
        if tiene_columna:
            cursor.execute("""
                UPDATE empresas 
                SET nombre=%s, rif=%s, correo=%s, telefono=%s, direccion=%s, tasa_cambio=%s, permite_reiniciar_historial=%s
                WHERE id=%s
            """, (data.get('nombre'), data.get('rif'), data.get('correo'), data.get('telefono'),
                  data.get('direccion'), data.get('tasa_cambio'), data.get('permite_reiniciar_historial', False), id))
        else:
            cursor.execute("""
                UPDATE empresas 
                SET nombre=%s, rif=%s, correo=%s, telefono=%s, direccion=%s, tasa_cambio=%s
                WHERE id=%s
            """, (data.get('nombre'), data.get('rif'), data.get('correo'), data.get('telefono'),
                  data.get('direccion'), data.get('tasa_cambio'), id))
            
            cursor.execute("ALTER TABLE empresas ADD COLUMN permite_reiniciar_historial BOOLEAN DEFAULT FALSE")
        
        if 'admin_username' in data and data['admin_username']:
            cursor.execute("UPDATE usuarios SET username = %s WHERE empresa_id = %s AND role = 'admin'", (data['admin_username'], id))
        if 'admin_email' in data and data['admin_email']:
            cursor.execute("UPDATE usuarios SET email = %s WHERE empresa_id = %s AND role = 'admin'", (data['admin_email'], id))
        if 'admin_password' in data and data['admin_password']:
            hashed = bcrypt.hashpw(data['admin_password'].encode('utf-8'), bcrypt.gensalt())
            cursor.execute("UPDATE usuarios SET password_hash = %s WHERE empresa_id = %s AND role = 'admin'", (hashed, id))
        
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/super/empresas/<int:id>/toggle-status', methods=['POST'])
@requiere_super_admin
def super_toggle_empresa_status(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE empresas SET activa = NOT activa WHERE id = %s", (id,))
    conn.commit()
    safe_close_conn(conn, cursor)
    return jsonify({'status': 'OK'}), 200

@app.route('/api/super/empresas/<int:id>/reset', methods=['POST'])
@requiere_super_admin
def super_resetear_empresa(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (id,))
        tasa = cursor.fetchone()[0] if cursor.rowcount > 0 else 544.58
        
        cursor.execute("DELETE fp FROM facturas_pagos fp JOIN facturas_cabecera fc ON fp.factura_numero = fc.numero WHERE fc.empresa_id = %s", (id,))
        cursor.execute("DELETE fd FROM facturas_detalle fd JOIN facturas_cabecera fc ON fd.factura_numero = fc.numero WHERE fc.empresa_id = %s", (id,))
        cursor.execute("DELETE FROM facturas_cabecera WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM historial_cierres WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM caja_sesion WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM historial_inventario WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM productos WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM clientes WHERE empresa_id = %s", (id,))
        cursor.execute("INSERT INTO clientes (rif, nombre, empresa_id) VALUES ('J-00000000-0', 'Cliente General', %s)", (id,))
        cursor.execute("UPDATE empresas SET ultimo_reporte_z = 0, tasa_cambio = %s WHERE id = %s", (tasa, id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

@app.route('/api/super/empresas/<int:id>', methods=['DELETE'])
@requiere_super_admin
def super_eliminar_empresa(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE fp FROM facturas_pagos fp JOIN facturas_cabecera fc ON fp.factura_numero = fc.numero WHERE fc.empresa_id = %s", (id,))
        cursor.execute("DELETE fd FROM facturas_detalle fd JOIN facturas_cabecera fc ON fd.factura_numero = fc.numero WHERE fc.empresa_id = %s", (id,))
        cursor.execute("DELETE FROM facturas_cabecera WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM historial_cierres WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM caja_sesion WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM historial_inventario WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM productos WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM clientes WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM usuarios WHERE empresa_id = %s", (id,))
        cursor.execute("DELETE FROM empresas WHERE id = %s", (id,))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== SUPER ADMIN: LISTAR CIERRES DE UNA EMPRESA ==========
@app.route('/api/super/cierres/empresa/<int:empresa_id>', methods=['GET'])
@requiere_super_admin
def super_listar_cierres_empresa(empresa_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT h.id, h.fecha_cierre, h.usuario_id, u.username,
               h.total_usd, h.total_bs, h.datos, h.numero_reporte_empresa
        FROM historial_cierres h
        JOIN usuarios u ON h.usuario_id = u.id
        WHERE h.empresa_id = %s
        ORDER BY h.fecha_cierre DESC
    """, (empresa_id,))
    registros = cursor.fetchall()
    for r in registros:
        if r['fecha_cierre'] and hasattr(r['fecha_cierre'], 'strftime'):
            r['fecha_cierre'] = r['fecha_cierre'].strftime('%Y-%m-%d %H:%M:%S')
        r['datos'] = json.loads(r['datos'])
        r['total_usd'] = float(r['total_usd'] or 0)
        r['total_bs'] = float(r['total_bs'] or 0)
    safe_close_conn(conn, cursor)
    return jsonify(registros), 200

# ========== SUPER ADMIN: ELIMINAR CIERRE ==========
@app.route('/api/super/cierres/<int:id>', methods=['DELETE'])
@requiere_super_admin
def super_eliminar_cierre(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM historial_cierres WHERE id = %s", (id,))
        conn.commit()
        return jsonify({'status': 'OK', 'mensaje': 'Reporte Z eliminado correctamente'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        safe_close_conn(conn, cursor)

# ========== RUTAS ESTÁTICAS ==========
@app.route('/')
@app.route('/<path:filename>')
def serve_frontend(filename='login.html'):
    if filename.startswith('api/') or filename.startswith('socket.io/'):
        return jsonify({'error': 'Not found'}), 404
    for carpeta in ('.', 'frontend', '../frontend'):
        try:
            return send_from_directory(carpeta, filename)
        except (FileNotFoundError, NotFound, NotADirectoryError):
            continue
    return jsonify({'error': 'Not found'}), 404

@app.route('/static/<path:filename>')
def serve_static(filename):
    for carpeta in ('static', 'frontend/static', '../frontend/static'):
        try:
            return send_from_directory(carpeta, filename)
        except (FileNotFoundError, NotFound, NotADirectoryError):
            continue
    return jsonify({'error': 'Archivo no encontrado'}), 404

# ========== MANEJADORES DE ERRORES ==========
@app.errorhandler(404)
def not_found(error):
    if request.path.startswith('/api'):
        return jsonify({'error': 'Endpoint no encontrado'}), 404
    return "Página no encontrada", 404

@app.errorhandler(500)
def internal_error(error):
    if request.path.startswith('/api'):
        return jsonify({'error': 'Error interno del servidor'}), 500
    return "Error interno del servidor", 500

# ========== INICIO DE HILOS EN SEGUNDO PLANO ==========
def iniciar_verificador():
    thread = threading.Thread(target=verificar_habitaciones_vencidas, daemon=True)
    thread.start()
    print("✅ Verificador de habitaciones vencidas iniciado (cada 5 minutos)")

def iniciar_actualizador_tasa():
    thread = threading.Thread(target=actualizar_tasas_automaticas, daemon=True)
    thread.start()
    print("✅ Actualizador automático de tasa BCV iniciado (cada hora)")

iniciar_verificador()
iniciar_actualizador_tasa()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
