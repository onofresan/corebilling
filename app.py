import os
import mysql.connector
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime, timedelta
import json
import bcrypt
from functools import wraps
import csv
import io
import traceback
import random
from decimal import Decimal
from werkzeug.utils import secure_filename
import jwt
from flask_socketio import SocketIO, emit
import stripe
import requests  # Para consumir API externa

# ========== CONFIGURACIÓN ==========
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'clave_jwt_super_secreta_cambiar_en_produccion')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=8)

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_...')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY', 'pk_test_...')

socketio = SocketIO(app, cors_allowed_origins="*")

UPLOAD_FOLDER = 'static/logos'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CORS(app, origins="*", supports_credentials=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ========== FUNCIONES AUXILIARES ==========
def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', 'Koko.2590'),
        database=os.environ.get('DB_NAME', 'facturacion')
    )

def obtener_tasa_bcv():
    """
    Obtiene la tasa de cambio oficial del BCV desde exchangerate.host
    Retorna un float o None si falla.
    """
    try:
        response = requests.get('https://api.exchangerate.host/latest?base=USD&symbols=VES', timeout=5)
        if response.status_code == 200:
            data = response.json()
            tasa = data.get('rates', {}).get('VES')
            if tasa:
                return float(tasa)
    except Exception as e:
        print(f"Error al obtener tasa BCV: {e}")
    return None

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
        except Exception as e:
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

def registrar_historial_inventario(cursor, codigo, descripcion, tipo, cantidad_anterior, cantidad_nueva, nota=''):
    usuario = request.username
    empresa_id = request.empresa_id
    cursor.execute("""
        INSERT INTO historial_inventario (usuario, producto_codigo, producto_descripcion, tipo, cantidad_anterior, cantidad_nueva, nota, empresa_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (usuario, codigo, descripcion, tipo, cantidad_anterior, cantidad_nueva, nota, empresa_id))

def crear_alerta(empresa_id, tipo, mensaje, usuario_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO alertas (empresa_id, tipo, mensaje, usuario_id, fecha, leida)
        VALUES (%s, %s, %s, %s, NOW(), 0)
    """, (empresa_id, tipo, mensaje, usuario_id))
    conn.commit()
    cursor.close()
    conn.close()
    socketio.emit('nueva_alerta', {'tipo': tipo, 'mensaje': mensaje}, room=str(empresa_id))

# ========== AUTENTICACIÓN JWT ==========
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('usuario')
    password = data.get('contrasena')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
    user = cursor.fetchone()
    _ = cursor.fetchall()
    cursor.close()
    conn.close()
    if not user or not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
        return jsonify({'error': 'Credenciales inválidas'}), 401
    if user['role'] != 'super_admin':
        conn2 = get_db_connection()
        cursor2 = conn2.cursor(dictionary=True)
        cursor2.execute("SELECT activa FROM empresas WHERE id = %s", (user['empresa_id'],))
        empresa = cursor2.fetchone()
        _ = cursor2.fetchall()
        cursor2.close()
        conn2.close()
        if not empresa or not empresa.get('activa', True):
            return jsonify({'error': 'Empresa desactivada'}), 403
    token = jwt.encode({
        'user_id': user['id'],
        'role': user['role'],
        'username': user['username'],
        'empresa_id': user.get('empresa_id')
    }, app.config['SECRET_KEY'], algorithm='HS256')
    return jsonify({'token': token, 'role': user['role'], 'username': user['username'], 'empresa_id': user.get('empresa_id')}), 200

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

# ========== WEBSOCKETS ==========
@socketio.on('join')
def handle_join(data):
    empresa_id = data.get('empresa_id')
    if empresa_id:
        from flask_socketio import join_room
        join_room(str(empresa_id))

# ========== TASA DE CAMBIO AUTOMÁTICA (BCV) ==========
@app.route('/api/tasa-bcv', methods=['GET'])
def obtener_tasa_bcv_endpoint():
    """
    Endpoint para obtener la tasa oficial del BCV desde la API externa.
    Guarda la tasa en la empresa (si se detecta empresa_id) y la devuelve.
    """
    tasa = obtener_tasa_bcv()
    if tasa is None:
        return jsonify({'error': 'No se pudo obtener la tasa del BCV'}), 503
    # Si la solicitud viene de un usuario autenticado, actualizar la empresa
    token = request.headers.get('Authorization')
    empresa_id = None
    if token:
        try:
            token_data = token.split(' ')[1]
            decoded = jwt.decode(token_data, app.config['SECRET_KEY'], algorithms=['HS256'])
            empresa_id = decoded.get('empresa_id')
            if empresa_id:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("UPDATE empresas SET tasa_cambio = %s WHERE id = %s", (tasa, empresa_id))
                conn.commit()
                cursor.close()
                conn.close()
        except:
            pass
    return jsonify({'tasa': tasa}), 200

@app.route('/api/tasa', methods=['GET'])
@requiere_rol('cajero')
def obtener_tasa():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
    tasa = cursor.fetchone()
    cursor.close()
    conn.close()
    if tasa and tasa['tasa_cambio']:
        return jsonify({'tasa': float(tasa['tasa_cambio'])}), 200
    # Si no hay tasa guardada, intentar obtener de BCV
    tasa_bcv = obtener_tasa_bcv()
    if tasa_bcv:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE empresas SET tasa_cambio = %s WHERE id = %s", (tasa_bcv, empresa_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'tasa': tasa_bcv}), 200
    return jsonify({'tasa': 544.58}), 200

