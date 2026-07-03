from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import os
import json
import urllib.parse
import urllib.request

app = Flask(__name__)
CORS(app)
DB_PATH = '/data/menu.db'
ADMIN_PASSWORD = os.getenv('CHEF_PASSWORD')

def init_db():
    if not os.path.exists('/data'):
        os.makedirs('/data')
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS choices (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, choice TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('CREATE TABLE IF NOT EXISTS menu_items (item TEXT PRIMARY KEY)')
        
        initial_items = ['Burger', 'Hot Dog', 'Pasta', 'Charcuterie', 'Jacket Potatoes', 'Ramen', 'Sushi']
        for item in initial_items:
            conn.execute("INSERT OR IGNORE INTO menu_items (item) VALUES (?)", (item,))
        conn.commit()

@app.route('/submit', methods=['POST'])
def submit():
    data = request.json
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO choices (day, choice) VALUES (?, ?)", (data['day'], data['choice']))
        conn.execute("INSERT OR IGNORE INTO menu_items (item) VALUES (?)", (data['choice'],))
    return jsonify({"status": "success"})

@app.route('/menu-options', methods=['GET'])
def get_options():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT item FROM menu_items ORDER BY item ASC")
        return jsonify([r[0] for r in cursor.fetchall()])

@app.route('/history', methods=['GET'])
def history():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT day, choice FROM choices ORDER BY id DESC LIMIT 20")
        return jsonify([{"day": r[0], "choice": r[1]} for r in cursor.fetchall()])

@app.route('/purge', methods=['POST'])
def purge():
    data = request.json
    if data.get('password') == ADMIN_PASSWORD:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM choices")
            conn.execute("DELETE FROM menu_items")
            conn.commit()
        init_db() # Restore the hardcoded defaults
        return jsonify({"status": "cleared"}), 200
    return jsonify({"status": "unauthorized"}), 401

@app.route('/buses', methods=['GET'])
def buses():
    query = (
        "SELECT bus_id, line, lat, lon, bearing, timestamp "
        "FROM bus_positions "
        "WHERE timestamp > dateadd('m', -5, now()) "
        "LATEST ON timestamp PARTITION BY bus_id"
    )
    url = 'http://questdb:9000/exec?query=' + urllib.parse.quote(query)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            rows = json.load(r).get('dataset', [])
    except Exception:
        return jsonify({"type": "FeatureCollection", "features": []}), 503
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row[3], row[2]]},
            "properties": {"bus_id": row[0], "line": row[1], "bearing": row[4]},
        }
        for row in rows
    ]
    return jsonify({"type": "FeatureCollection", "features": features})


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)
