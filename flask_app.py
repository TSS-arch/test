from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import shutil
import sqlite3
import logging
import hashlib
from datetime import datetime
from html import escape
from functools import wraps
import time
import json
from typing import Tuple, Optional, Dict, Any
import uuid

# ============================================================================
# Configuration
# ============================================================================

class Config:
    # Server
    SECRET_KEY = os.environ.get('SECRET_KEY', os.urandom(24).hex())
    
    # File upload
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'databases')
    ALLOWED_EXTENSIONS = {'db', 'sqlite', 'sqlite3'}
    
    # Database
    DB_PATH = os.path.join(UPLOAD_FOLDER, 'uploaded_database.db')
    DB_BACKUP_FOLDER = os.path.join(UPLOAD_FOLDER, 'backups')
    
    # Security
    MAX_BACKUPS = int(os.environ.get('MAX_BACKUPS', 5))
    BACKUP_RETENTION_DAYS = int(os.environ.get('BACKUP_RETENTION_DAYS', 30))
    
    # Rate limiting (requests per minute)
    RATE_LIMIT = int(os.environ.get('RATE_LIMIT', 60))
    
    # Logging
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_FILE = os.environ.get('LOG_FILE', 'app.log')
    
    # CORS
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '*').split(',')

# ============================================================================
# Initialize App
# ============================================================================

app = Flask(__name__)
app.config.from_object(Config)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Enable CORS
CORS(app, origins=Config.CORS_ORIGINS)

