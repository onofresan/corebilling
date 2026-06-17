# ===== MONKEY PATCH AL PRINCIPIO (evita advertencia) =====
from gevent import monkey
monkey.patch_all()

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
import requests

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

# ========== CONEXIÓN A BASE DE DATOS (CORREGIDA) ==========
def get_db_connection():
    """
    Establece conexión con MySQL en Aiven usando SSL y timeout.
    """
    try:
        conn = mysql.connector.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            port=int(os.environ.get('DB_PORT', 3306)),
            user=os.environ.get('DB_USER', 'root'),
            password=os.environ.get('DB_PASSWORD', 'Koko.2590'),
            database=os.environ.get('DB_NAME', 'facturacion'),
            use_pure=True,                     # Evita problemas con la extensión C
            connection_timeout=10,              # Timeout de conexión en segundos
            ssl_disabled=False,                 # Obliga SSL (Aiven lo requiere)
            # Si Aiven te da un certificado .pem, descomenta la siguiente línea:
            # ssl_ca='/path/to/ca.pem'
        )
        return conn
    except mysql.connector.Error as err:
        # Lanza una excepción con mensaje claro para el endpoint
        raise Exception(f"Error de conexión a la base de datos: {err}")

# ========== FUNCIONES AUXILIARES ==========
def obtener_tasa_bcv():
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

# ========== AUTENTICACIÓN JWT (CORREGIDO) ==========
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('usuario')
    password = data.get('contrasena')

    if not username or not password:
        return jsonify({'error': 'Faltan credenciales'}), 400

    try:
        conn = get_db_connection()
    except Exception as e:
        # Error de conexión a la BD
        return jsonify({'error': f'Error de conexión a la base de datos: {str(e)}'}), 500

    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
        user = cursor.fetchone()
    except mysql.connector.Error as err:
        cursor.close()
        conn.close()
        return jsonify({'error': f'Error en la consulta: {str(err)}'}), 500
    finally:
        cursor.close()
        conn.close()

    if not user or not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
        return jsonify({'error': 'Credenciales inválidas'}), 401

    if user['role'] != 'super_admin':
        try:
            conn2 = get_db_connection()
            cursor2 = conn2.cursor(dictionary=True)
            cursor2.execute("SELECT activa FROM empresas WHERE id = %s", (user['empresa_id'],))
            empresa = cursor2.fetchone()
            cursor2.close()
            conn2.close()
            if not empresa or not empresa.get('activa', True):
                return jsonify({'error': 'Empresa desactivada'}), 403
        except Exception as e:
            return jsonify({'error': f'Error verificando empresa: {str(e)}'}), 500

    token = jwt.encode({
        'user_id': user['id'],
        'role': user['role'],
        'username': user['username'],
        'empresa_id': user.get('empresa_id')
    }, app.config['SECRET_KEY'], algorithm='HS256')

    return jsonify({
        'token': token,
        'role': user['role'],
        'username': user['username'],
        'empresa_id': user.get('empresa_id')
    }), 200

# ========== EL RESTO DEL CÓDIGO PERMANECE IGUAL ==========
# (Todas las demás rutas y funciones se mantienen sin cambios)
# Para que el archivo sea completo, debes copiar el resto de tu código
# desde la línea 75 hasta el final, exactamente como lo tenías,
# pero con la función get_db_connection reemplazada por la nueva.

# ========== INICIO ==========
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
