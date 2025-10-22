from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from PIL import Image, ImageOps
import os
from pathlib import Path
import uuid
from uuid import uuid4
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename 
from functools import wraps
from flask import session

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,        # trust X-Forwarded-For
    x_proto=1,      # trust X-Forwarded-Proto (http/https)
    x_host=1,       # trust X-Forwarded-Host
    x_port=1,       # trust X-Forwarded-Port
    x_prefix=1
)
DB_PATH = Path("inventory.db")

USERS = {
    "admin": "password",   
    # "staff": "pass"  
}

# Upload config
UPLOAD_FOLDER = Path("static/uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTS = {"png", "jpg", "jpeg", "gif"}
MAX_MB = 10
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

SIZES = ["S", "M", "L", "XL"]

# --- Authentication def ---
def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapper

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_photo_column(conn):
    # Add column 'photo' if missing (SQLite)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "photo" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN photo TEXT")
        conn.commit()

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL CHECK(price >= 0),
            quantity INTEGER NOT NULL CHECK(quantity >= 0),
            size TEXT NOT NULL CHECK(size IN ('S','M','L','XL')),
            photo TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            user TEXT,
            action TEXT NOT NULL CHECK(action IN ('ADD','WITHDRAW')),
            item_id INTEGER,
            name TEXT,
            size TEXT,
            price REAL,
            quantity INTEGER,            -- how many were added/withdrawn
            subtotal REAL                -- price * quantity snapshot
        );
    """)
    conn.commit()
    conn.close()

def log_action(conn, *, user, action, item_id, name, size, price, quantity):
    """Write one row to logs using an existing open connection/transaction."""
    conn.execute("""
        INSERT INTO logs (user, action, item_id, name, size, price, quantity, subtotal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user, action, item_id, name, size, float(price), int(quantity), float(price)*int(quantity)))

def get_cart():
    # cart is dict: { item_id(str): qty(int) }
    return session.get("withdraw_cart", {})

def save_cart(cart):
    session["withdraw_cart"] = cart

def cart_count():
    cart = get_cart()
    return sum(cart.values())  # total qty in cart (use len(cart) if you want distinct)

def save_uploaded_image(file):
    if not file:
        return None

    # create folder if not exist
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # generate unique filename
    ext = file.filename.rsplit(".", 1)[-1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_FOLDER, filename)

    # open image with Pillow
    img = Image.open(file)
    
    # Auto-rotate based on EXIF
    img = ImageOps.exif_transpose(img)

    # optional: resize if bigger than 800px wide
    max_width = 500
    if img.width > max_width:
        ratio = max_width / float(img.width)
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.LANCZOS)

    # save with compression
    if ext in ["jpg", "jpeg"]:
        img = img.convert("RGB")  # ensure correct format
        img.save(path, "JPEG", quality=40, optimize=True)
    elif ext == "png":
        img.save(path, "PNG", optimize=True)
    else:
        # fallback: save original
        file.save(path)

    return filename

