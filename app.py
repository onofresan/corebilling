from gevent import monkey
monkey.patch_all()

import os
import mysql.connector
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime, timedelta
import json
import bcrypt
import jwt
from functools import wraps
from flask_socketio import SocketIO
import stripe
import requests
import traceback

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'clave_jwt_super_secreta')
CORS(app, origins="*", supports_credentials=True)

# ========== CONEXIÓN FORZADA A defaultdb ==========
def get_db_connection():
    """Conexión directa a defaultdb con credenciales fijas"""
    try:
        conn = mysql.connector.connect(
            host='corebilling-db-onofresanchez1515-bd0c.j.aivencloud.com',
            port=22119,
            user='avnadmin',
            password='AVNS_MKNpYf2pgrWhwGYFa3a',
            database='defaultdb',  # FORZADO
            use_pure=True,
            connection_timeout=30,
            ssl_disabled=False,
        )
        # Verificar conexión
        cursor = conn.cursor()
        cursor.execute("SELECT DATABASE();")
        db_name = cursor.fetchone()[0]
        print(f"✅ Conectado a: {db_name}")  # Esto se verá en logs de Render
        cursor.close()
        return conn
    except mysql.connector.Error as err:
        raise Exception(f"Error de conexión: {err}")

# ========== ENDPOINT DE DIAGNÓSTICO ==========
@app.route('/api/test-db', methods=['GET'])
def test_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Ver base de datos actual
        cursor.execute("SELECT DATABASE();")
        db_name = cursor.fetchone()[0]
        
        # 2. Ver tablas
        cursor.execute("SHOW TABLES;")
        tables = [row[0] for row in cursor.fetchall()]
        
        # 3. Ver si usuarios existe
        cursor.execute("SELECT COUNT(*) FROM usuarios;")
        count = cursor.fetchone()[0]
        
        cursor.close()
        conn.close()
        
        return jsonify({
            'status': 'OK',
            'database': db_name,
            'tables': tables,
            'usuarios_count': count
        }), 200
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

# ========== LOGIN ==========
@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.json
        username = data.get('usuario')
        password = data.get('contrasena')
        
        if not username or not password:
            return jsonify({'error': 'Faltan credenciales'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Forzar base de datos (por si acaso)
        cursor.execute("USE defaultdb;")
        
        cursor.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if not user:
            return jsonify({'error': 'Usuario no encontrado'}), 401
        
        if not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            return jsonify({'error': 'Contraseña incorrecta'}), 401
        
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
        
    except Exception as e:
        print("ERROR en login:", traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ========== FRONTEND ==========
@app.route('/')
def serve_index():
    return send_from_directory('.', 'login.html')

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory('.', filename)

# ========== INICIO ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