# ============================================================================
# Logging Setup
# ============================================================================

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(Config.LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# Rate Limiting
# ============================================================================

class RateLimiter:
    """Simple in-memory rate limiter"""
    def __init__(self):
        self.requests = {}
    
    def is_allowed(self, client_id: str) -> Tuple[bool, int]:
        now = time.time()
        window = 60  # 1 minute window
        
        if client_id not in self.requests:
            self.requests[client_id] = []
        
        # Clean old requests
        self.requests[client_id] = [t for t in self.requests[client_id] if now - t < window]
        
        if len(self.requests[client_id]) >= Config.RATE_LIMIT:
            return False, Config.RATE_LIMIT
        
        self.requests[client_id].append(now)
        return True, Config.RATE_LIMIT - len(self.requests[client_id])

rate_limiter = RateLimiter()

def rate_limit(f):
    """Rate limiting decorator"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_id = request.remote_addr
        allowed, remaining = rate_limiter.is_allowed(client_id)
        
        if not allowed:
            logger.warning(f"Rate limit exceeded for {client_id}")
            return jsonify({
                'success': False,
                'error': f'Rate limit exceeded. Max {Config.RATE_LIMIT} requests per minute'
            }), 429
        
        response = f(*args, **kwargs)
        
        # Add rate limit headers
        if isinstance(response, tuple):
            resp_obj = response[0]
            if hasattr(resp_obj, 'headers'):
                resp_obj.headers['X-RateLimit-Limit'] = str(Config.RATE_LIMIT)
                resp_obj.headers['X-RateLimit-Remaining'] = str(remaining)
            return response
        
        if hasattr(response, 'headers'):
            response.headers['X-RateLimit-Limit'] = str(Config.RATE_LIMIT)
            response.headers['X-RateLimit-Remaining'] = str(remaining)
        
        return response
    return decorated_function

# ============================================================================
# Utility Functions
# ============================================================================

def ensure_directories():
    """Create necessary directories if they don't exist"""
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(Config.DB_BACKUP_FOLDER, exist_ok=True)

def allowed_file(filename: str) -> bool:
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS

def validate_database(file_path: str) -> Tuple[bool, Any]:
    """Validate if file is a valid SQLite database"""
    try:
        conn = sqlite3.connect(file_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        
        # Check if any table has 'id' column
        has_id_column = False
        for (table_name,) in tables:
            cursor.execute(f"PRAGMA table_info('{table_name}')")
            columns = [col[1] for col in cursor.fetchall()]
            if 'id' in columns:
                has_id_column = True
                break
        
        conn.close()
        return True, {'tables': tables, 'has_id_column': has_id_column}
    except Exception as e:
        logger.error(f"Database validation error: {str(e)}")
        return False, str(e)

def create_backup() -> Optional[str]:
    """Create a backup of the current database"""
    if not os.path.exists(Config.DB_PATH):
        return None
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f"backup_{timestamp}_{uuid.uuid4().hex[:8]}.db"
    backup_path = os.path.join(Config.DB_BACKUP_FOLDER, backup_name)
    
    try:
        shutil.copy2(Config.DB_PATH, backup_path)
        
        # Clean old backups
        cleanup_old_backups()
        
        logger.info(f"Backup created: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"Backup creation failed: {str(e)}")
        return None

def cleanup_old_backups():
    """Remove old backups based on retention policy"""
    try:
        backups = []
        for file in os.listdir(Config.DB_BACKUP_FOLDER):
            if file.startswith('backup_') and file.endswith('.db'):
                file_path = os.path.join(Config.DB_BACKUP_FOLDER, file)
                backups.append((file_path, os.path.getmtime(file_path)))
        
        # Sort by modification time (oldest first)
        backups.sort(key=lambda x: x[1])
        
        # Remove exceeding max backups
        if len(backups) > Config.MAX_BACKUPS:
            for i in range(len(backups) - Config.MAX_BACKUPS):
                os.remove(backups[i][0])
                logger.info(f"Removed old backup: {backups[i][0]}")
        
        # Remove backups older than retention days
        cutoff_time = time.time() - (Config.BACKUP_RETENTION_DAYS * 86400)
        for backup_path, mtime in backups:
            if mtime < cutoff_time:
                os.remove(backup_path)
                logger.info(f"Removed expired backup: {backup_path}")
                
    except Exception as e:
        logger.error(f"Backup cleanup failed: {str(e)}")

def get_file_hash(file_path: str) -> str:
    """Calculate SHA256 hash of file"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

# ============================================================================
# Report Generation
# ============================================================================

def generate_single_record_report(db_path: str, table_name: str, record_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Generate HTML report for a single record by ID"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if table has 'id' column
        cursor.execute(f"PRAGMA table_info('{table_name}')")
        columns_info = cursor.fetchall()
        columns = [col[1] for col in columns_info]
        
        if 'id' not in columns:
            conn.close()
            return None, "Table does not have an 'id' column"
        
        # Fetch the record
        cursor.execute(f"SELECT * FROM '{table_name}' WHERE id = ?", (record_id,))
        record = cursor.fetchone()
        
        if not record:
            conn.close()
            return None, f"No record found with ID {record_id}"
        
        record_data = dict(zip(columns, record))
        
        def get_value(field_name: str):
            for key, value in record_data.items():
                if key.lower() == field_name.lower():
                    return value if value is not None else ''
            return ''
        
        values = {
            'NAME': get_value('name'),
            'BILL NO': get_value('bill_no'),
            'SAMPLE': get_value('sample'),
            'WEIGHT': get_value('weight'),
            'FINE': get_value('fine'),
            'GOLD PURITY': get_value('gold_purity'),
            'KARAT': get_value('karat'),
            'SILVER': get_value('silver'),
            'COPPER': get_value('copper'),
            'ZINC': get_value('zinc'),
            'NICKEL': get_value('nickel'),
            'CADMIUM': get_value('cadmium'),
            'IRIDIUM': get_value('iridium'),
            'RHODIUM': get_value('rhodium'),
            'RUTHENIUM': get_value('ruthenium'),
            'INDIUM': get_value('indium'),
            'TIN': get_value('tin'),
            'PALLADIUM': get_value('palladium'),
            'OSMIUM': get_value('osmium'),
            'CHROMIUM': get_value('chromium'),
            'LEAD': get_value('lead'),
            'COBALT': get_value('cobalt'),
            'BISMUTH': get_value('bismuth'),
            'PLATINUM': get_value('platinum')
        }
        
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Metal Analysis Report - ID {record_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        .report-container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }}
        .report-header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
        }}
        .title {{
            font-size: 28px;
            font-weight: bold;
            margin-bottom: 10px;
        }}
        .meta-info {{
            font-size: 14px;
            opacity: 0.9;
        }}
        .metal-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        .metal-table td, .metal-table th {{
            border: 1px solid #ddd;
            padding: 12px;
        }}
        .metal-table td:first-child {{
            background-color: #f8f9fa;
            font-weight: bold;
            width: 150px;
        }}
        .metal-table td:not(:first-child) {{
            text-align: right;
        }}
        .footer {{
            background: #f8f9fa;
            padding: 20px;
            text-align: center;
            font-size: 12px;
            color: #666;
        }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .report-container {{ box-shadow: none; }}
            .report-header {{ background: #667eea; }}
        }}
    </style>
</head>
<body>
<div class="report-container">
    <div class="report-header">
        <div class="title">🔬 Metal Analysis Report</div>
        <div class="meta-info">
            Record ID: {escape(str(record_data.get('id', 'N/A')))} | 
            Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |
            Table: {escape(table_name)}
        </div>
    </div>
    
    <table class="metal-table">
        <tr><td>NAME</td><td>{escape(str(values.get('NAME', '')))}</td>
            <td>BILL NO</td><td>{escape(str(values.get('BILL NO', '')))}</td>
            <td>SAMPLE</td><td>{escape(str(values.get('SAMPLE', '')))}</td>
            <td>WEIGHT</td><td>{escape(str(values.get('WEIGHT', '')))}</td>
        </tr>
        <tr><td>FINE</td><td>{escape(str(values.get('FINE', '')))}</td>
            <td>GOLD PURITY</td><td>{escape(str(values.get('GOLD PURITY', '')))}</td>
            <td>KARAT</td><td>{escape(str(values.get('KARAT', '')))}</td>
            <td>SILVER</td><td>{escape(str(values.get('SILVER', '')))}</td>
        </tr>
        <tr><td>COPPER</td><td>{escape(str(values.get('COPPER', '')))}</td>
            <td>ZINC</td><td>{escape(str(values.get('ZINC', '')))}</td>
            <td>NICKEL</td><td>{escape(str(values.get('NICKEL', '')))}</td>
            <td>CADMIUM</td><td>{escape(str(values.get('CADMIUM', '')))}</td>
        </tr>
        <tr><td>IRIDIUM</td><td>{escape(str(values.get('IRIDIUM', '')))}</td>
            <td>RHODIUM</td><td>{escape(str(values.get('RHODIUM', '')))}</td>
            <td>RUTHENIUM</td><td>{escape(str(values.get('RUTHENIUM', '')))}</td>
            <td>INDIUM</td><td>{escape(str(values.get('INDIUM', '')))}</td>
        </tr>
        <tr><td>TIN</td><td>{escape(str(values.get('TIN', '')))}</td>
            <td>PALLADIUM</td><td>{escape(str(values.get('PALLADIUM', '')))}</td>
            <td>OSMIUM</td><td>{escape(str(values.get('OSMIUM', '')))}</td>
            <td>CHROMIUM</td><td>{escape(str(values.get('CHROMIUM', '')))}</td>
        </tr>
        <tr><td>LEAD</td><td>{escape(str(values.get('LEAD', '')))}</td>
            <td>COBALT</td><td>{escape(str(values.get('COBALT', '')))}</td>
            <td>BISMUTH</td><td>{escape(str(values.get('BISMUTH', '')))}</td>
            <td>PLATINUM</td><td>{escape(str(values.get('PLATINUM', '')))}</td>
        </tr>
    </table>
    
    <div class="footer">
        Generated by Metal Analysis System | Report ID: {uuid.uuid4().hex[:8].upper()}
    </div>
</div>
</body>
</html>"""
        
        conn.close()
        return html, None
        
    except Exception as e:
        logger.error(f"Report generation error: {str(e)}")
        return None, str(e)

# ============================================================================
# API Endpoints
# ============================================================================

@app.route('/api/upload-database', methods=['POST'])
@rate_limit
def upload_database():
    """Upload a database file. Replaces existing database if present."""
    try:
        if 'database' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file provided. Use key "database" in form-data'
            }), 400
        
        file = request.files['database']
        
        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400
        
        if not allowed_file(file.filename):
            return jsonify({
                'success': False,
                'error': f'File type not allowed. Allowed types: {", ".join(Config.ALLOWED_EXTENSIONS)}'
            }), 400
        
        filename = secure_filename(file.filename)
        temp_path = os.path.join(Config.UPLOAD_FOLDER, f'temp_{uuid.uuid4().hex}_{filename}')
        file.save(temp_path)
        
        # Validate database
        is_valid, result = validate_database(temp_path)
        
        if not is_valid:
            os.remove(temp_path)
            logger.warning(f"Invalid database upload attempt: {filename}")
            return jsonify({
                'success': False,
                'error': f'Invalid database file: {result}'
            }), 400
        
        # Create backup before replacing
        database_exists = os.path.exists(Config.DB_PATH)
        backup_path = None
        
        if database_exists:
            backup_path = create_backup()
            os.remove(Config.DB_PATH)
        
        # Move new database
        shutil.move(temp_path, Config.DB_PATH)
        
        # Calculate file hash
        file_hash = get_file_hash(Config.DB_PATH)
        
        logger.info(f"Database uploaded successfully: {filename} (Hash: {file_hash[:8]}...)")
        
        response_data = {
            'success': True,
            'message': 'Database replaced successfully' if database_exists else 'Database uploaded successfully',
            'data': {
                'filename': filename,
                'tables_count': len(result['tables']),
                'has_id_column': result['has_id_column'],
                'database_size_bytes': os.path.getsize(Config.DB_PATH),
                'database_size_mb': round(os.path.getsize(Config.DB_PATH) / (1024 * 1024), 2),
                'file_hash': file_hash[:16],
                'backup_created': backup_path is not None
            }
        }
        
        if backup_path:
            response_data['data']['backup_path'] = backup_path
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Upload error: {str(e)}", exc_info=True)
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500

@app.route('/api/report/<int:record_id>', methods=['GET'])
@rate_limit
def get_report_by_id(record_id):
    """Generate HTML report for a specific record ID"""
    try:
        if not os.path.exists(Config.DB_PATH):
            return render_template_string("""
                <div style="text-align:center;padding:50px;font-family:Arial">
                    <h1>❌ No Database Found</h1>
                    <p>Please upload a database using /api/upload-database</p>
                </div>
            """), 404
        
        table_name = request.args.get('table')
        
        conn = sqlite3.connect(Config.DB_PATH)
        cursor = conn.cursor()
        
        if not table_name:
            # Find table with 'id' column
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """)
            tables = cursor.fetchall()
            
            for (tbl,) in tables:
                cursor.execute(f"PRAGMA table_info('{tbl}')")
                columns = [col[1] for col in cursor.fetchall()]
                if 'id' in columns:
                    table_name = tbl
                    break
            
            if not table_name:
                conn.close()
                return render_template_string("""
                    <div style="text-align:center;padding:50px;font-family:Arial">
                        <h1>❌ No Suitable Table Found</h1>
                        <p>No table with 'id' column found in the database</p>
                    </div>
                """), 404
        
        html_content, error = generate_single_record_report(Config.DB_PATH, table_name, record_id)
        
        conn.close()
        
        if error:
            return render_template_string(f"""
                <div style="text-align:center;padding:50px;font-family:Arial">
                    <h1>❌ Error</h1>
                    <p>{error}</p>
                    <a href="/api/tables">View Available Tables</a>
                </div>
            """), 404
        
        logger.info(f"Report generated for ID {record_id} from table {table_name}")
        return html_content
        
    except Exception as e:
        logger.error(f"Report generation error: {str(e)}", exc_info=True)
        return render_template_string(f"""
            <div style="text-align:center;padding:50px;font-family:Arial">
                <h1>❌ Server Error</h1>
                <p>{str(e)}</p>
            </div>
        """), 500

@app.route('/api/tables', methods=['GET'])
@rate_limit
def list_tables():
    """Get list of all tables and available record IDs"""
    try:
        if not os.path.exists(Config.DB_PATH):
            return jsonify({
                'success': False,
                'error': 'No database uploaded yet'
            }), 404
        
        conn = sqlite3.connect(Config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """)
        tables = cursor.fetchall()
        
        result = {}
        for (table_name,) in tables:
            cursor.execute(f"PRAGMA table_info('{table_name}')")
            columns = [col[1] for col in cursor.fetchall()]
            
            if 'id' in columns:
                cursor.execute(f"SELECT COUNT(*) FROM '{table_name}'")
                count = cursor.fetchone()[0]
                
                cursor.execute(f"SELECT id FROM '{table_name}' ORDER BY id LIMIT 100")
                ids = [row[0] for row in cursor.fetchall()]
                
                result[table_name] = {
                    'has_id_column': True,
                    'record_count': count,
                    'available_ids': ids,
                    'total_ids': count,
                    'id_range': {'min': min(ids) if ids else None, 'max': max(ids) if ids else None}
                }
            else:
                cursor.execute(f"SELECT COUNT(*) FROM '{table_name}'")
                count = cursor.fetchone()[0]
                result[table_name] = {
                    'has_id_column': False,
                    'record_count': count,
                    'message': 'Table does not have an ID column'
                }
        
        conn.close()
        
        return jsonify({
            'success': True,
            'database_path': Config.DB_PATH,
            'database_exists': True,
            'tables': result,
            'total_tables': len(tables)
        }), 200
        
    except Exception as e:
        logger.error(f"List tables error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/database-status', methods=['GET'])
@rate_limit
def database_status():
    """Check if a database is currently uploaded"""
    try:
        if os.path.exists(Config.DB_PATH):
            db_size = os.path.getsize(Config.DB_PATH)
            file_hash = get_file_hash(Config.DB_PATH)
            
            conn = sqlite3.connect(Config.DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            conn.close()
            
            # List backups
            backups = []
            for file in os.listdir(Config.DB_BACKUP_FOLDER):
                if file.startswith('backup_') and file.endswith('.db'):
                    backup_path = os.path.join(Config.DB_BACKUP_FOLDER, file)
                    backups.append({
                        'name': file,
                        'size_mb': round(os.path.getsize(backup_path) / (1024 * 1024), 2),
                        'created': datetime.fromtimestamp(os.path.getctime(backup_path)).isoformat()
                    })
            
            return jsonify({
                'success': True,
                'database_exists': True,
                'data': {
                    'size_bytes': db_size,
                    'size_mb': round(db_size / (1024 * 1024), 2),
                    'tables_count': len(tables),
                    'tables': [table[0] for table in tables],
                    'path': Config.DB_PATH,
                    'file_hash': file_hash[:16],
                    'last_modified': datetime.fromtimestamp(os.path.getmtime(Config.DB_PATH)).isoformat(),
                    'backups_count': len(backups),
                    'backups': backups[:5]  # Last 5 backups
                }
            }), 200
        else:
            return jsonify({
                'success': True,
                'database_exists': False,
                'message': 'No database uploaded yet'
            }), 200
            
    except Exception as e:
        logger.error(f"Database status error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Error checking database status: {str(e)}'
        }), 500

@app.route('/api/delete-database', methods=['DELETE'])
@rate_limit
def delete_database():
    """Delete the uploaded database"""
    try:
        if not os.path.exists(Config.DB_PATH):
            return jsonify({
                'success': False,
                'error': 'No database found to delete'
            }), 404
        
        # Create final backup before deletion
        backup_path = create_backup()
        
        os.remove(Config.DB_PATH)
        
        logger.info(f"Database deleted. Backup created at {backup_path}")
        
        return jsonify({
            'success': True,
            'message': 'Database deleted successfully',
            'backup_created': backup_path is not None,
            'backup_path': backup_path
        }), 200
        
    except Exception as e:
        logger.error(f"Delete database error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Error deleting database: {str(e)}'
        }), 500

