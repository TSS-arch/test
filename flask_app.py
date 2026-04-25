from flask import Flask, request, redirect, url_for, flash
import sqlite3
import os
import uuid
import time
import traceback
from html import escape
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {'.db', '.sqlite', '.sqlite3'}

# ================= DB CONNECTION =================
def get_db_connection(db_path):
    return sqlite3.connect(db_path, timeout=10, check_same_thread=False)

# ================= FILE VALIDATION =================
def allowed_file(filename):
    return any(filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS)

# ================= CLEANUP OLD FILES =================
def cleanup_uploads(folder, max_age=3600):
    now = time.time()
    for f in os.listdir(folder):
        path = os.path.join(folder, f)
        if os.path.isfile(path):
            if now - os.path.getmtime(path) > max_age:
                os.remove(path)

# ================= ERROR HANDLER =================
@app.errorhandler(Exception)
def handle_exception(e):
    print(traceback.format_exc())
    return f"""
    <h3>❌ Internal Server Error</h3>
    <pre>{escape(str(e))}</pre>
    """, 500

# ================= REPORT GENERATION =================
def generate_single_record_report(db_path, table_name, record_id):
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute(f'PRAGMA table_info("{table_name}")')
        columns_info = cursor.fetchall()
        columns = [col[1] for col in columns_info]

        if 'Id' not in columns:
            return None

        cursor.execute(f'SELECT * FROM "{table_name}" WHERE Id = ?', (record_id,))
        record = cursor.fetchone()

        if not record:
            return None

        record_data = dict(zip(columns, record))

    # Limit columns to avoid heavy rendering
    MAX_COLUMNS = 50

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Report {record_id}</title>
        <style>
            body {{
                font-family: Arial;
                background: #f4f6f9;
                padding: 20px;
            }}
            .card {{
                background: white;
                padding: 20px;
                border-radius: 10px;
                box-shadow: 0 5px 20px rgba(0,0,0,0.1);
                max-width: 900px;
                margin: auto;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            td {{
                padding: 10px;
                border-bottom: 1px solid #ddd;
            }}
            td:first-child {{
                font-weight: bold;
                width: 30%;
                background: #f9f9f9;
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>📊 Record Report (ID: {record_id})</h2>
            <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <table>
    """

    for i, (col, val) in enumerate(record_data.items()):
        if i >= MAX_COLUMNS:
            break
        html += f"""
        <tr>
            <td>{escape(col)}</td>
            <td>{escape(str(val))}</td>
        </tr>
        """

    html += """
            </table>
        </div>
    </body>
    </html>
    """

    return html

# ================= ROUTES =================

@app.route('/')
def index():
    return '''
    <html>
    <body style="font-family: Arial; text-align:center; padding:50px;">
        <h1>🔬 Metal Report System</h1>
        <form action="/upload" method="post" enctype="multipart/form-data">
            <input type="file" name="database" required>
            <br><br>
            <button type="submit">Upload</button>
        </form>
    </body>
    </html>
    '''

@app.route('/upload', methods=['POST'])
def upload_file():
    cleanup_uploads(app.config['UPLOAD_FOLDER'])

    if 'database' not in request.files:
        return "No file uploaded"

    file = request.files['database']

    if file.filename == '':
        return "No file selected"

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        db_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(db_path)

        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = cursor.fetchall()

        if not tables:
            os.remove(db_path)
            return "❌ No tables found"

        table_links = ''.join(
            [f'<li><a href="/browse/{unique_filename}/{t[0]}">{t[0]}</a></li>' for t in tables]
        )

        return f"""
        <h2>Select Table</h2>
        <ul>{table_links}</ul>
        """

    return "Invalid file"

@app.route('/browse/<filename>/<table_name>')
def browse_table(filename, table_name):
    db_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    if not os.path.exists(db_path):
        return "File not found"

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute(f'PRAGMA table_info("{table_name}")')
        columns = [c[1] for c in cursor.fetchall()]

        cursor.execute(f'SELECT * FROM "{table_name}" LIMIT 50')
        records = cursor.fetchall()

    if 'Id' not in columns:
        return "Table has no Id column"

    rows = ""
    for r in records:
        rid = r[columns.index('Id')]
        rows += f"<tr><td>{rid}</td><td><a href='/report/{filename}/{table_name}/{rid}'>View</a></td></tr>"

    return f"""
    <h2>{table_name}</h2>
    <table border=1 cellpadding=10>
        <tr><th>ID</th><th>Action</th></tr>
        {rows}
    </table>
    """
@app.route('/api/upload', methods=['POST'])
def api_upload():
    try:
        if 'database' not in request.files:
            return {"error": "No database file"}, 400

        file = request.files['database']

        if file.filename == '':
            return {"error": "No file selected"}, 400

        if not allowed_file(file.filename):
            return {"error": "Invalid file type"}, 400

        cleanup_uploads(app.config['UPLOAD_FOLDER'])

        filename = secure_filename(file.filename)

        # 🔥 Use filename as identity (important change)
        file_id = filename.replace('.', '_')   # stable id

        stored_name = f"{file_id}.db"
        db_path = os.path.join(app.config['UPLOAD_FOLDER'], stored_name)

        # 🔴 DELETE OLD FILE IF EXISTS
        if os.path.exists(db_path):
            os.remove(db_path)

        # Save new file
        file.save(db_path)

        # Read tables
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            tables = [t[0] for t in cursor.fetchall()]

        return {
            "file_id": file_id,
            "tables": tables,
            "message": "Uploaded (replaced if existed)"
        }

    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/report/<filename>/<table_name>/<int:record_id>')
def view_report(filename, table_name, record_id):
    db_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    if not os.path.exists(db_path):
        return "File not found"

    html = generate_single_record_report(db_path, table_name, record_id)

    if html:
        return html
    return "Record not found"

# ================= RUN =================
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
