import mysql.connector
import os
import bcrypt

def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', 'Koko.2590'),
        database=os.environ.get('DB_NAME', 'facturacion')
    )

def init_database():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Crear tabla empresas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS empresas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(255) NOT NULL,
            rif VARCHAR(50) UNIQUE NOT NULL,
            correo VARCHAR(255),
            telefono VARCHAR(50),
            direccion TEXT,
            tasa_cambio DECIMAL(10,4) DEFAULT 544.58,
            logo_url VARCHAR(255),
            activa BOOLEAN DEFAULT TRUE,
            ultimo_reporte_z INT DEFAULT 0,
            permite_reiniciar_historial BOOLEAN DEFAULT FALSE
        )
    """)
    
    # Crear tabla usuarios (incluyendo las columnas que usas)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            email VARCHAR(255),
            telefono VARCHAR(50),
            role VARCHAR(50) DEFAULT 'cajero',
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla clientes
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            rif VARCHAR(50) UNIQUE NOT NULL,
            nombre VARCHAR(255) NOT NULL,
            telefono VARCHAR(50),
            direccion TEXT,
            email VARCHAR(255),
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla productos
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS productos (
            codigo VARCHAR(50) PRIMARY KEY,
            descripcion VARCHAR(255) NOT NULL,
            categoria VARCHAR(100),
            precio_compra DECIMAL(10,2) DEFAULT 0,
            precio_venta DECIMAL(10,2) DEFAULT 0,
            existencia DECIMAL(10,2) DEFAULT 0,
            iva DECIMAL(5,2) DEFAULT 16,
            unidad_medida VARCHAR(50) DEFAULT 'unidad',
            tipo_producto VARCHAR(20) DEFAULT 'normal',
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla facturas_cabecera
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS facturas_cabecera (
            numero INT AUTO_INCREMENT PRIMARY KEY,
            fecha DATETIME DEFAULT NOW(),
            cliente_id INT,
            usuario_id INT,
            caja_sesion_id INT,
            subtotal_usd DECIMAL(10,2) DEFAULT 0,
            iva_usd DECIMAL(10,2) DEFAULT 0,
            total_usd DECIMAL(10,2) DEFAULT 0,
            tasa_cambio DECIMAL(10,4) DEFAULT 544.58,
            metodo_pago VARCHAR(50),
            referencia VARCHAR(100),
            extras JSON,
            moneda VARCHAR(10) DEFAULT 'Bs',
            monto_bs DECIMAL(10,2) DEFAULT 0,
            estado VARCHAR(20) DEFAULT 'activa',
            motivo_anulacion VARCHAR(255),
            empresa_id INT,
            porcentaje_servicio DECIMAL(5,2) DEFAULT 0,
            monto_servicio_usd DECIMAL(10,2) DEFAULT 0,
            tipo_credito BOOLEAN DEFAULT FALSE,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla facturas_detalle
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS facturas_detalle (
            id INT AUTO_INCREMENT PRIMARY KEY,
            factura_numero INT,
            producto_codigo VARCHAR(50),
            cantidad DECIMAL(10,2),
            precio_unitario DECIMAL(10,2),
            iva_unitario DECIMAL(10,2),
            descuento DECIMAL(10,2) DEFAULT 0,
            nota_descuento VARCHAR(255),
            subtotal_sin_iva DECIMAL(10,2),
            subtotal_con_iva DECIMAL(10,2),
            empresa_id INT,
            INDEX (factura_numero)
        )
    """)
    
    # Crear tabla facturas_pagos
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS facturas_pagos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            factura_numero INT,
            metodo_pago VARCHAR(50),
            monto_usd DECIMAL(10,2),
            monto_bs DECIMAL(10,2),
            referencia VARCHAR(100),
            nota VARCHAR(255),
            es_administracion BOOLEAN DEFAULT FALSE,
            INDEX (factura_numero)
        )
    """)
    
    # Crear tabla caja_sesion
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS caja_sesion (
            id INT AUTO_INCREMENT PRIMARY KEY,
            empresa_id INT,
            usuario_id INT,
            fecha_apertura DATETIME DEFAULT NOW(),
            fecha_cierre DATETIME,
            estado VARCHAR(20) DEFAULT 'abierta',
            total_usd DECIMAL(10,2) DEFAULT 0,
            total_bs DECIMAL(10,2) DEFAULT 0,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla historial_cierres
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_cierres (
            id INT AUTO_INCREMENT PRIMARY KEY,
            fecha_cierre DATETIME DEFAULT NOW(),
            usuario_id INT,
            total_usd DECIMAL(10,2),
            total_bs DECIMAL(10,2),
            datos JSON,
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla historial_inventario
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_inventario (
            id INT AUTO_INCREMENT PRIMARY KEY,
            fecha DATETIME DEFAULT NOW(),
            usuario VARCHAR(100),
            producto_codigo VARCHAR(50),
            producto_descripcion VARCHAR(255),
            tipo VARCHAR(50),
            cantidad_anterior DECIMAL(10,2),
            cantidad_nueva DECIMAL(10,2),
            nota TEXT,
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla alertas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alertas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            empresa_id INT,
            tipo VARCHAR(50),
            mensaje TEXT,
            usuario_id INT,
            fecha DATETIME DEFAULT NOW(),
            leida BOOLEAN DEFAULT FALSE,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla recetas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recetas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            codigo VARCHAR(50) UNIQUE NOT NULL,
            nombre VARCHAR(255) NOT NULL,
            descripcion TEXT,
            precio_venta DECIMAL(10,2),
            tiempo_preparacion INT DEFAULT 0,
            disponible BOOLEAN DEFAULT TRUE,
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla recetas_detalle
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recetas_detalle (
            id INT AUTO_INCREMENT PRIMARY KEY,
            receta_id INT,
            producto_codigo VARCHAR(50),
            cantidad_necesaria DECIMAL(10,2),
            empresa_id INT,
            INDEX (receta_id)
        )
    """)
    
    # Crear tabla kits
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kits (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(255) NOT NULL,
            producto_padre_codigo VARCHAR(50),
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla kit_detalle
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kit_detalle (
            id INT AUTO_INCREMENT PRIMARY KEY,
            kit_id INT,
            producto_hijo_codigo VARCHAR(50),
            cantidad_estimada DECIMAL(10,2),
            empresa_id INT,
            INDEX (kit_id)
        )
    """)
    
    # Crear tabla proveedores
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS proveedores (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(255) NOT NULL,
            rif VARCHAR(50),
            telefono VARCHAR(50),
            email VARCHAR(255),
            direccion TEXT,
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla ordenes_compra
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ordenes_compra (
            id INT AUTO_INCREMENT PRIMARY KEY,
            proveedor_id INT,
            fecha DATETIME DEFAULT NOW(),
            estado VARCHAR(50) DEFAULT 'recibida',
            total_usd DECIMAL(10,2),
            empresa_id INT,
            INDEX (empresa_id)
        )
    """)
    
    # Crear tabla ordenes_detalle
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ordenes_detalle (
            id INT AUTO_INCREMENT PRIMARY KEY,
            orden_id INT,
            producto_codigo VARCHAR(50),
            cantidad DECIMAL(10,2),
            precio_unitario DECIMAL(10,2),
            subtotal DECIMAL(10,2),
            INDEX (orden_id)
        )
    """)
    
    # Insertar cliente genérico si no existe
    cursor.execute("SELECT id FROM clientes WHERE rif = 'J-00000000-0'")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO clientes (rif, nombre, empresa_id) VALUES ('J-00000000-0', 'Cliente General', NULL)")
    
    # Insertar super_admin si no existe
    cursor.execute("SELECT id FROM usuarios WHERE username = 'super_admin'")
    if not cursor.fetchone():
        password = "koko.080502"
        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode()
        cursor.execute("""
            INSERT INTO usuarios (username, password_hash, email, role, empresa_id)
            VALUES (%s, %s, %s, %s, %s)
        """, ("super_admin", hashed, "admin@corebilling.com", "super_admin", None))
    
    conn.commit()
    cursor.close()
    conn.close()
    print("✅ Base de datos inicializada correctamente.")

if __name__ == '__main__':
    init_database()
