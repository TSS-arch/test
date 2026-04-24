from flask import Flask, request, jsonify
import os
import shutil
from werkzeug.utils import secure_filename
import sqlite3
from functools import wraps

app = Flask(__name__)

# Configuration
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size
app.config['UPLOAD_FOLDER'] = 'databases'
app.config['ALLOWED_EXTENSIONS'] = {'db', 'sqlite', 'sqlite3'}

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Database configuration
DB_PATH = os.path.join(app.config['UPLOAD_FOLDER'], 'uploaded_database.db')

def allowed_file(filename):
    """Check if the file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def validate_database(file_path):
    """Validate if the uploaded file is a valid SQLite database"""
    try:
        conn = sqlite3.connect(file_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        conn.close()
        return True, tables
    except Exception as e:
        return False, str(e)

# API endpoint to upload database
@app.route('/api/upload-database', methods=['POST'])
def upload_database():
    """
    Upload a database file. Replaces existing database if present.
    
    Expected form-data with key 'database'
    """
    try:
        # Check if file is present in request
        if 'database' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file provided. Use key "database" in form-data'
            }), 400
        
    


        file = request.files['database']
        
        # Check if filename is empty
        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400
        
        # Check if file extension is allowed
        if not allowed_file(file.filename):
            return jsonify({
                'success': False,
                'error': f'File type not allowed. Allowed types: {", ".join(app.config["ALLOWED_EXTENSIONS"])}'
            }), 400
        
        # Secure the filename and save temporarily
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f'temp_{filename}')
        file.save(temp_path)
        
        # Validate the database
        is_valid, result = validate_database(temp_path)
        
        if not is_valid:
            os.remove(temp_path)
            return jsonify({
                'success': False,
                'error': f'Invalid database file: {result}'
            }), 400
        
        # Check if database already exists
        database_exists = os.path.exists(DB_PATH)
        
        # Replace existing database
        if database_exists:
            # Create backup of existing database (optional)
            backup_path = DB_PATH + '.backup'
            shutil.copy2(DB_PATH, backup_path)
            
            # Remove existing database
            os.remove(DB_PATH)
        
        # Move the uploaded file to final location
        shutil.move(temp_path, DB_PATH)
        
        # Get database info
        db_size = os.path.getsize(DB_PATH)
        tables_count = len(result)
        
        response_data = {
            'success': True,
            'message': 'Database uploaded successfully',
            'data': {
                'filename': filename,
                'tables_count': tables_count,
                'database_size_bytes': db_size,
                'database_size_mb': round(db_size / (1024 * 1024), 2),
                'tables': [table[0] for table in result]
            }
        }
        
        if database_exists:
            response_data['message'] = 'Database replaced successfully'
            response_data['data']['previous_database_backup'] = 'backup created'
        
        return jsonify(response_data), 200
        
    except Exception as e:
        # Clean up temp file if it exists
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500

# API endpoint to check database status
@app.route('/api/database-status', methods=['GET'])
def database_status():
    """Check if a database is currently uploaded"""
    try:
        if os.path.exists(DB_PATH):
            db_size = os.path.getsize(DB_PATH)
            
            # Get database info
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            conn.close()
            
            return jsonify({
                'success': True,
                'database_exists': True,
                'data': {
                    'size_bytes': db_size,
                    'size_mb': round(db_size / (1024 * 1024), 2),
                    'tables_count': len(tables),
                    'tables': [table[0] for table in tables],
                    'path': DB_PATH
                }
            }), 200
        else:
            return jsonify({
                'success': True,
                'database_exists': False,
                'message': 'No database uploaded yet'
            }), 200
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Error checking database status: {str(e)}'
        }), 500

# API endpoint to delete database
@app.route('/api/delete-database', methods=['DELETE'])
def delete_database():
    """Delete the uploaded database"""
    try:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            return jsonify({
                'success': True,
                'message': 'Database deleted successfully'
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': 'No database found to delete'
            }), 404
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Error deleting database: {str(e)}'
        }), 500

# API endpoint to download the current database
@app.route('/api/download-database', methods=['GET'])
def download_database():
    """Download the current database file"""
    from flask import send_file
    
    try:
        if os.path.exists(DB_PATH):
            return send_file(
                DB_PATH,
                as_attachment=True,
                download_name='database.db',
                mimetype='application/x-sqlite3'
            )
        else:
            return jsonify({
                'success': False,
                'error': 'No database found to download'
            }), 404
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Error downloading database: {str(e)}'
        }), 500

# Health check endpoint
@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'success': True,
        'status': 'running',
        'message': 'Database upload API is operational'
    }), 200

# Error handlers
@app.errorhandler(413)
def too_large(e):
    return jsonify({
        'success': False,
        'error': 'File too large. Maximum size is 50MB'
    }), 413

@app.errorhandler(404)
def not_found(e):
    return jsonify({
        'success': False,
        'error': 'Endpoint not found'
    }), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({
        'success': False,
        'error': 'Internal server error'
    }), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
