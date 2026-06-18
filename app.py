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
    try:
        conn = mysql.connector.connect(
            host='corebilling-db-onofresanchez1515-bd0c.j.aivencloud.com',
            port=22119,
            user='avnadmin',
            password='AVNS_MKNpYf2pgrWhwGYFa3a',
            database='defaultdb',
            use_pure=True,
            connection_timeout=30,
            ssl_disabled=False,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT DATABASE();")
        db_name = cursor.fetchone()[0]
        print(f"✅ Conectado a: {db_name}")
        cursor.close()
        return conn
    except mysql.connector.Error as err:
        raise Exception(f"Error de conexión: {err}")

# ========== INICIALIZAR TABLAS ==========
def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS empresas (
                id INT AUTO_INCREMENT PRIMARY KEY,
                nombre VARCHAR(100),
                rif VARCHAR(20),
                correo VARCHAR(100),
                telefono VARCHAR(20),
                direccion TEXT,
                tasa_cambio DECIMAL(10,2) DEFAULT 544.58,
                logo_url VARCHAR(255),
                activa BOOLEAN DEFAULT TRUE,
                ultimo_reporte_z INT DEFAULT 0
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) DEFAULT 'cajero',
                email VARCHAR(100),
                empresa_id INT,
                telefono VARCHAR(20),
                email_verificado BOOLEAN DEFAULT TRUE,
                telefono_verificado BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            INSERT IGNORE INTO empresas (id, nombre, rif, correo, telefono, activa)
            VALUES (1, 'hatkokoland', 'J-30391009-0', 'onofresanchez1515@gmail.com', '04248553424', 1)
        """)
        
        cursor.execute("""
            INSERT IGNORE INTO usuarios (username, password_hash, role, email, empresa_id)
            VALUES ('restaurante', '$2b$12$gL6YmB7f1Lw4PqUeYpA4qOZbC0VcG9rHfN3sKjPqR8tLxZmVfWnYq', 'admin', 'restaurante@corebilling.com', 1)
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Base de datos inicializada correctamente")
        return True
    except Exception as e:
        print(f"❌ Error al inicializar BD: {e}")
        return False

# ========== ENDPOINT DE DIAGNÓSTICO ==========
@app.route('/api/test-db', methods=['GET'])
def test_db():
    try:
        init_db()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DATABASE();")
        db_name = cursor.fetchone()[0]
        cursor.execute("SHOW TABLES;")
        tables = [row[0] for row in cursor.fetchall()]
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