@app.route('/api/download-database', methods=['GET'])
@rate_limit
def download_database():
    """Download the current database file"""
    try:
        if not os.path.exists(Config.DB_PATH):
            return jsonify({
                'success': False,
                'error': 'No database found to download'
            }), 404
        
        logger.info(f"Database downloaded by {request.remote_addr}")
        
        return send_file(
            Config.DB_PATH,
            as_attachment=True,
            download_name=f'metal_analysis_{datetime.now().strftime("%Y%m%d")}.db',
            mimetype='application/x-sqlite3'
        )
        
    except Exception as e:
        logger.error(f"Download error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Error downloading database: {str(e)}'
        }), 500

@app.route('/api/backups', methods=['GET'])
@rate_limit
def list_backups():
    """List all available backups"""
    try:
        backups = []
        for file in os.listdir(Config.DB_BACKUP_FOLDER):
            if file.startswith('backup_') and file.endswith('.db'):
                backup_path = os.path.join(Config.DB_BACKUP_FOLDER, file)
                backups.append({
                    'name': file,
                    'size_bytes': os.path.getsize(backup_path),
                    'size_mb': round(os.path.getsize(backup_path) / (1024 * 1024), 2),
                    'created': datetime.fromtimestamp(os.path.getctime(backup_path)).isoformat(),
                    'path': backup_path
                })
        
        backups.sort(key=lambda x: x['created'], reverse=True)
        
        return jsonify({
            'success': True,
            'backups': backups,
            'total_backups': len(backups),
            'max_backups': Config.MAX_BACKUPS,
            'retention_days': Config.BACKUP_RETENTION_DAYS
        }), 200
        
    except Exception as e:
        logger.error(f"List backups error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/restore-backup/<backup_name>', methods=['POST'])
@rate_limit
def restore_backup(backup_name):
    """Restore a specific backup"""
    try:
        backup_path = os.path.join(Config.DB_BACKUP_FOLDER, backup_name)
        
        if not os.path.exists(backup_path):
            return jsonify({
                'success': False,
                'error': 'Backup not found'
            }), 404
        
        # Create backup of current database before restore
        if os.path.exists(Config.DB_PATH):
            create_backup()
            os.remove(Config.DB_PATH)
        
        # Restore backup
        shutil.copy2(backup_path, Config.DB_PATH)
        
        logger.info(f"Database restored from backup: {backup_name}")
        
        return jsonify({
            'success': True,
            'message': f'Database restored successfully from {backup_name}'
        }), 200
        
    except Exception as e:
        logger.error(f"Restore backup error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'success': True,
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database_loaded': os.path.exists(Config.DB_PATH),
        'version': '1.0.0'
    }), 200

