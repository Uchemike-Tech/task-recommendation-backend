# backend/app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
import requests
import sqlite3
from datetime import datetime, timedelta
import os
from functools import wraps

app = Flask(__name__)
CORS(app)

# Configuration
app.config['JWT_SECRET_KEY'] = 'your-super-secret-key-change-this-in-production'
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=24)
jwt = JWTManager(app)

# ML Service URL (Update with your Render URL)
ML_SERVICE_URL = os.environ.get('ML_SERVICE_URL', 'https://ml-task-service.onrender.com')

# Database setup
def init_db():
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE NOT NULL,
                  password TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # User tasks table
    c.execute('''CREATE TABLE IF NOT EXISTS user_tasks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  task_id INTEGER NOT NULL,
                  task_name TEXT NOT NULL,
                  status TEXT DEFAULT 'pending',
                  priority INTEGER,
                  estimated_hours REAL,
                  complexity INTEGER,
                  category TEXT,
                  completed_at TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (id))''')
    
    # Task completion history
    c.execute('''CREATE TABLE IF NOT EXISTS task_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  task_id INTEGER NOT NULL,
                  completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (id))''')
    
    conn.commit()
    conn.close()

init_db()

# Helper function to get tasks from ML service
def get_all_tasks_from_ml():
    try:
        response = requests.get(f"{ML_SERVICE_URL}/tasks", timeout=10)
        if response.status_code == 200:
            return response.json()['tasks']
        return []
    except Exception as e:
        print(f"Error fetching tasks from ML service: {e}")
        return []

# Helper function to check task dependencies
def check_dependencies(task_id, completed_tasks):
    try:
        response = requests.get(f"{ML_SERVICE_URL}/dependencies/{task_id}", timeout=10)
        if response.status_code == 200:
            deps = response.json()['dependencies']
            return all(dep in completed_tasks for dep in deps)
        return True
    except:
        return True

# Routes
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    
    try:
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", 
                  (username, password))
        conn.commit()
        return jsonify({'message': 'User created successfully'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Username already exists'}), 409
    finally:
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE username = ? AND password = ?", 
              (username, password))
    user = c.fetchone()
    conn.close()
    
    if user:
        access_token = create_access_token(identity=str(user[0]))
        return jsonify({
            'token': access_token, 
            'user_id': user[0],
            'username': username
        }), 200
    else:
        return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/tasks', methods=['GET'])
@jwt_required()
def get_user_tasks():
    user_id = int(get_jwt_identity())
    
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    c.execute("""SELECT task_id, task_name, status, priority, estimated_hours, 
                       complexity, category, completed_at 
                FROM user_tasks WHERE user_id = ?""", (user_id,))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        # Initialize tasks from ML service
        ml_tasks = get_all_tasks_from_ml()
        if ml_tasks:
            conn = sqlite3.connect('tasks.db')
            c = conn.cursor()
            for task in ml_tasks:
                c.execute("""INSERT INTO user_tasks 
                           (user_id, task_id, task_name, priority, estimated_hours, complexity, category) 
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                          (user_id, task['task_id'], task['task_name'], 
                           task['priority'], task['estimated_hours'], 
                           task['complexity'], task['category']))
            conn.commit()
            conn.close()
            
            tasks = [{
                'task_id': task['task_id'],
                'task_name': task['task_name'],
                'status': 'pending',
                'priority': task['priority'],
                'estimated_hours': task['estimated_hours'],
                'complexity': task['complexity'],
                'category': task['category'],
                'completed_at': None
            } for task in ml_tasks]
        else:
            tasks = []
    else:
        tasks = [{
            'task_id': row[0],
            'task_name': row[1],
            'status': row[2],
            'priority': row[3],
            'estimated_hours': row[4],
            'complexity': row[5],
            'category': row[6],
            'completed_at': row[7]
        } for row in rows]
    
    return jsonify({'tasks': tasks})

@app.route('/tasks/<int:task_id>/complete', methods=['PUT'])
@jwt_required()
def complete_task(task_id):
    user_id = int(get_jwt_identity())
    
    # Get completed tasks
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    c.execute("SELECT task_id FROM task_history WHERE user_id = ?", (user_id,))
    completed_tasks = [row[0] for row in c.fetchall()]
    
    # Check dependencies
    if not check_dependencies(task_id, completed_tasks):
        return jsonify({'error': 'Dependencies not completed for this task'}), 400
    
    # Mark task as completed
    c.execute("""UPDATE user_tasks 
                 SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
                 WHERE user_id = ? AND task_id = ?""", (user_id, task_id))
    
    # Record in history
    c.execute("INSERT INTO task_history (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
    
    conn.commit()
    conn.close()
    
    return jsonify({'message': 'Task completed successfully'})

@app.route('/recommendation', methods=['GET'])
@jwt_required()
def get_recommendation():
    user_id = int(get_jwt_identity())
    
    # Get completed tasks in order
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    c.execute("SELECT task_id FROM task_history WHERE user_id = ? ORDER BY completed_at", (user_id,))
    completed_tasks = [row[0] for row in c.fetchall()]
    conn.close()
    
    # Get prediction from ML service
    try:
        response = requests.post(
            f"{ML_SERVICE_URL}/predict",
            json={"completed_task_ids": completed_tasks},
            timeout=10
        )
        if response.status_code == 200:
            prediction = response.json()
            return jsonify({
                'recommended_task_id': prediction['predicted_task_id'],
                'recommended_task_name': prediction['predicted_task_name'],
                'confidence': prediction.get('confidence'),
                'top_3': prediction.get('top_3_predictions', [])
            })
        else:
            return jsonify({'error': 'ML service prediction failed'}), 500
    except Exception as e:
        print(f"Prediction error: {e}")
        return jsonify({'error': 'Unable to get recommendation'}), 500

@app.route('/reset_tasks', methods=['POST'])
@jwt_required()
def reset_tasks():
    user_id = int(get_jwt_identity())
    
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    
    # Reset user tasks
    c.execute("DELETE FROM task_history WHERE user_id = ?", (user_id,))
    c.execute("UPDATE user_tasks SET status = 'pending', completed_at = NULL WHERE user_id = ?", (user_id,))
    
    conn.commit()
    conn.close()
    
    return jsonify({'message': 'All tasks reset successfully'})

@app.route('/profile', methods=['GET'])
@jwt_required()
def get_profile():
    user_id = int(get_jwt_identity())
    
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    c.execute("SELECT username, created_at FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    
    c.execute("SELECT COUNT(*) FROM task_history WHERE user_id = ?", (user_id,))
    completed_count = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM user_tasks WHERE user_id = ?", (user_id,))
    total_tasks = c.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'username': user[0],
        'joined_date': user[1],
        'completed_tasks': completed_count,
        'total_tasks': total_tasks,
        'progress': (completed_count / total_tasks * 100) if total_tasks > 0 else 0
    })

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'service': 'backend'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)