@app.route('/api/test-db', methods=['GET'])
def test_db_connection():
    try:
        import socket
        host = os.environ.get('DB_HOST', 'localhost')
        port = int(os.environ.get('DB_PORT', 3306))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            # Puerto abierto, probamos conexión MySQL
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            conn.close()
            return jsonify({'status': 'OK', 'message': 'Conexión exitosa'}), 200
        else:
            return jsonify({'status': 'ERROR', 'message': f'No se puede conectar al host:port (código {result})'}), 500
    except Exception as e:
        return jsonify({'status': 'ERROR', 'message': str(e)}), 500