@app.route('/api/metrics', methods=['GET'])
@rate_limit
def get_metrics():
    """Get application metrics"""
    try:
        db_exists = os.path.exists(Config.DB_PATH)
        db_size = os.path.getsize(Config.DB_PATH) if db_exists else 0
        
        backups_count = len([f for f in os.listdir(Config.DB_BACKUP_FOLDER) 
                           if f.startswith('backup_') and f.endswith('.db')])
        
        return jsonify({
            'success': True,
            'metrics': {
                'database_size_mb': round(db_size / (1024 * 1024), 2) if db_exists else 0,
                'database_exists': db_exists,
                'backups_count': backups_count,
                'uptime_seconds': time.time() - app.start_time if hasattr(app, 'start_time') else 0,
                'max_backups': Config.MAX_BACKUPS,
                'backup_retention_days': Config.BACKUP_RETENTION_DAYS
            }
        }), 200
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# Error Handlers
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({
        'success': False,
        'error': 'Endpoint not found'
    }), 404

@app.errorhandler(413)
def too_large(e):
    return jsonify({
        'success': False,
        'error': f'File too large. Maximum size is {Config.MAX_CONTENT_LENGTH // (1024*1024)}MB'
    }), 413

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {str(e)}")
    return jsonify({
        'success': False,
        'error': 'Internal server error'
    }), 500