@app.route("/test_ip")
def test_ip():
    return f"Your IP: {request.remote_addr}"


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)   # current page
    per_page = 5                                   # items per page
    offset = (page - 1) * per_page

    conn = get_db()

    if q:
        # count total
        total = conn.execute("""
            SELECT COUNT(*) FROM items
            WHERE name LIKE ? OR size LIKE ?
        """, (f"%{q}%", f"%{q}%")).fetchone()[0]

        rows = conn.execute("""
            SELECT * FROM items
            WHERE name LIKE ? OR size LIKE ?
            ORDER BY id ASC
            LIMIT ? OFFSET ?
        """, (f"%{q}%", f"%{q}%", per_page, offset)).fetchall()
    else:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

        rows = conn.execute("""
            SELECT * FROM items
            ORDER BY id ASC
            LIMIT ? OFFSET ?
        """, (per_page, offset)).fetchall()

    conn.close()

    total_pages = (total // per_page) + (1 if total % per_page else 0)

    return render_template(
        "list.html",
        items=rows,
        q=q,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        if username in USERS and USERS[username] == password:
            session["user"] = username
            flash(f"Welcome, {username}!", "success")
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        else:
            flash("Invalid username or password.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out.", "info")
    return redirect(url_for("index"))

@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    if request.method == "POST":
        name = request.form.get("name","").strip()
        price = request.form.get("price","").strip()
        quantity = request.form.get("quantity","").strip()
        size = request.form.get("size","").strip()
        file = request.files.get("photo")

        errors = []
        if not name: errors.append("Name is required.")
        try:
            price_val = float(price);  assert price_val >= 0
        except:
            errors.append("Price must be a number ≥ 0.")
        try:
            qty_val = int(quantity);   assert qty_val >= 0
        except:
            errors.append("Quantity must be an integer ≥ 0.")
        if size not in SIZES:
            errors.append("Size must be one of S, M, L, XL.")

        photo_name = None
        if file and file.filename:
            try:
                photo_name = save_uploaded_image(file)
            except Exception as e:
                errors.append(str(e))

        if errors:
            for e in errors: flash(e, "danger")
            return render_template("form.html", mode="Add",
                                   item={"name":name, "price":price, "quantity":quantity, "size":size, "photo":photo_name},
                                   SIZES=SIZES)

        conn = get_db()
        cur = conn.execute(
            "INSERT INTO items (name,price,quantity,size,photo) VALUES (?,?,?,?,?)",
            (name, price_val, qty_val, size, photo_name)
        )
        new_id = cur.lastrowid
        log_action(conn,user=(session.get("user") or "-"),action="ADD",item_id=new_id,name=name,size=size,price=price_val,quantity=qty_val)

        conn.commit(); conn.close()
        flash("Item added.", "success")
        return redirect(url_for("index"))

    return render_template("form.html", mode="Add", item=None, SIZES=SIZES)

@app.route("/edit/<int:item_id>", methods=["GET", "POST"])
@login_required
def edit(item_id):
    conn = get_db()
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        conn.close()
        flash("Item not found.", "warning")
        return redirect(url_for("index"))

    if request.method == "POST":
        # NEW: read the desired new id
        new_id_raw = request.form.get("id", "").strip()

        name = request.form.get("name","").strip()
        price = request.form.get("price","").strip()
        quantity = request.form.get("quantity","").strip()
        size = request.form.get("size","").strip()
        file = request.files.get("photo")
        keep_old = request.form.get("keep_old_photo") == "1"

        errors = []
        # validate new id
        try:
            new_id = int(new_id_raw)
            if new_id <= 0:
                errors.append("ID must be a positive integer.")
        except:
            errors.append("ID must be an integer.")

        if not name: errors.append("Name is required.")
        try:
            price_val = float(price);  assert price_val >= 0
        except:
            errors.append("Price must be a number ≥ 0.")
        try:
            qty_val = int(quantity);   assert qty_val >= 0
        except:
            errors.append("Quantity must be an integer ≥ 0.")
        if size not in SIZES:
            errors.append("Size must be one of S, M, L, XL.")

        # photo logic
        photo_name = item["photo"]
        if file and file.filename:
            try:
                photo_name = save_uploaded_image(file)
            except Exception as e:
                errors.append(str(e))
        elif not keep_old:
            photo_name = None

        # if changing to a different id, ensure it doesn't already exist
        if new_id != item["id"]:
            exists = conn.execute("SELECT 1 FROM items WHERE id=?", (new_id,)).fetchone()
            if exists:
                errors.append(f"ID {new_id} is already in use.")

        if errors:
            for e in errors: flash(e, "danger")
            conn.close()
            return render_template("form.html", mode="Edit",
                                   item={"id": item_id, "name":name, "price":price, "quantity":quantity, "size":size, "photo":photo_name},
                                   SIZES=SIZES)

        # IMPORTANT: update id and other fields in one statement
        conn.execute("""
            UPDATE items
            SET id=?, name=?, price=?, quantity=?, size=?, photo=?
            WHERE id=?""",
            (new_id, name, price_val, qty_val, size, photo_name, item_id))
        conn.commit(); conn.close()

        flash(f"Item updated (ID now {new_id}).", "success")
        return redirect(url_for("index"))

    conn.close()
    return render_template("form.html", mode="Edit", item=item, SIZES=SIZES)

@app.route('/duplicate/<int:item_id>', methods=['GET', 'POST'])
def duplicate_item(item_id):
    conn = get_db()
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        flash("Item not found", "danger")
        return redirect(url_for('index'))

    if request.method == 'POST':
        new_id_raw = request.form.get("id", "").strip()
        name = request.form.get("name","").strip()
        price = request.form.get("price","").strip()
        quantity = request.form.get("quantity","").strip()
        size = request.form.get("size","").strip()
        file = request.files.get("photo")
        keep_old = request.form.get("keep_old_photo") == "1"

        errors = []
        # validate new id
        try:
            new_id = int(new_id_raw)
            if new_id <= 0:
                errors.append("ID must be a positive integer.")
        except:
            errors.append("ID must be an integer.")

        if not name: errors.append("Name is required.")
        try:
            price_val = float(price);  assert price_val >= 0
        except:
            errors.append("Price must be a number ≥ 0.")
        try:
            qty_val = int(quantity);   assert qty_val >= 0
        except:
            errors.append("Quantity must be an integer ≥ 0.")
        if size not in SIZES:
            errors.append("Size must be one of S, M, L, XL.")

        # photo logic
        photo_name = item["photo"]
        if file and file.filename:
            try:
                photo_name = save_uploaded_image(file)
            except Exception as e:
                errors.append(str(e))
        elif not keep_old:
            photo_name = None

        cur = conn.execute("INSERT INTO items (name,price,quantity,size,photo) VALUES (?,?,?,?,?)",(name, price_val, qty_val, size, photo_name))
        new_id = cur.lastrowid
        log_action(conn,user=(session.get("user") or "-"),action="ADD",item_id=new_id,name=name,size=size,price=price_val,quantity=qty_val)

        conn.commit(); conn.close()
        flash("Item duplicated successfully", "success")
        return redirect(url_for('index'))

    return render_template('form.html', mode="Duplicate", item=item, SIZES=SIZES)

@app.route("/withdraw")
@login_required
def withdraw_page():
    cart = get_cart()
    if not cart:
        items = []
        total = 0.0
    else:
        # fetch items in cart
        ids = tuple(int(i) for i in cart.keys())
        placeholders = ",".join("?" for _ in ids)
        conn = get_db()
        rows = conn.execute(f"SELECT * FROM items WHERE id IN ({placeholders})", ids).fetchall()
        conn.close()
        # attach qty & subtotal
        items = []
        total = 0.0
        for r in rows:
            qty = int(cart.get(str(r["id"]), 0))
            subtotal = float(r["price"]) * qty
            total += subtotal
            items.append({"row": r, "qty": qty, "subtotal": subtotal})
    return render_template("withdraw.html", items=items, total=total)

@app.route("/withdraw/add/<int:item_id>", methods=["POST"])
@login_required
def withdraw_add(item_id):
    qty_raw = request.form.get("qty", "1").strip()
    try:
        qty = max(1, int(qty_raw))
    except:
        qty = 1
    cart = get_cart()
    cart[str(item_id)] = cart.get(str(item_id), 0) + qty
    save_cart(cart)
    flash("Added to withdraw list.", "success")
    return redirect(request.referrer or url_for("index"))

@app.route("/withdraw/update", methods=["POST"])
@login_required
def withdraw_update():
    # expects fields like qty_<id>
    cart = {}
    for k, v in request.form.items():
        if k.startswith("qty_"):
            item_id = k[4:]
            try:
                q = int(v)
            except:
                q = 0
            if q > 0:
                cart[item_id] = q
    save_cart(cart)
    flash("Withdraw quantities updated.", "success")
    return redirect(url_for("withdraw_page"))

@app.route("/withdraw/clear", methods=["POST"])
@login_required
def withdraw_clear():
    save_cart({})
    flash("Withdraw list cleared.", "info")
    return redirect(url_for("withdraw_page"))

@app.route("/withdraw/confirm", methods=["POST"])
@login_required
def withdraw_confirm():
    cart = get_cart()
    if not cart:
        flash("Nothing to withdraw.", "warning")
        return redirect(url_for("withdraw_page"))

    conn = get_db()
    try:
        conn.execute("BEGIN")
        # check stock and deduct
        for sid, qty in cart.items():
            item_id = int(sid)
            row = conn.execute("SELECT quantity, name FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise ValueError(f"Item {item_id} not found.")
            if qty > row["quantity"]:
                raise ValueError(f"Not enough stock for '{row['name']}'. Requested {qty}, have {row['quantity']}.")

        for sid, qty in cart.items():
            item_id = int(sid)
            # fetch snapshot for logging (name/size/price)
            r = conn.execute(
                "SELECT name, size, price FROM items WHERE id=?", (item_id,)
            ).fetchone()

            # write one log row per item
            log_action(
                conn,
                user=(session.get("user") or "-"),
                action="WITHDRAW",
                item_id=item_id,
                name=r["name"],
                size=r["size"],
                price=r["price"],
                quantity=int(qty),
            )

            # deduct stock
            conn.execute(
                "UPDATE items SET quantity = quantity - ? WHERE id=?",
                (int(qty), item_id),
            )

        conn.commit()
        save_cart({})
        flash("Withdraw confirmed. Stock updated.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Withdraw failed: {e}", "danger")
    finally:
        conn.close()
    return redirect(url_for("index"))

@app.route("/delete/<int:item_id>", methods=["POST"])
@login_required
def delete(item_id):
    conn = get_db()
    # (Optional) If you want to also delete the file from disk, read the photo name first.
    row = conn.execute("SELECT photo FROM items WHERE id=?", (item_id,)).fetchone()
    conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit(); conn.close()

    # Uncomment to remove file from disk when item is deleted:
    # if row and row["photo"]:
    #     try:
    #         (UPLOAD_DIR / row["photo"]).unlink(missing_ok=True)
    #     except Exception:
    #         pass

    flash("Item deleted.", "info")
    return redirect(url_for("index"))

@app.route("/logs")
@login_required
def view_logs():
    # optional filters
    action = request.args.get("action", "").upper().strip()
    q = request.args.get("q", "").strip()

    sql = "SELECT * FROM logs"
    params = []
    clauses = []

    if action in ("ADD","WITHDRAW"):
        clauses.append("action = ?")
        params.append(action)
    if q:
        # search by name or user or size
        clauses.append("(name LIKE ? OR user LIKE ? OR size LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    sql += " ORDER BY id DESC LIMIT 500"  # show recent 500

    conn = get_db()
    rows = conn.execute(sql, params).fetchall()

    # totals for current view
    totals = conn.execute("""
        SELECT
          SUM(CASE WHEN action='ADD' THEN quantity ELSE 0 END) AS added_qty,
          SUM(CASE WHEN action='WITHDRAW' THEN quantity ELSE 0 END) AS withdrawn_qty,
          SUM(CASE WHEN action='ADD' THEN subtotal ELSE 0 END) AS added_value,
          SUM(CASE WHEN action='WITHDRAW' THEN subtotal ELSE 0 END) AS withdrawn_value
        FROM (""" + sql + """)
    """, params).fetchone()
    conn.close()

    return render_template("logs.html", logs=rows, totals=totals, q=q, action=action)

if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
