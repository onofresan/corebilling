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

# ========== RUTAS DE AUTENTICACIÓN ==========
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

# ========== PRODUCTOS ==========
@app.route('/api/productos', methods=['GET'])
@requiere_rol('cajero')
def obtener_productos():
    empresa_id = request.empresa_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT codigo, descripcion as nombre, categoria, COALESCE(unidad_medida, 'unidad') as unidad_medida, COALESCE(tipo_producto, 'normal') as tipo_producto, COALESCE(precio_compra, 0) as costo, COALESCE(precio_venta, 0) as venta, COALESCE(iva, 16) as iva, COALESCE(existencia, 0) as stock FROM productos WHERE empresa_id = %s ORDER BY codigo", (empresa_id,))
    productos = cursor.fetchall()
    for p in productos:
        for key in ['costo', 'venta', 'iva', 'stock']:
            if p[key] is not None:
                p[key] = float(p[key])
    cursor.close()
    conn.close()
    return jsonify(productos), 200

# ========== FACTURAS ==========
@app.route('/api/facturas', methods=['GET'])
@requiere_rol('cajero')
def listar_facturas():
    empresa_id = request.empresa_id
    search = request.args.get('search', '')
    fecha_desde = request.args.get('fecha_desde', '')
    fecha_hasta = request.args.get('fecha_hasta', '')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    query = "SELECT fc.numero AS id, fc.fecha, COALESCE(c.nombre, 'Cliente General') AS cliente_nombre, fc.tasa_cambio, fc.total_usd, fc.monto_bs, fc.metodo_pago, fc.referencia, fc.estado, u.username as cajero, fc.porcentaje_servicio, fc.monto_servicio_usd FROM facturas_cabecera fc LEFT JOIN clientes c ON fc.cliente_id = c.id LEFT JOIN usuarios u ON fc.usuario_id = u.id WHERE fc.empresa_id = %s"
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