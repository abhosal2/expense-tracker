from flask import Flask, request, jsonify, send_from_directory, Response, session, redirect
import sqlite3
import os
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")  # set SECRET_KEY on Render for best practice
DB_NAME = "expenses.db"


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Personal expenses
    cur.execute("""
        CREATE TABLE IF NOT EXISTS personal_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Split expenses (header)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS split_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            paid_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Split people (line items)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS split_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            split_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY(split_id) REFERENCES split_expenses(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()


init_db()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
def root():
    # If logged in, go to app. Otherwise show login page.
    if session.get("user_id"):
        return redirect("/app")
    return send_from_directory(".", "login.html")


@app.route("/app")
def app_page():
    if not session.get("user_id"):
        return redirect("/")
    return send_from_directory(".", "index.html")


# ---------- AUTH API ----------

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    pw_hash = generate_password_hash(password)

    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, pw_hash))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Username already exists"}), 400

    conn.close()
    return jsonify({"ok": True})


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = get_conn()
    row = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid username or password"}), 400

    session["user_id"] = row["id"]
    session["username"] = username
    return jsonify({"ok": True})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me", methods=["GET"])
def me():
    if not session.get("user_id"):
        return jsonify({"loggedIn": False})
    return jsonify({"loggedIn": True, "username": session.get("username")})


# ---------- PERSONAL API ----------

@app.route("/api/personal", methods=["GET"])
@login_required
def list_personal():
    uid = session["user_id"]
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, date, category, amount, description
        FROM personal_expenses
        WHERE user_id = ?
        ORDER BY date DESC, id DESC
    """, (uid,)).fetchall()
    conn.close()

    return jsonify([
        {"id": r["id"], "date": r["date"], "category": r["category"], "amount": r["amount"], "desc": r["description"]}
        for r in rows
    ])


@app.route("/api/personal", methods=["POST"])
@login_required
def add_personal():
    uid = session["user_id"]
    data = request.get_json(force=True)

    date = data.get("date")
    category = data.get("category")
    amount = data.get("amount")
    desc = data.get("desc")

    if not (date and category and desc) or amount is None:
        return jsonify({"error": "Missing fields"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO personal_expenses (user_id, date, category, amount, description) VALUES (?, ?, ?, ?, ?)",
        (uid, date, category, float(amount), desc),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id})


@app.route("/api/personal/<int:pid>", methods=["DELETE"])
@login_required
def delete_personal(pid: int):
    uid = session["user_id"]
    conn = get_conn()
    conn.execute("DELETE FROM personal_expenses WHERE id = ? AND user_id = ?", (pid, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- SPLIT API ----------

@app.route("/api/split", methods=["GET"])
@login_required
def list_split():
    uid = session["user_id"]
    conn = get_conn()
    splits = conn.execute("""
        SELECT id, date, category, amount, description, paid_by
        FROM split_expenses
        WHERE user_id = ?
        ORDER BY date DESC, id DESC
    """, (uid,)).fetchall()

    result = []
    for s in splits:
        people = conn.execute("""
            SELECT name, amount
            FROM split_people
            WHERE split_id = ?
            ORDER BY id ASC
        """, (s["id"],)).fetchall()

        result.append({
            "id": s["id"],
            "date": s["date"],
            "category": s["category"],
            "amount": s["amount"],
            "desc": s["description"],
            "paidBy": s["paid_by"],
            "people": [{"name": p["name"], "amount": p["amount"]} for p in people]
        })

    conn.close()
    return jsonify(result)


@app.route("/api/split", methods=["POST"])
@login_required
def add_split():
    uid = session["user_id"]
    data = request.get_json(force=True)

    date = data.get("date")
    category = data.get("category")
    amount = data.get("amount")
    desc = data.get("desc")
    paid_by = data.get("paidBy")
    people = data.get("people", [])

    if not (date and category and desc and paid_by) or amount is None:
        return jsonify({"error": "Missing fields"}), 400

    if not isinstance(people, list) or len(people) == 0:
        return jsonify({"error": "Split must include at least one person"}), 400

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO split_expenses (user_id, date, category, amount, description, paid_by) VALUES (?, ?, ?, ?, ?, ?)",
        (uid, date, category, float(amount), desc, paid_by),
    )
    split_id = cur.lastrowid

    for p in people:
        n = (p.get("name") or "").strip()
        a = p.get("amount")
        if n and a is not None:
            cur.execute(
                "INSERT INTO split_people (split_id, name, amount) VALUES (?, ?, ?)",
                (split_id, n, float(a)),
            )

    conn.commit()
    conn.close()
    return jsonify({"id": split_id})


@app.route("/api/split/<int:sid>", methods=["DELETE"])
@login_required
def delete_split(sid: int):
    uid = session["user_id"]
    conn = get_conn()

    # Ensure the split belongs to the user
    row = conn.execute("SELECT id FROM split_expenses WHERE id = ? AND user_id = ?", (sid, uid)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    conn.execute("DELETE FROM split_people WHERE split_id = ?", (sid,))
    conn.execute("DELETE FROM split_expenses WHERE id = ? AND user_id = ?", (sid, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- CSV EXPORT (current user's data) ----------

@app.route("/api/export.csv", methods=["GET"])
@login_required
def export_csv():
    uid = session["user_id"]
    conn = get_conn()
    cur = conn.cursor()

    personal = cur.execute("""
        SELECT id, date, category, amount, description
        FROM personal_expenses
        WHERE user_id = ?
        ORDER BY date DESC, id DESC
    """, (uid,)).fetchall()

    splits = cur.execute("""
        SELECT id, date, category, amount, description, paid_by
        FROM split_expenses
        WHERE user_id = ?
        ORDER BY date DESC, id DESC
    """, (uid,)).fetchall()

    def esc(s):
        s = str(s)
        s = s.replace('"', '""')
        return f'"{s}"'

    lines = []
    lines.append("type,record_id,date,category,total_amount,description,paid_by,split_id,person_name,person_amount")

    for r in personal:
        lines.append(
            f'PERSONAL,{r["id"]},{r["date"]},{esc(r["category"])},{r["amount"]},{esc(r["description"])},,,,'
        )

    for s in splits:
        people = cur.execute("""
            SELECT name, amount
            FROM split_people
            WHERE split_id = ?
            ORDER BY id ASC
        """, (s["id"],)).fetchall()

        if not people:
            lines.append(
                f'SPLIT,{s["id"]},{s["date"]},{esc(s["category"])},{s["amount"]},{esc(s["description"])},{esc(s["paid_by"])},{s["id"]},,'
            )
        else:
            for p in people:
                lines.append(
                    f'SPLIT,{s["id"]},{s["date"]},{esc(s["category"])},{s["amount"]},{esc(s["description"])},{esc(s["paid_by"])},{s["id"]},{esc(p["name"])},{p["amount"]}'
                )

    conn.close()

    csv_text = "\n".join(lines)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=expenses_export.csv"}
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
