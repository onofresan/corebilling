import os
import mysql.connector

def get_db_connection():
    # Si SKIP_DB_INIT está activado, saltamos la conexión
    if os.environ.get('SKIP_DB_INIT', 'false').lower() == 'true':
        print("⚠️ SKIP_DB_INIT activado - Saltando conexión a BD durante build")
        return None
    
    try:
        return mysql.connector.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            user=os.environ.get('DB_USER', 'root'),
            password=os.environ.get('DB_PASSWORD', ''),
            database=os.environ.get('DB_NAME', 'facturacion')
        )
    except Exception as e:
        print(f"❌ Error conectando a BD: {e}")
        print("⚠️ Continuando con el build...")
        return None

def init_database():
    print("🚀 Inicializando base de datos...")
    
    conn = get_db_connection()
    
    if conn is None:
        print("✅ Build en Render - Saltando inicialización de BD (SKIP_DB_INIT=true)")
        return
    
    cursor = conn.cursor()
    
    # Verificar si la tabla empresas existe
    cursor.execute("SHOW TABLES LIKE 'empresas'")
    if cursor.fetchone():
        print("✅ Base de datos ya inicializada")
        cursor.close()
        conn.close()
        return
    
    print("📦 Creando tablas base...")
    
    # Crear tabla empresas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS empresas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(100) NOT NULL,
            rif VARCHAR(20) NOT NULL UNIQUE,
            correo VARCHAR(100),
            telefono VARCHAR(20),
            direccion TEXT,
            tasa_cambio DECIMAL(10,2) DEFAULT 544.58,
            logo_url VARCHAR(255),
            activa BOOLEAN DEFAULT TRUE,
            ultimo_reporte_z INT DEFAULT 0,
            permite_reiniciar_historial BOOLEAN DEFAULT FALSE,
            fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Crear tabla usuarios
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(50) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            email VARCHAR(100),
            telefono VARCHAR(20),
            role VARCHAR(20) DEFAULT 'cajero',
            empresa_id INT,
            activo BOOLEAN DEFAULT TRUE,
            fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id) ON DELETE CASCADE
        )
    """)
    
    # Crear cliente general
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            rif VARCHAR(20) NOT NULL,
            nombre VARCHAR(100) NOT NULL,
            telefono VARCHAR(20),
            direccion TEXT,
            email VARCHAR(100),
            empresa_id INT,
            fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id) ON DELETE CASCADE
        )
    """)
    
    conn.commit()
    cursor.close()
    conn.close()
    
    print("✅ Base de datos inicializada correctamente")

if __name__ == "__main__":
    init_database()
