from flask import Flask, request, jsonify, send_from_directory, Response
import sqlite3

app = Flask(__name__)
DB_NAME = "expenses.db"


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Personal expenses
    cur.execute("""
        CREATE TABLE IF NOT EXISTS personal_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL
        )
    """)

    # Split expenses (header)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS split_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            paid_by TEXT NOT NULL
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


@app.route("/")
def home():
    # Serves your index.html from the same folder as app.py
    return send_from_directory(".", "index.html")


# ---------- PERSONAL API ----------

@app.route("/api/personal", methods=["GET"])
def list_personal():
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, date, category, amount, description
        FROM personal_expenses
        ORDER BY date DESC, id DESC
    """).fetchall()
    conn.close()

    return jsonify([
        {
            "id": r["id"],
            "date": r["date"],
            "category": r["category"],
            "amount": r["amount"],
            "desc": r["description"],
        }
        for r in rows
    ])


@app.route("/api/personal", methods=["POST"])
def add_personal():
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
        "INSERT INTO personal_expenses (date, category, amount, description) VALUES (?, ?, ?, ?)",
        (date, category, float(amount), desc),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()

    return jsonify({"id": new_id})


@app.route("/api/personal/<int:pid>", methods=["DELETE"])
def delete_personal(pid: int):
    conn = get_conn()
    conn.execute("DELETE FROM personal_expenses WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- SPLIT API ----------

@app.route("/api/split", methods=["GET"])
def list_split():
    conn = get_conn()
    splits = conn.execute("""
        SELECT id, date, category, amount, description, paid_by
        FROM split_expenses
        ORDER BY date DESC, id DESC
    """).fetchall()

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
def add_split():
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
        "INSERT INTO split_expenses (date, category, amount, description, paid_by) VALUES (?, ?, ?, ?, ?)",
        (date, category, float(amount), desc, paid_by),
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
def delete_split(sid: int):
    # Delete children then parent (works even if FK cascade isn't enabled)
    conn = get_conn()
    conn.execute("DELETE FROM split_people WHERE split_id = ?", (sid,))
    conn.execute("DELETE FROM split_expenses WHERE id = ?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})
@app.route("/api/export.csv", methods=["GET"])
def export_csv():
    conn = get_conn()
    cur = conn.cursor()

    # Personal expenses
    personal = cur.execute("""
        SELECT id, date, category, amount, description
        FROM personal_expenses
        ORDER BY date DESC, id DESC
    """).fetchall()

    # Split expenses + people
    splits = cur.execute("""
        SELECT id, date, category, amount, description, paid_by
        FROM split_expenses
        ORDER BY date DESC, id DESC
    """).fetchall()

    # Build CSV
    # One simple format: each row is either PERSONAL or SPLIT_PERSON
    # SPLIT_PERSON rows include split_id and person_name/person_amount
    lines = []
    lines.append("type,record_id,date,category,total_amount,description,paid_by,split_id,person_name,person_amount")

    # PERSONAL rows
    for r in personal:
        # type, record_id, date, category, total_amount, description, paid_by, split_id, person_name, person_amount
        lines.append(f'PERSONAL,{r["id"]},{r["date"]},"{r["category"]}",{r["amount"]},"{r["description"].replace("\"","\"\"")}",,,,')

    # SPLIT rows (expand per person)
    for s in splits:
        people = cur.execute("""
            SELECT name, amount
            FROM split_people
            WHERE split_id = ?
            ORDER BY id ASC
        """, (s["id"],)).fetchall()

        # If no people (shouldn’t happen), still export one row
        if not people:
            lines.append(
                f'SPLIT,{s["id"]},{s["date"]},"{s["category"]}",{s["amount"]},"{s["description"].replace("\"","\"\"")}","{s["paid_by"].replace("\"","\"\"")}",{s["id"]},,'
            )
        else:
            for p in people:
                lines.append(
                    f'SPLIT,{s["id"]},{s["date"]},"{s["category"]}",{s["amount"]},"{s["description"].replace("\"","\"\"")}","{s["paid_by"].replace("\"","\"\"")}",{s["id"]},"{p["name"].replace("\"","\"\"")}",{p["amount"]}'
                )

    conn.close()

    csv_text = "\n".join(lines)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=expenses_export.csv"}
    )



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