@app.route('/api/tasa', methods=['POST'])
@requiere_rol('admin')
def guardar_tasa():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE empresas SET tasa_cambio = %s WHERE id = %s", (data['tasa'], empresa_id))
    conn.commit()
    cursor.close()
    conn.close()
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
    cursor.close()
    conn.close()
    return jsonify(usuarios), 200

@app.route('/api/usuarios', methods=['POST'])
@requiere_rol('admin')
def crear_usuario():
    data = request.json
    username = data.get('usuario')
    password = data.get('contrasena')
    email = data.get('correo')
    role = data.get('role', 'cajero')
    telefono = data.get('telefono', '')
    empresa_id = request.empresa_id
    if not username or not password:
        return jsonify({'error': 'Faltan datos'}), 400
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM usuarios WHERE username = %s AND empresa_id = %s", (username, empresa_id))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({'error': 'El nombre de usuario ya existe'}), 400
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    cursor.execute("""
        INSERT INTO usuarios (username, password_hash, role, email, empresa_id, telefono, email_verificado, telefono_verificado)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE)
    """, (username, hashed, role, email, empresa_id, telefono))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 201

@app.route('/api/usuarios/<int:id>', methods=['PUT'])
@requiere_rol('admin')
def actualizar_usuario(id):
    data = request.json
    nuevo_username = data.get('username')
    nueva_password = data.get('password')
    empresa_id = request.empresa_id
    if not nuevo_username and not nueva_password:
        return jsonify({'error': 'No se enviaron datos para actualizar'}), 400
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM usuarios WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    if not cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({'error': 'Usuario no encontrado'}), 404
    if nuevo_username:
        cursor.execute("SELECT id FROM usuarios WHERE username = %s AND id != %s", (nuevo_username, id))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({'error': 'El nombre de usuario ya está en uso'}), 400
        cursor.execute("UPDATE usuarios SET username = %s WHERE id = %s", (nuevo_username, id))
    if nueva_password:
        hashed = bcrypt.hashpw(nueva_password.encode('utf-8'), bcrypt.gensalt())
        cursor.execute("UPDATE usuarios SET password_hash = %s WHERE id = %s", (hashed, id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 200

@app.route('/api/usuarios/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_usuario(id):
    empresa_id = request.empresa_id
    if id == request.user_id:
        return jsonify({'error': 'No puedes eliminarte a ti mismo'}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM usuarios WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 200

# ========== PROVEEDORES Y COMPRAS ==========
@app.route('/api/proveedores', methods=['GET'])
@requiere_rol('admin')
def listar_proveedores():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM proveedores WHERE empresa_id = %s", (empresa_id,))
    proveedores = cursor.fetchall()
    cursor.close()
    conn.close()
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
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 201

@app.route('/api/proveedores/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_proveedor(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM proveedores WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 200

@app.route('/api/ordenes-compra', methods=['GET'])
@requiere_rol('admin')
def listar_ordenes_compra():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT oc.*, p.nombre as proveedor_nombre
        FROM ordenes_compra oc
        JOIN proveedores p ON oc.proveedor_id = p.id
        WHERE oc.empresa_id = %s
        ORDER BY oc.fecha DESC
    """, (empresa_id,))
    ordenes = cursor.fetchall()
    for orden in ordenes:
        cursor.execute("""
            SELECT od.*, pr.descripcion as producto_nombre
            FROM ordenes_detalle od
            JOIN productos pr ON od.producto_codigo = pr.codigo
            WHERE od.orden_id = %s
        """, (orden['id'],))
        orden['detalle'] = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(ordenes), 200

@app.route('/api/ordenes-compra', methods=['POST'])
@requiere_rol('admin')
def crear_orden_compra():
    data = request.json
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            INSERT INTO ordenes_compra (proveedor_id, fecha, estado, total_usd, empresa_id)
            VALUES (%s, NOW(), 'recibida', %s, %s)
        """, (data['proveedor_id'], data['total_usd'], empresa_id))
        orden_id = cursor.lastrowid
        for item in data['detalle']:
            cursor.execute("""
                INSERT INTO ordenes_detalle (orden_id, producto_codigo, cantidad, precio_unitario, subtotal)
                VALUES (%s, %s, %s, %s, %s)
            """, (orden_id, item['codigo'], item['cantidad'], item['precio'], item['subtotal']))
            cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (item['codigo'], empresa_id))
            prod = cursor.fetchone()
            nuevo_stock = prod['existencia'] + item['cantidad']
            cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, item['codigo'], empresa_id))
            registrar_historial_inventario(cursor, item['codigo'], prod['descripcion'], 'ingreso_compra', prod['existencia'], nuevo_stock, f"Orden de compra #{orden_id}")
        conn.commit()
        crear_alerta(empresa_id, 'stock', f"Orden de compra #{orden_id} recibida. Se actualizó el inventario.")
        return jsonify({'status': 'OK', 'id': orden_id}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ========== ALERTAS ==========
@app.route('/api/alertas', methods=['GET'])
@requiere_rol('cajero')
def obtener_alertas():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM alertas WHERE empresa_id = %s AND leida = 0 ORDER BY fecha DESC", (empresa_id,))
    alertas = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(alertas), 200

@app.route('/api/alertas/marcar-leida/<int:id>', methods=['POST'])
@requiere_rol('cajero')
def marcar_alerta_leida(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE alertas SET leida = 1 WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 200

# ========== DASHBOARD PREDICTIVO ==========
@app.route('/api/dashboard-predictivo', methods=['GET'])
@requiere_rol('cajero')
def dashboard_predictivo():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT DATE(fecha) as dia, SUM(total_usd) as total_usd
        FROM facturas_cabecera
        WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND estado = 'activa' AND empresa_id = %s
        GROUP BY DATE(fecha)
        ORDER BY dia
    """, (empresa_id,))
    reales = cursor.fetchall()
    cursor.execute("""
        SELECT AVG(total_usd) as promedio
        FROM (
            SELECT DATE(fecha) as dia, SUM(total_usd) as total_usd
            FROM facturas_cabecera
            WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) AND estado = 'activa' AND empresa_id = %s
            GROUP BY DATE(fecha)
        ) as sub
    """, (empresa_id,))
    promedio_row = cursor.fetchone()
    promedio = float(promedio_row['promedio']) if promedio_row['promedio'] else 0
    prediccion = [{'dia': (datetime.now() + timedelta(days=i)).strftime('%Y-%m-%d'), 'prediccion_usd': promedio} for i in range(1, 8)]
    cursor.close()
    conn.close()
    return jsonify({'reales': reales, 'prediccion': prediccion}), 200

# ========== STRIPE ==========
@app.route('/api/create-payment-intent', methods=['POST'])
@requiere_rol('cajero')
def create_payment_intent():
    data = request.json
    amount = int(data['amount'] * 100)
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency='usd',
            metadata={'empresa_id': request.empresa_id}
        )
        return jsonify({'clientSecret': intent.client_secret}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# ========== EMPRESA Y TASA ==========
@app.route('/api/empresa', methods=['GET'])
@requiere_rol('cajero')
def obtener_empresa():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT nombre, rif, correo, telefono, direccion, tasa_cambio, logo_url FROM empresas WHERE id = %s", (empresa_id,))
    empresa = cursor.fetchone()
    cursor.close()
    conn.close()
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
    cursor.execute("""
        UPDATE empresas 
        SET nombre=%s, rif=%s, correo=%s, telefono=%s, direccion=%s, tasa_cambio=%s
        WHERE id=%s
    """, (data['nombre'], data['rif'], data['correo'], data['telefono'], data.get('direccion', ''), data.get('tasa_cambio', 544.58), empresa_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 200

@app.route('/api/empresa/logo', methods=['POST'])
@requiere_rol('admin')
def subir_logo():
    empresa_id = request.empresa_id
    if 'logo' not in request.files:
        return jsonify({'error': 'No se envió ningún archivo'}), 400
    file = request.files['logo']
    if file.filename == '':
        return jsonify({'error': 'Nombre de archivo vacío'}), 400
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
    cursor.close()
    conn.close()
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
    cursor.close()
    conn.close()
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
    cursor.close()
    conn.close()
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
                nombre = VALUES(nombre),
                telefono = VALUES(telefono),
                direccion = VALUES(direccion),
                email = VALUES(email)
        """, (data['rif'], data['nombre'], data.get('telefono', ''), data.get('direccion', ''), data.get('email', ''), empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except mysql.connector.Error as err:
        return jsonify({'error': str(err)}), 500
    finally:
        cursor.close()
        conn.close()

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
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 200

@app.route('/api/clientes/<int:id>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_cliente(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as total FROM facturas_cabecera WHERE cliente_id = %s AND empresa_id = %s", (id, empresa_id))
    total = cursor.fetchone()[0]
    if total > 0:
        cursor.close()
        conn.close()
        return jsonify({'error': 'No se puede eliminar el cliente porque tiene facturas asociadas'}), 400
    cursor.execute("DELETE FROM clientes WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK'}), 200

# ========== PRODUCTOS ==========
@app.route('/api/productos', methods=['GET'])
@requiere_rol('cajero')
def obtener_productos():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT 
            p.codigo, p.descripcion AS nombre, p.categoria, 
            COALESCE(p.unidad_medida, 'unidad') AS unidad_medida,
            COALESCE(p.tipo_producto, 'normal') AS tipo_producto,
            COALESCE(p.precio_compra, 0) AS costo,
            COALESCE(p.precio_venta, 0) AS venta,
            COALESCE(p.iva, 16) AS iva,
            COALESCE(p.existencia, 0) AS stock
        FROM productos p
        WHERE p.empresa_id = %s
        ORDER BY p.codigo
    """, (empresa_id,))
    productos = cursor.fetchall()
    for p in productos:
        for key in ['costo', 'venta', 'iva', 'stock']:
            if p[key] is not None:
                p[key] = float(p[key])
    cursor.close()
    conn.close()
    return jsonify(productos), 200

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
            return jsonify({'error': 'El stock, costo y venta no pueden ser negativos'}), 400
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
                                           anterior_stock, nuevo_stock, f"Actualización: costo={costo}, venta={venta}, iva={iva}% | Nota: {nota}")
        else:
            cursor.execute("""
                INSERT INTO productos (codigo, descripcion, categoria, precio_compra, precio_venta, existencia, iva, unidad_medida, tipo_producto, empresa_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (data['codigo'], data['nombre'], data['categoria'], costo, venta, nuevo_stock, iva, data.get('unidad_medida', 'unidad'), tipo_producto, empresa_id))
            registrar_historial_inventario(cursor, data['codigo'], data['nombre'], 'creacion', 0, nuevo_stock, f'Producto creado | Nota: {nota}')
        conn.commit()
        if nuevo_stock < 5 and tipo_producto not in ['receta', 'kit_hijo']:
            crear_alerta(empresa_id, 'stock_bajo', f"El producto {data['codigo']} tiene stock bajo: {nuevo_stock}")
        return jsonify({'status': 'OK'}), 200
    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/productos/<codigo>', methods=['DELETE'])
@requiere_rol('admin')
def eliminar_producto(codigo):
    data = request.json
    nota = data.get('nota', '') if data else ''
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT COUNT(*) as total FROM facturas_detalle WHERE producto_codigo = %s", (codigo,))
        if cursor.fetchone()['total'] > 0:
            return jsonify({'error': 'No se puede eliminar el producto porque tiene ventas asociadas'}), 400
        cursor.execute("SELECT descripcion, existencia FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
        prod = cursor.fetchone()
        if not prod:
            return jsonify({'error': 'Producto no encontrado'}), 404
        registrar_historial_inventario(cursor, codigo, prod['descripcion'], 'eliminacion',
                                       prod['existencia'], 0, f'Producto eliminado del sistema | Nota: {nota}')
        cursor.execute("DELETE FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err)}), 500
    finally:
        cursor.close()
        conn.close()

# ========== MOVIMIENTOS DE INVENTARIO ==========
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
        return jsonify({'error': 'Cantidad debe ser >0'}), 400
    if not nota:
        return jsonify({'error': 'Debe proporcionar una nota'}), 400
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT descripcion, existencia FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
    prod = cursor.fetchone()
    if not prod:
        cursor.close()
        conn.close()
        return jsonify({'error': 'Producto no encontrado'}), 404
    stock_anterior = float(prod['existencia'])
    if tipo == 'ingreso':
        nuevo_stock = stock_anterior + cantidad
        tipo_mov = 'ingreso'
    else:
        nuevo_stock = stock_anterior - cantidad
        if nuevo_stock < 0:
            cursor.close()
            conn.close()
            return jsonify({'error': 'Stock negativo no permitido'}), 400
        tipo_mov = 'reduccion'
    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, codigo, empresa_id))
    registrar_historial_inventario(cursor, codigo, prod['descripcion'], tipo_mov, stock_anterior, nuevo_stock, nota)
    conn.commit()
    cursor.close()
    conn.close()
    if nuevo_stock < 5:
        crear_alerta(empresa_id, 'stock_bajo', f"Producto {codigo} ahora tiene stock {nuevo_stock}")
    return jsonify({'status': 'OK', 'nuevo_stock': nuevo_stock}), 200

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
    cursor.close()
    conn.close()
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
        cursor.close()
        conn.close()

@app.route('/api/recetas/<int:id>', methods=['GET'])
@requiere_rol('cajero')
def obtener_receta(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM recetas WHERE id = %s AND empresa_id = %s", (id, empresa_id))
    receta = cursor.fetchone()
    if not receta:
        cursor.close()
        conn.close()
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
    cursor.close()
    conn.close()
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
        cursor.close()
        conn.close()

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
        cursor.close()
        conn.close()

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
    cursor.close()
    conn.close()
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
        cursor.close()
        conn.close()

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
        cursor.close()
        conn.close()

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
        cursor.close()
        conn.close()

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
            return jsonify({'error': f'Stock insuficiente del producto padre ({padre_codigo})'}), 400
        cursor.execute("SELECT producto_hijo_codigo, cantidad_estimada FROM kit_detalle WHERE kit_id = %s AND empresa_id = %s", (kit_id, empresa_id))
        hijos = cursor.fetchall()
        if not hijos:
            return jsonify({'error': 'El kit no tiene productos hijos'}), 400
        for hijo in hijos:
            factor = float(hijo['cantidad_estimada'])
            cantidad_hijo = factor * cantidad_padre
            cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (hijo['producto_hijo_codigo'], empresa_id))
            hijo_actual = cursor.fetchone()
            if hijo_actual:
                nuevo_stock = float(hijo_actual['existencia']) + cantidad_hijo
                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock, hijo['producto_hijo_codigo'], empresa_id))
                registrar_historial_inventario(cursor, hijo['producto_hijo_codigo'], hijo_actual['descripcion'], 'despiece',
                                               float(hijo_actual['existencia']), nuevo_stock, f"Despiece desde {padre_codigo}, cantidad padre: {cantidad_padre}")
        nuevo_stock_padre = float(padre['existencia']) - cantidad_padre
        cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (nuevo_stock_padre, padre_codigo, empresa_id))
        registrar_historial_inventario(cursor, padre_codigo, padre['descripcion'], 'despiece',
                                       float(padre['existencia']), nuevo_stock_padre, f"Despiece a través de kit {kit_id}, cantidad: {cantidad_padre}")
        conn.commit()
        return jsonify({'status': 'OK', 'mensaje': f'Despiece realizado. Nuevo stock padre: {nuevo_stock_padre}'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

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
                return jsonify({'error': f'Producto {h["codigo"]} no pertenece a este kit'}), 400
        padre_necesario = sum(h['cantidad'] / factores[h['codigo']] for h in hijos_procesar)
        cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (padre_codigo, empresa_id))
        padre = cursor.fetchone()
        if not padre or padre['existencia'] < padre_necesario:
            return jsonify({'error': f'Stock insuficiente del padre. Necesario: {padre_necesario}, disponible: {padre["existencia"]}'}), 400
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
                                               float(hijo_actual['existencia']), nuevo_stock, f"Despiece selectivo desde {padre_codigo}")
        conn.commit()
        return jsonify({'status': 'OK', 'mensaje': f'Despiece selectivo realizado. Padre descontado: {padre_necesario}'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

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
    cursor.close()
    conn.close()
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
        cursor.close()
        conn.close()
        return jsonify({'error': 'Ya tienes una caja abierta'}), 400
    now = datetime.now()
    cursor.execute("""
        INSERT INTO caja_sesion (empresa_id, usuario_id, fecha_apertura, estado)
        VALUES (%s, %s, %s, 'abierta')
    """, (empresa_id, usuario_id, now))
    conn.commit()
    cursor.close()
    conn.close()
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
        cursor.close()
        conn.close()
        return jsonify({'error': 'No hay caja abierta'}), 400
    caja_id = caja['id']
    cursor.execute("""
        SELECT fc.total_usd, fc.subtotal_usd, fc.iva_usd, fc.monto_servicio_usd
        FROM facturas_cabecera fc
        WHERE fc.caja_sesion_id = %s AND fc.estado = 'activa'
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
    for m in ['Efectivo', 'Divisa', 'Pago Movil', 'Biopago', 'Transferencia', 'Punto de Venta', 'Vale', 'Cashea', 'Casa']:
        if m not in pagos_dict:
            pagos_dict[m] = {'usd': 0.0, 'bs': 0.0}
    cursor.execute("SELECT ultimo_reporte_z FROM empresas WHERE id = %s", (empresa_id,))
    row = cursor.fetchone()
    if row and row['ultimo_reporte_z'] is not None:
        nuevo_numero = row['ultimo_reporte_z'] + 1
        cursor.execute("UPDATE empresas SET ultimo_reporte_z = %s WHERE id = %s", (nuevo_numero, empresa_id))
    else:
        nuevo_numero = 1
        try:
            cursor.execute("ALTER TABLE empresas ADD COLUMN ultimo_reporte_z INT DEFAULT 0")
            cursor.execute("UPDATE empresas SET ultimo_reporte_z = 1 WHERE id = %s", (empresa_id,))
        except:
            pass
    num_reporte = f"REP-{nuevo_numero:06d}"
    cursor.execute("SELECT nombre, rif, direccion FROM empresas WHERE id = %s", (empresa_id,))
    empresa = cursor.fetchone()
    ahora = datetime.now()
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
        'empresa': empresa
    })
    cursor.execute("""
        INSERT INTO historial_cierres (fecha_cierre, usuario_id, total_usd, total_bs, datos, empresa_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (ahora, usuario_id, total_cobrado_usd, total_cobrado_bs, datos_json, empresa_id))
    cursor.execute("""
        UPDATE caja_sesion SET estado = 'cerrada', fecha_cierre = %s, total_usd = %s, total_bs = %s
        WHERE id = %s
    """, (ahora, total_cobrado_usd, total_cobrado_bs, caja_id))
    conn.commit()
    cursor.close()
    conn.close()
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
            'tasa': tasa
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
        cursor.close()
        conn.close()
        return jsonify({'error': 'No hay cajas abiertas'}), 400
    ahora = datetime.now()
    for c in cajas:
        caja_id = c['id']
        usuario_id = c['usuario_id']
        cursor.execute("""
            SELECT SUM(total_usd) as total_usd, SUM(monto_bs) as total_bs
            FROM facturas_cabecera
            WHERE caja_sesion_id = %s AND estado = 'activa'
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
    cursor.close()
    conn.close()
    return jsonify({'status': 'OK', 'mensaje': 'Cierre general completado'}), 200

# ========== FACTURAS ==========
@app.route('/api/facturas', methods=['POST'])
@requiere_rol('cajero')
def guardar_factura():
    data = request.json
    empresa_id = request.empresa_id
    usuario_id = request.user_id
    articulos = data.get('articulos', [])
    if not articulos:
        return jsonify({'error': 'El carrito está vacío'}), 400
    conn = get_db_connection()
    cursor_caja = conn.cursor(dictionary=True)
    cursor_caja.execute("""
        SELECT id FROM caja_sesion
        WHERE empresa_id = %s AND usuario_id = %s AND estado = 'abierta'
        ORDER BY fecha_apertura DESC LIMIT 1
    """, (empresa_id, usuario_id))
    caja = cursor_caja.fetchone()
    cursor_caja.close()
    if not caja:
        conn.close()
        return jsonify({'error': 'Debes abrir tu caja antes de facturar'}), 403
    caja_sesion_id = caja['id']
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT tasa_cambio FROM empresas WHERE id = %s", (empresa_id,))
        tasa_row = cursor.fetchone()
        tasa = float(tasa_row['tasa_cambio']) if tasa_row else 544.58
        moneda = data.get('moneda', 'Bs')
        pagos = data.get('pagos', [])
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
        total_usd = total_sin_servicio + monto_servicio
        total_bs = total_usd * Decimal(str(tasa))
        metodo = data.get('metodo_pago', 'Efectivo')
        total_pagado_usd = Decimal('0')
        for p in pagos:
            if p.get('metodo_pago') == 'Casa':
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
        elif metodo == 'Casa':
            if total_pagado_usd > tolerancia:
                return jsonify({'error': 'Las facturas de Casa no pueden tener pagos'}), 400
        else:
            if abs(total_pagado_usd - total_usd) > tolerancia:
                return jsonify({'error': f'El monto pagado ({total_pagado_usd}) no coincide con el total ({total_usd})'}), 400
        referencia = data.get('referencia', '')
        extras = None
        if metodo == 'Cashea':
            extras = json.dumps({'inicial': float(data.get('cashea_inicial', 0)), 'cuotas': int(data.get('cashea_cuotas', 0))})
            referencia = ''
        elif metodo == 'Casa':
            extras = json.dumps({'nota': data.get('nota_casa', '')})
            referencia = ''
        cliente_id = data.get('cliente_id')
        if not cliente_id:
            cursor.execute("SELECT id FROM clientes WHERE empresa_id = %s LIMIT 1", (empresa_id,))
            row = cursor.fetchone()
            cliente_id = row['id'] if row else None
            if not cliente_id:
                cursor.execute("INSERT INTO clientes (rif, nombre, empresa_id) VALUES ('J-00000000-0', 'Cliente General', %s)", (empresa_id,))
                cliente_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO facturas_cabecera
            (fecha, cliente_id, usuario_id, caja_sesion_id, subtotal_usd, iva_usd, total_usd, tasa_cambio,
             metodo_pago, referencia, extras, moneda, monto_bs, estado, empresa_id,
             porcentaje_servicio, monto_servicio_usd)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'activa', %s, %s, %s)
        """, (cliente_id, usuario_id, caja_sesion_id, float(subtotal_usd - iva_total_usd), float(iva_total_usd),
              float(total_usd), tasa, metodo, referencia, extras, moneda, float(total_bs), empresa_id,
              float(porcentaje_servicio), float(monto_servicio)))
        factura_id = cursor.lastrowid
        for det in productos_detalle:
            if det['tipo'] == 'receta':
                ingredientes = recetas_ingredientes.get(det['codigo'], [])
                for ing in ingredientes:
                    needed = ing['cantidad'] * det['cantidad']
                    cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (ing['codigo'], empresa_id))
                    ing_data = cursor.fetchone()
                    stock_ant = Decimal(str(ing_data['existencia']))
                    nuevo_stock = stock_ant - needed
                    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nuevo_stock), ing['codigo'], empresa_id))
                    registrar_historial_inventario(cursor, ing['codigo'], ing_data['descripcion'], 'venta', float(stock_ant), float(nuevo_stock), f"Factura #{factura_id} - Receta {det['codigo']}")
            else:
                cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (det['codigo'], empresa_id))
                prod_data = cursor.fetchone()
                stock_ant = Decimal(str(prod_data['existencia']))
                nuevo_stock = stock_ant - det['cantidad']
                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nuevo_stock), det['codigo'], empresa_id))
                registrar_historial_inventario(cursor, det['codigo'], prod_data['descripcion'], 'venta', float(stock_ant), float(nuevo_stock), f"Factura #{factura_id}")
            cursor.execute("""
                INSERT INTO facturas_detalle (factura_numero, producto_codigo, cantidad, precio_unitario, iva_unitario,
                    descuento, nota_descuento, subtotal_sin_iva, subtotal_con_iva, empresa_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (factura_id, det['codigo'], float(det['cantidad']), float(det['precio_unitario']), float(det['iva_unitario']),
                  float(det['descuento']), det['nota_desc'], float(det['subtotal_sin_iva']), float(det['subtotal_con_iva']), empresa_id))
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
            cursor.execute("""
                INSERT INTO facturas_pagos (factura_numero, metodo_pago, monto_usd, monto_bs, referencia, nota)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (factura_id, p['metodo_pago'], monto_usd, monto_bs, p.get('referencia', ''), p.get('nota', '')))
        conn.commit()
        crear_alerta(empresa_id, 'factura', f"Factura #{factura_id} creada por {request.username}")
        return jsonify({'status': 'OK', 'factura_id': factura_id}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/facturas', methods=['GET'])
@requiere_rol('cajero')
def listar_facturas():
    empresa_id = request.empresa_id
    search = request.args.get('search', '')
    fecha_desde = request.args.get('fecha_desde', '')
    fecha_hasta = request.args.get('fecha_hasta', '')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    query = """
        SELECT fc.numero AS id, fc.fecha, COALESCE(c.nombre, 'Cliente General') AS cliente_nombre,
               fc.tasa_cambio, fc.total_usd, fc.monto_bs, fc.metodo_pago, fc.referencia, fc.estado,
               u.username as cajero,
               fc.porcentaje_servicio, fc.monto_servicio_usd
        FROM facturas_cabecera fc
        LEFT JOIN clientes c ON fc.cliente_id = c.id
        LEFT JOIN usuarios u ON fc.usuario_id = u.id
        WHERE fc.empresa_id = %s
    """
    params = [empresa_id]
    if search:
        query += " AND (fc.numero LIKE %s OR c.nombre LIKE %s)"
        params.extend([f'%{search}%', f'%{search}%'])
    if fecha_desde:
        query += " AND DATE(fc.fecha) >= %s"
        params.append(fecha_desde)
    if fecha_hasta:
        query += " AND DATE(fc.fecha) <= %s"
        params.append(fecha_hasta)
    query += " ORDER BY fc.fecha DESC"
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
    cursor.close()
    conn.close()
    return jsonify(facturas), 200

@app.route('/api/facturas/<int:id>', methods=['GET'])
@requiere_rol('cajero')
def detalle_factura(id):
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT fc.numero, fc.fecha, fc.subtotal_usd, fc.iva_usd, fc.total_usd, fc.tasa_cambio,
               fc.metodo_pago, fc.referencia, fc.extras, fc.moneda, fc.monto_bs, fc.estado,
               fc.porcentaje_servicio, fc.monto_servicio_usd,
               c.nombre AS cliente_nombre, c.rif AS cliente_rif, u.username as cajero
        FROM facturas_cabecera fc
        LEFT JOIN clientes c ON fc.cliente_id = c.id
        LEFT JOIN usuarios u ON fc.usuario_id = u.id
        WHERE fc.numero = %s AND fc.empresa_id = %s
    """, (id, empresa_id))
    factura = cursor.fetchone()
    if not factura:
        cursor.close()
        conn.close()
        return jsonify({'error': 'Factura no encontrada'}), 404
    cursor.execute("""
        SELECT p.descripcion AS nombre, fd.cantidad, fd.precio_unitario, fd.iva_unitario,
               fd.descuento, fd.nota_descuento, fd.subtotal_sin_iva, fd.subtotal_con_iva
        FROM facturas_detalle fd
        JOIN productos p ON fd.producto_codigo = p.codigo
        WHERE fd.factura_numero = %s
    """, (id,))
    productos = cursor.fetchall()
    for p in productos:
        for key in ['precio_unitario', 'iva_unitario', 'descuento', 'subtotal_sin_iva', 'subtotal_con_iva']:
            if p[key] is not None:
                p[key] = float(p[key])
    cursor.execute("SELECT metodo_pago, monto_usd, monto_bs, referencia, nota FROM facturas_pagos WHERE factura_numero = %s", (id,))
    pagos = cursor.fetchall()
    for p in pagos:
        p['monto_usd'] = float(p['monto_usd'] or 0)
        p['monto_bs'] = float(p['monto_bs'] or 0)
    cursor.close()
    conn.close()
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
        cursor.execute("SELECT estado FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        factura = cursor.fetchone()
        if not factura or factura['estado'] == 'anulada':
            return jsonify({'error': 'Factura no encontrada o ya anulada'}), 400
        cursor.execute("SELECT producto_codigo, cantidad FROM facturas_detalle WHERE factura_numero = %s", (id,))
        detalles = cursor.fetchall()
        for det in detalles:
            codigo = det['producto_codigo']
            cantidad = Decimal(str(det['cantidad']))
            cursor.execute("SELECT tipo_producto FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
            tipo = cursor.fetchone()['tipo_producto']
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
                    nueva_cantidad = Decimal(str(prod['existencia'])) + needed
                    cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nueva_cantidad), ing['producto_codigo'], empresa_id))
                    registrar_historial_inventario(cursor, ing['producto_codigo'], prod['descripcion'], 'anulacion',
                                                   float(prod['existencia']), float(nueva_cantidad), f"Anulación factura #{id} - Receta {codigo} - Motivo: {motivo}")
            else:
                cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
                prod = cursor.fetchone()
                nueva_cantidad = Decimal(str(prod['existencia'])) + cantidad
                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nueva_cantidad), codigo, empresa_id))
                registrar_historial_inventario(cursor, codigo, prod['descripcion'], 'anulacion',
                                               float(prod['existencia']), float(nueva_cantidad), f"Anulación factura #{id} - Motivo: {motivo}")
        cursor.execute("UPDATE facturas_cabecera SET estado = 'anulada', motivo_anulacion = %s WHERE numero = %s AND empresa_id = %s", (motivo, id, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK', 'mensaje': 'Factura anulada y stock restaurado'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

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
        cursor.execute("SELECT estado FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        factura = cursor.fetchone()
        if not factura:
            return jsonify({'error': 'Factura no encontrada'}), 404
        if factura['estado'] == 'activa':
            cursor.execute("SELECT producto_codigo, cantidad FROM facturas_detalle WHERE factura_numero = %s", (id,))
            for det in cursor.fetchall():
                codigo = det['producto_codigo']
                cantidad = Decimal(str(det['cantidad']))
                cursor.execute("SELECT existencia, descripcion FROM productos WHERE codigo = %s AND empresa_id = %s", (codigo, empresa_id))
                prod = cursor.fetchone()
                nueva_cantidad = Decimal(str(prod['existencia'])) + cantidad
                cursor.execute("UPDATE productos SET existencia = %s WHERE codigo = %s AND empresa_id = %s", (float(nueva_cantidad), codigo, empresa_id))
                registrar_historial_inventario(cursor, codigo, prod['descripcion'], 'eliminacion_factura',
                                               float(prod['existencia']), float(nueva_cantidad), f"Eliminación física factura #{id} - Motivo: {motivo}")
        cursor.execute("DELETE FROM facturas_detalle WHERE factura_numero = %s", (id,))
        cursor.execute("DELETE FROM facturas_pagos WHERE factura_numero = %s", (id,))
        cursor.execute("DELETE FROM facturas_cabecera WHERE numero = %s AND empresa_id = %s", (id, empresa_id))
        conn.commit()
        return jsonify({'status': 'OK'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ========== REPORTE X ==========
@app.route('/api/reporte-x', methods=['GET'])
@requiere_rol('cajero')
def reporte_detallado():
    empresa_id = request.empresa_id
    usuario_id = request.user_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT fc.numero AS factura_id, fc.fecha, c.nombre AS cliente, fc.metodo_pago, fc.referencia, fc.moneda,
               fc.porcentaje_servicio, fc.monto_servicio_usd,
               p.descripcion AS producto, fd.cantidad, fd.precio_unitario, fd.iva_unitario, fd.descuento, fd.nota_descuento,
               fd.subtotal_sin_iva, fd.subtotal_con_iva
        FROM facturas_cabecera fc
        LEFT JOIN clientes c ON fc.cliente_id = c.id
        JOIN facturas_detalle fd ON fc.numero = fd.factura_numero
        JOIN productos p ON fd.producto_codigo = p.codigo
        WHERE DATE(fc.fecha) = CURDATE() AND fc.estado = 'activa' AND fc.empresa_id = %s AND fc.usuario_id = %s
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
    cursor.close()
    conn.close()
    return jsonify({
        'fecha_hora': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
        fecha_inicio = datetime.now().strftime('%Y-%m-%d')
        fecha_fin = fecha_inicio
    elif periodo == 'semana':
        fecha_inicio = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        fecha_fin = datetime.now().strftime('%Y-%m-%d')
    elif periodo == 'mes':
        fecha_inicio = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        fecha_fin = datetime.now().strftime('%Y-%m-%d')
    else:
        fecha_inicio = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        fecha_fin = datetime.now().strftime('%Y-%m-%d')
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
        JOIN productos p ON fd.producto_codigo = p.codigo
        WHERE DATE(fc.fecha) BETWEEN %s AND %s
          AND fc.estado = 'activa'
          AND fc.empresa_id = %s
        GROUP BY p.codigo, p.descripcion
        ORDER BY {order_field}
        LIMIT %s
    """
    cursor.execute(query, (fecha_inicio, fecha_fin, empresa_id, limite))
    resultados = cursor.fetchall()
    cursor.close()
    conn.close()
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
                       h.total_usd, h.total_bs, h.datos
                FROM historial_cierres h
                JOIN usuarios u ON h.usuario_id = u.id
                WHERE h.empresa_id = %s AND h.usuario_id = %s
                ORDER BY h.fecha_cierre DESC
            """, (empresa_id, usuario_id))
        else:
            cursor.execute("""
                SELECT h.id, h.fecha_cierre, h.usuario_id, u.username,
                       h.total_usd, h.total_bs, h.datos
                FROM historial_cierres h
                JOIN usuarios u ON h.usuario_id = u.id
                WHERE h.empresa_id = %s
                ORDER BY h.fecha_cierre DESC
            """, (empresa_id,))
    else:
        cursor.execute("""
            SELECT h.id, h.fecha_cierre, h.usuario_id, u.username,
                   h.total_usd, h.total_bs, h.datos
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
    cursor.close()
    conn.close()
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
    cursor.close()
    conn.close()
    return jsonify(registros), 200

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
        WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) AND estado = 'activa' AND empresa_id = %s
        GROUP BY DATE(fecha)
        ORDER BY dia
    """, (empresa_id,))
    diarias = cursor.fetchall()
    for d in diarias:
        d['total_bs'] = float(d['total_bs'] or 0)
    cursor.execute("""
        SELECT metodo_pago, SUM(total_usd) as total_usd
        FROM facturas_cabecera
        WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND estado = 'activa' AND empresa_id = %s
        GROUP BY metodo_pago
    """, (empresa_id,))
    por_metodo = cursor.fetchall()
    for p in por_metodo:
        p['total_usd'] = float(p['total_usd'] or 0)
    cursor.close()
    conn.close()
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
        cursor.close()
        conn.close()

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
        SELECT fc.numero, fc.fecha, c.nombre, fc.moneda, fc.total_usd, fc.monto_bs,
               fc.metodo_pago, fc.referencia, fc.estado, u.username as cajero,
               fc.porcentaje_servicio, fc.monto_servicio_usd
        FROM facturas_cabecera fc
        LEFT JOIN clientes c ON fc.cliente_id = c.id
        LEFT JOIN usuarios u ON fc.usuario_id = u.id
        WHERE fc.empresa_id = %s
        ORDER BY fc.numero DESC
    """
    return exportar_a_csv(consulta, (empresa_id,), 'historial_facturas')