# ============================================================================
# Home Page
# ============================================================================

@app.route('/', methods=['GET'])
def home():
    """API documentation home page"""
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Metal Analysis API - Production Ready</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
        }
        .header h1 { font-size: 32px; margin-bottom: 10px; }
        .header p { opacity: 0.9; }
        .content { padding: 40px; }
        .section { margin-bottom: 30px; }
        .section h2 {
            color: #667eea;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e0e0e0;
        }
        .endpoint {
            background: #f8f9fa;
            border-left: 4px solid #667eea;
            padding: 15px;
            margin: 15px 0;
            border-radius: 4px;
        }
        .method {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 12px;
            margin-right: 10px;
        }
        .post { background: #4CAF50; color: white; }
        .get { background: #2196F3; color: white; }
        .delete { background: #f44336; color: white; }
        .url { font-family: monospace; font-size: 14px; }
        pre {
            background: #2d2d2d;
            color: #f8f8f2;
            padding: 15px;
            border-radius: 6px;
            overflow-x: auto;
            margin-top: 10px;
        }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            background: #e0e0e0;
            margin-left: 10px;
        }
        .status {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #4CAF50;
            margin-right: 8px;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        @media (max-width: 768px) {
            .content { padding: 20px; }
            .header { padding: 20px; }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🔬 Metal Analysis API</h1>
        <p>Production-Ready REST API for Metal Analysis Database Management</p>
        <div style="margin-top: 15px;">
            <span class="status"></span> API Status: Operational
            <span class="badge">v1.0.0</span>
        </div>
    </div>
    
    <div class="content">
        <div class="section">
            <h2>📤 Database Management</h2>
            <div class="endpoint">
                <span class="method post">POST</span>
                <span class="url">/api/upload-database</span>
                <p style="margin-top: 10px;">Upload SQLite database file (replaces existing with backup)</p>
            </div>
            <div class="endpoint">
                <span class="method get">GET</span>
                <span class="url">/api/download-database</span>
                <p style="margin-top: 10px;">Download current database</p>
            </div>
            <div class="endpoint">
                <span class="method delete">DELETE</span>
                <span class="url">/api/delete-database</span>
                <p style="margin-top: 10px;">Delete current database (creates backup)</p>
            </div>
            <div class="endpoint">
                <span class="method get">GET</span>
                <span class="url">/api/database-status</span>
                <p style="margin-top: 10px;">Check database status and metadata</p>
            </div>
        </div>
        
        <div class="section">
            <h2>📊 Report Generation</h2>
            <div class="endpoint">
                <span class="method get">GET</span>
                <span class="url">/api/report/&lt;record_id&gt;</span>
                <p style="margin-top: 10px;">Generate HTML report for specific record ID</p>
                <pre>GET /api/report/123</pre>
                <pre>GET /api/report/123?table=table_name</pre>
            </div>
            <div class="endpoint">
                <span class="method get">GET</span>
                <span class="url">/api/tables</span>
                <p style="margin-top: 10px;">List all tables with available record IDs</p>
            </div>
        </div>
        
        <div class="section">
            <h2>💾 Backup & Recovery</h2>
            <div class="endpoint">
                <span class="method get">GET</span>
                <span class="url">/api/backups</span>
                <p style="margin-top: 10px;">List all available backups</p>
            </div>
            <div class="endpoint">
                <span class="method post">POST</span>
                <span class="url">/api/restore-backup/&lt;backup_name&gt;</span>
                <p style="margin-top: 10px;">Restore database from backup</p>
            </div>
        </div>
        
        <div class="section">
            <h2>🔧 System</h2>
            <div class="endpoint">
                <span class="method get">GET</span>
                <span class="url">/api/health</span>
                <p style="margin-top: 10px;">Health check endpoint</p>
            </div>
            <div class="endpoint">
                <span class="method get">GET</span>
                <span class="url">/api/metrics</span>
                <p style="margin-top: 10px;">Get application metrics</p>
            </div>
        </div>
        
        <div class="section">
            <h2>🚀 Quick Start</h2>
            <h3>Python Example:</h3>
            <pre>
import requests

# Upload database
with open('metal_data.db', 'rb') as f:
    response = requests.post(
        'http://localhost:5000/api/upload-database',
        files={'database': f}
    )
    print(response.json())

# Generate report
response = requests.get('http://localhost:5000/api/report/1')
with open('report.html', 'w') as f:
    f.write(response.text)

# Check database status
response = requests.get('http://localhost:5000/api/database-status')
print(response.json())

# List all tables
response = requests.get('http://localhost:5000/api/tables')
print(response.json())</pre>
            
            <h3>cURL Examples:</h3>
            <pre>
# Upload database
curl -X POST http://localhost:5000/api/upload-database \
  -F "database=@metal_data.db"

# Download database
curl -X GET http://localhost:5000/api/download-database \
  --output downloaded.db

# Generate report
curl http://localhost:5000/api/report/1 > report.html

# List backups
curl http://localhost:5000/api/backups</pre>
        </div>
        
        <div class="section">
            <h2>⚙️ Environment Variables</h2>
            <pre>
SECRET_KEY=your_secret_key_here
UPLOAD_FOLDER=databases
MAX_BACKUPS=5
BACKUP_RETENTION_DAYS=30
RATE_LIMIT=60
LOG_LEVEL=INFO
CORS_ORIGINS=https://yourdomain.com,https://app.yourdomain.com</pre>
        </div>
    </div>
</div>
</body>
</html>
    """)

# ============================================================================
# Application Startup
# ============================================================================

if __name__ == '__main__':
    ensure_directories()
    app.start_time = time.time()
    
    logger.info("=" * 60)
    logger.info("🔬 Metal Analysis API - Production Version")
    logger.info("=" * 60)
    logger.info(f"Upload folder: {Config.UPLOAD_FOLDER}")
    logger.info(f"Backup folder: {Config.DB_BACKUP_FOLDER}")
    logger.info(f"Max backups: {Config.MAX_BACKUPS}")
    logger.info(f"Backup retention: {Config.BACKUP_RETENTION_DAYS} days")
    logger.info(f"Rate limit: {Config.RATE_LIMIT} requests/minute")
    logger.info(f"Log level: {Config.LOG_LEVEL}")
    logger.info("=" * 60)
    
    # Use Gunicorn for production
    # Command: gunicorn -w 4 -b 0.0.0.0:5000 app:app
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