# ========== SUPER ADMIN ==========
@app.route('/api/super/empresas', methods=['GET'])
@requiere_super_admin
def super_listar_empresas():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
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
    cursor.close()
    conn.close()
    return jsonify(empresas), 200

@app.route('/api/super/empresas/<int:id>', methods=['PUT'])
@requiere_super_admin
def super_editar_empresa(id):
    data = request.json
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE empresas 
            SET nombre=%s, rif=%s, correo=%s, telefono=%s, direccion=%s, tasa_cambio=%s
            WHERE id=%s
        """, (data.get('nombre'), data.get('rif'), data.get('correo'), data.get('telefono'),
              data.get('direccion'), data.get('tasa_cambio'), id))
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
        cursor.close()
        conn.close()

@app.route('/api/super/empresas/<int:id>/toggle-status', methods=['POST'])
@requiere_super_admin
def super_toggle_empresa_status(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE empresas SET activa = NOT activa WHERE id = %s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
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
        cursor.close()
        conn.close()

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
        cursor.close()
        conn.close()

# ========== RUTAS PARA SERVIR EL FRONTEND ==========
@app.route('/')
def serve_index():
    return send_from_directory('.', 'login.html')

@app.route('/<path:filename>')
def serve_static_files(filename):
    if filename.startswith('static/'):
        return send_from_directory('.', filename)
    return send_from_directory('.', filename)

# ========== INICIO ==========
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)