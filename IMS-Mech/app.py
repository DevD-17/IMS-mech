"""
IMS-Mech - Mechanical Manufacturing Inventory Management System
===============================================================
A single-file Flask application covering:
  * Inventory (raw / WIP / finished goods) with stock transactions
  * Suppliers and purchase orders (multi-line, partial receipts, reorder)
  * Manufacturing (BOM with kerf/scrap, job orders, routing, scrap recovery)
  * Customers, sales orders and dispatch (with serials & warranty)
  * Traceability (heat/melt numbers, MTC references)
  * Valuation (weighted-average cost + FIFO batches)
  * Basic role-based access (Admin / Store Manager / Production Manager)

Run:  python app.py
"""

import os
from datetime import date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, g,
)
import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("IMS_SECRET_KEY", "ims-mech-secret-key-change-in-production")

# ----------------------------------------------------------------------
# Database configuration (override with environment variables)
# ----------------------------------------------------------------------
DB_CONFIG = {
    "host":     os.environ.get("IMS_DB_HOST", "localhost"),
    "port":     os.environ.get("IMS_DB_PORT", "5432"),
    "database": os.environ.get("IMS_DB_NAME", "ims_mech"),
    "user":     os.environ.get("IMS_DB_USER", "postgres"),
    "password": os.environ.get("IMS_DB_PASSWORD", "postgres"),
}

VALID_ROLES = {"Admin", "Store Manager", "Production Manager"}


def get_db():
    """Return a new PostgreSQL connection."""
    return psycopg2.connect(**DB_CONFIG)


# ----------------------------------------------------------------------
# Authentication helpers
# ----------------------------------------------------------------------
@app.before_request
def load_logged_in_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.user_id, u.username, u.full_name, r.role_name
                    FROM users u
                    JOIN roles r ON r.role_id = u.role_id
                    WHERE u.user_id = %s AND u.active = TRUE
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if row:
                    g.user = {
                        "id": row[0], "username": row[1],
                        "full_name": row[2], "role": row[3],
                    }
        finally:
            conn.close()


def login_required(role=None):
    """Decorator: require login (and optionally a role).

    Works both as ``@login_required`` and ``@login_required(role='Admin')``.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not g.user:
                flash("Please log in to access this page.")
                return redirect(url_for("login"))
            if role and g.user["role"] != role and g.user["role"] != "Admin":
                flash("You do not have permission to access that page.")
                return redirect(url_for("home"))
            return fn(*args, **kwargs)
        return wrapper

    # If used as @login_required (no parentheses), role is the view function.
    if callable(role):
        view_fn = role
        role = None
        return deco(view_fn)
    return deco


def ensure_admin_account():
    """Make sure the seeded admin account has a real password hash."""
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT password_hash FROM users WHERE username = 'admin'"
                )
                row = cur.fetchone()
                if row and "placeholder" in (row[0] or ""):
                    cur.execute(
                        "UPDATE users SET password_hash = %s WHERE username = 'admin'",
                        (generate_password_hash("admin123"),),
                    )
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Inventory helper functions
# ----------------------------------------------------------------------
def default_location(cur):
    """Return location_id of Main Warehouse / A-01."""
    cur.execute(
        """
        SELECT sl.location_id
        FROM storage_locations sl
        JOIN warehouses w ON w.warehouse_id = sl.warehouse_id
        WHERE w.warehouse_name = 'Main Warehouse' AND sl.location_code = 'A-01'
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("Default storage location not found. Run schema.sql first.")
    return row[0]


def get_stock(cur, item_id):
    """Return total stock quantity for an item across all locations."""
    cur.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM inventory_stock WHERE item_id = %s",
        (item_id,),
    )
    return cur.fetchone()[0]


def add_stock(cur, item_id, location_id, quantity, unit_cost=0,
              heat_lot=None, mtc_ref=None, tx_type="RECEIPT", note=""):
    """
    Add stock to inventory. Creates a FIFO batch, updates the
    weighted-average unit cost, and records a stock transaction.
    Returns the new transaction_id.
    """
    cur.execute(
        """
        SELECT quantity, unit_cost FROM inventory_stock
        WHERE item_id = %s AND location_id = %s
        """,
        (item_id, location_id),
    )
    row = cur.fetchone()
    if row:
        old_qty, old_cost = row[0], (row[1] or 0)
    else:
        old_qty, old_cost = 0, 0

    new_qty = old_qty + quantity
    if new_qty > 0:
        new_avg_cost = ((old_qty * old_cost) + (quantity * unit_cost)) / new_qty
    else:
        new_avg_cost = 0

    cur.execute(
        """
        INSERT INTO inventory_stock (item_id, location_id, quantity, unit_cost)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (item_id, location_id)
        DO UPDATE SET quantity = inventory_stock.quantity + EXCLUDED.quantity,
                      unit_cost = EXCLUDED.unit_cost
        """,
        (item_id, location_id, quantity, new_avg_cost),
    )

    cur.execute(
        """
        INSERT INTO stock_transactions
            (item_id, location_id, transaction_type, quantity, unit_cost,
             reference_note, heat_lot_no, mtc_reference)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING transaction_id
        """,
        (item_id, location_id, tx_type, quantity, unit_cost, note, heat_lot, mtc_ref),
    )
    tx_id = cur.fetchone()[0]

    # FIFO batch layer
    cur.execute(
        """
        INSERT INTO stock_batches
            (item_id, transaction_id, original_quantity, remaining_quantity,
             unit_cost, heat_lot_no, mtc_reference)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (item_id, tx_id, quantity, quantity, unit_cost, heat_lot, mtc_ref),
    )
    return tx_id


def remove_stock(cur, item_id, location_id, quantity, tx_type="DISPATCH", note=""):
    """
    Remove stock using FIFO batches (oldest first). Records a transaction
    using the weighted-average unit cost. Raises ValueError if insufficient.
    Returns the transaction_id.
    """
    available = get_stock(cur, item_id)
    if available < quantity:
        cur.execute("SELECT item_name FROM items WHERE item_id = %s", (item_id,))
        name = cur.fetchone()[0]
        raise ValueError(
            f"Insufficient stock for {name}. Available: {available}, "
            f"requested: {quantity}."
        )

    # Weighted-average unit cost for valuation of the removal
    cur.execute(
        "SELECT unit_cost FROM inventory_stock WHERE item_id = %s",
        (item_id,),
    )
    rows = cur.fetchall()
    avg_cost = 0
    total_qty = sum(r[0] for r in rows) or 1
    avg_cost = sum((r[0] or 0) for r in rows) / len(rows) if rows else 0
    # Use the overall weighted avg from all locations
    cur.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_cost) / NULLIF(SUM(quantity), 0), 0)
        FROM inventory_stock WHERE item_id = %s
        """,
        (item_id,),
    )
    avg_cost = cur.fetchone()[0] or 0

    # Reduce inventory_stock (prefer the specified location, then others)
    remaining = quantity
    cur.execute(
        """
        SELECT stock_id, quantity FROM inventory_stock
        WHERE item_id = %s AND quantity > 0
        ORDER BY (location_id = %s) DESC, stock_id
        FOR UPDATE
        """,
        (item_id, location_id),
    )
    for stock_id, qty in cur.fetchall():
        if remaining <= 0:
            break
        take = min(qty, remaining)
        cur.execute(
            "UPDATE inventory_stock SET quantity = quantity - %s WHERE stock_id = %s",
            (take, stock_id),
        )
        remaining -= take

    # Consume FIFO batches
    remaining = quantity
    cur.execute(
        """
        SELECT batch_id, remaining_quantity FROM stock_batches
        WHERE item_id = %s AND remaining_quantity > 0
        ORDER BY created_at, batch_id
        FOR UPDATE
        """,
        (item_id,),
    )
    for batch_id, rem in cur.fetchall():
        if remaining <= 0:
            break
        take = min(rem, remaining)
        cur.execute(
            "UPDATE stock_batches SET remaining_quantity = remaining_quantity - %s WHERE batch_id = %s",
            (take, batch_id),
        )
        remaining -= take

    cur.execute(
        """
        INSERT INTO stock_transactions
            (item_id, location_id, transaction_type, quantity, unit_cost,
             reference_note)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING transaction_id
        """,
        (item_id, location_id, tx_type, quantity, avg_cost, note),
    )
    return cur.fetchone()[0]


# ======================================================================
# AUTH ROUTES
# ======================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, username, password_hash, role_id
                    FROM users WHERE username = %s AND active = TRUE
                    """,
                    (username,),
                )
                row = cur.fetchone()
                if not row or not check_password_hash(row[2], password):
                    flash("Invalid username or password.")
                    return render_template("login.html")
                session.clear()
                session["user_id"] = row[0]
                return redirect(url_for("home"))
        finally:
            conn.close()
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


@app.route("/add-user", methods=["GET", "POST"])
@login_required(role="Admin")
def add_user():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT role_id, role_name FROM roles ORDER BY role_name")
            roles = cur.fetchall()
    finally:
        conn.close()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role_id = request.form.get("role_id")
        if not username or not password or not role_id:
            flash("Username, password and role are required.")
            return render_template("add_user.html", roles=roles)
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO users (username, full_name, password_hash, role_id, active)
                        VALUES (%s, %s, %s, %s, TRUE)
                        """,
                        (username, full_name, generate_password_hash(password), int(role_id)),
                    )
            flash(f"User '{username}' created.")
        except Exception as e:
            flash(f"Could not create user: {e}")
        finally:
            conn.close()
        return redirect(url_for("add_user"))
    return render_template("add_user.html", roles=roles)


# ======================================================================
# DASHBOARD
# ======================================================================
@app.route("/")
@login_required
def home():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.item_id, i.item_code, i.item_name, i.item_type, i.unit,
                       i.reorder_level, COALESCE(SUM(s.quantity), 0) AS stock
                FROM items i
                LEFT JOIN inventory_stock s ON s.item_id = i.item_id
                GROUP BY i.item_id
                ORDER BY i.item_name
                """
            )
            items = cur.fetchall()
            low_stock = [it for it in items if it[6] <= it[5]]

            cur.execute("SELECT COUNT(*) FROM suppliers")
            supplier_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM purchase_orders WHERE status <> 'RECEIVED'")
            open_po_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM job_orders WHERE status <> 'COMPLETED'")
            pending_jo_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM job_orders WHERE status = 'COMPLETED'")
            completed_jo_count = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM sales_orders WHERE status <> 'DISPATCHED'"
            )
            pending_so_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM dispatch_challans")
            dispatch_count = cur.fetchone()[0]

            cur.execute(
                """
                SELECT t.transaction_id, i.item_name, t.transaction_type, t.quantity,
                       t.unit_cost, t.reference_note, t.created_at
                FROM stock_transactions t
                JOIN items i ON i.item_id = t.item_id
                ORDER BY t.transaction_id DESC
                LIMIT 10
                """
            )
            recent_tx = cur.fetchall()

            cur.execute(
                """
                SELECT po_id, s.name, status, order_date
                FROM purchase_orders p JOIN suppliers s ON s.supplier_id = p.supplier_id
                WHERE p.status <> 'RECEIVED'
                ORDER BY po_id DESC LIMIT 5
                """
            )
            open_pos = cur.fetchall()

            cur.execute(
                """
                SELECT j.job_order_id, i.item_name, j.planned_quantity, j.status
                FROM job_orders j JOIN items i ON i.item_id = j.product_item_id
                WHERE j.status <> 'COMPLETED'
                ORDER BY j.job_order_id DESC LIMIT 5
                """
            )
            pending_jos = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "dashboard.html",
        items=items, low_stock=low_stock,
        supplier_count=supplier_count, open_po_count=open_po_count,
        pending_jo_count=pending_jo_count, completed_jo_count=completed_jo_count,
        pending_so_count=pending_so_count, dispatch_count=dispatch_count,
        recent_tx=recent_tx, open_pos=open_pos, pending_jos=pending_jos,
    )


# ======================================================================
# INVENTORY
# ======================================================================
@app.route("/inventory")
@login_required
def inventory():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.item_id, i.item_code, i.item_name, i.item_type, i.unit,
                       i.reorder_level, COALESCE(SUM(s.quantity), 0) AS stock,
                       COALESCE(MAX(s.unit_cost), 0) AS unit_cost
                FROM items i
                LEFT JOIN inventory_stock s ON s.item_id = i.item_id
                GROUP BY i.item_id
                ORDER BY i.item_name
                """
            )
            items = cur.fetchall()

            cur.execute(
                """
                SELECT t.transaction_id, i.item_name, t.transaction_type, t.quantity,
                       t.unit_cost, t.heat_lot_no, t.mtc_reference, t.reference_note, t.created_at
                FROM stock_transactions t
                JOIN items i ON i.item_id = t.item_id
                ORDER BY t.transaction_id DESC
                LIMIT 50
                """
            )
            transactions = cur.fetchall()
    finally:
        conn.close()
    return render_template("inventory.html", items=items, transactions=transactions)


@app.route("/add-item", methods=["POST"])
@login_required(role="Store Manager")
def add_item():
    try:
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO items (item_code, item_name, item_type, unit,
                                           reorder_level, description, unit_cost)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING item_id
                        """,
                        (
                            request.form["item_code"].strip(),
                            request.form["item_name"].strip(),
                            request.form["item_type"],
                            request.form["unit"].strip() or "pcs",
                            float(request.form["reorder_level"] or 10),
                            request.form.get("description", "").strip(),
                            float(request.form.get("unit_cost") or 0),
                        ),
                    )
                    item_id = cur.fetchone()[0]
                    location_id = default_location(cur)
                    cur.execute(
                        "INSERT INTO inventory_stock (item_id, location_id, quantity) "
                        "VALUES (%s, %s, 0) ON CONFLICT DO NOTHING",
                        (item_id, location_id),
                    )
            flash("Item added successfully.")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not add item: {e}")
    return redirect(url_for("inventory"))


@app.route("/receive", methods=["POST"])
@login_required(role="Store Manager")
def receive():
    try:
        item_id = int(request.form["item_id"])
        quantity = float(request.form["quantity"])
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero.")
        unit_cost = float(request.form.get("unit_cost") or 0)
        heat_lot = request.form.get("heat_lot_no", "").strip() or None
        mtc_ref = request.form.get("mtc_reference", "").strip() or None
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    location_id = default_location(cur)
                    add_stock(
                        cur, item_id, location_id, quantity, unit_cost,
                        heat_lot, mtc_ref, "RECEIPT", "Manual stock receipt",
                    )
            flash(f"Received {quantity} units of stock.")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not receive stock: {e}")
    return redirect(url_for("inventory"))


@app.route("/dispatch", methods=["POST"])
@login_required(role="Store Manager")
def dispatch_manual():
    try:
        item_id = int(request.form["item_id"])
        quantity = float(request.form["quantity"])
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero.")
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    location_id = default_location(cur)
                    remove_stock(
                        cur, item_id, location_id, quantity,
                        "DISPATCH", "Manual stock dispatch",
                    )
            flash(f"Dispatched {quantity} units.")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not dispatch stock: {e}")
    return redirect(url_for("inventory"))


# ======================================================================
# SUPPLIERS
# ======================================================================
@app.route("/suppliers")
@login_required
def suppliers():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM suppliers ORDER BY name")
            suppliers_list = cur.fetchall()
    finally:
        conn.close()
    return render_template("suppliers.html", suppliers=suppliers_list)


@app.route("/add-supplier", methods=["POST"])
@login_required(role="Store Manager")
def add_supplier():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Supplier name is required.")
        return redirect(url_for("suppliers"))
    contact = request.form.get("contact_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    address = request.form.get("address", "").strip()
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO suppliers (name, contact_name, phone, email, address)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (name, contact, phone, email, address),
                )
        flash("Supplier added successfully.")
    except Exception as e:
        flash(f"Could not add supplier: {e}")
    finally:
        conn.close()
    return redirect(url_for("suppliers"))


# ======================================================================
# PURCHASING
# ======================================================================
@app.route("/purchasing")
@login_required
def purchasing():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT item_id, item_code, item_name FROM items ORDER BY item_name")
            items = cur.fetchall()
            cur.execute("SELECT supplier_id, name FROM suppliers ORDER BY name")
            suppliers_list = cur.fetchall()

            cur.execute(
                """
                SELECT p.po_id, s.name, p.status, p.order_date, p.expected_date,
                       p.total_amount
                FROM purchase_orders p
                JOIN suppliers s ON s.supplier_id = p.supplier_id
                ORDER BY p.po_id DESC
                """
            )
            pos = cur.fetchall()

            cur.execute(
                """
                SELECT pi.po_id, pi.item_id, i.item_name, pi.quantity, pi.unit_price
                FROM po_items pi JOIN items i ON i.item_id = pi.item_id
                ORDER BY pi.po_id
                """
            )
            po_items = cur.fetchall()

            cur.execute(
                """
                SELECT g.grn_id, g.po_id, g.received_at,
                       COUNT(gi.grn_item_id) AS line_count,
                       COALESCE(SUM(gi.quantity), 0) AS total_qty
                FROM goods_receipt_notes g
                LEFT JOIN grn_items gi ON gi.grn_id = g.grn_id
                GROUP BY g.grn_id
                ORDER BY g.grn_id DESC
                """
            )
            grns = cur.fetchall()
    finally:
        conn.close()
    return render_template(
        "purchasing.html", items=items, suppliers=suppliers_list,
        pos=pos, po_items=po_items, grns=grns,
    )


@app.route("/create-po", methods=["POST"])
@login_required(role="Store Manager")
def create_po():
    try:
        supplier_id = int(request.form["supplier_id"])
        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity[]")
        unit_prices = request.form.getlist("unit_price[]")

        if not item_ids:
            raise ValueError("At least one line item is required.")

        lines = []
        for i in range(len(item_ids)):
            qty = float(quantities[i])
            price = float(unit_prices[i] or 0)
            if qty <= 0:
                raise ValueError("All quantities must be greater than zero.")
            lines.append((int(item_ids[i]), qty, price))

        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    total = sum(q * p for _, q, p in lines)
                    cur.execute(
                        """
                        INSERT INTO purchase_orders
                            (supplier_id, status, order_date, total_amount)
                        VALUES (%s, 'OPEN', CURRENT_DATE, %s)
                        RETURNING po_id
                        """,
                        (supplier_id, total),
                    )
                    po_id = cur.fetchone()[0]
                    for item_id, qty, price in lines:
                        cur.execute(
                            """
                            INSERT INTO po_items (po_id, item_id, quantity, unit_price)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (po_id, item_id, qty, price),
                        )
            flash(f"Purchase Order PO-{po_id} created with {len(lines)} line(s).")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not create purchase order: {e}")
    return redirect(url_for("purchasing"))


@app.route("/receive-po", methods=["POST"])
@login_required(role="Store Manager")
def receive_po():
    try:
        po_id = int(request.form["po_id"])
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Check PO status and already-received quantities
                    cur.execute(
                        "SELECT status FROM purchase_orders WHERE po_id = %s",
                        (po_id,),
                    )
                    po_row = cur.fetchone()
                    if not po_row:
                        raise ValueError("Purchase order not found.")
                    if po_row[0] == "RECEIVED":
                        raise ValueError("This purchase order is already fully received.")

                    cur.execute(
                        """
                        SELECT pi.po_item_id, pi.item_id, pi.quantity,
                               pi.unit_price,
                               COALESCE(SUM(gi.quantity), 0) AS received_qty
                        FROM po_items pi
                        LEFT JOIN grn_items gi ON gi.item_id = pi.item_id
                            AND gi.grn_id IN (
                                SELECT grn_id FROM goods_receipt_notes WHERE po_id = %s
                            )
                        WHERE pi.po_id = %s
                        GROUP BY pi.po_item_id, pi.item_id, pi.quantity, pi.unit_price
                        """,
                        (po_id, po_id),
                    )
                    po_lines = cur.fetchall()
                    if not po_lines:
                        raise ValueError("This PO has no line items.")

                    # Create GRN header
                    cur.execute(
                        "INSERT INTO goods_receipt_notes (po_id, received_at) "
                        "VALUES (%s, CURRENT_TIMESTAMP) RETURNING grn_id",
                        (po_id,),
                    )
                    grn_id = cur.fetchone()[0]
                    location_id = default_location(cur)

                    # Determine quantity to receive per line
                    # If the form provides per-line qty use it, else receive remainder.
                    form_qty = request.form.getlist("receive_qty[]")
                    form_items = request.form.getlist("receive_item_id[]")

                    total_received_possible = False
                    if form_items:
                        qty_map = {
                            int(form_items[i]): float(form_qty[i])
                            for i in range(len(form_items))
                        }
                    else:
                        qty_map = None

                    any_received = False
                    all_full = True
                    for po_item_id, item_id, ordered, unit_price, received in po_lines:
                        remaining = ordered - received
                        if remaining <= 0:
                            continue
                        if qty_map is not None:
                            recv_qty = qty_map.get(item_id, remaining)
                        else:
                            recv_qty = remaining
                        if recv_qty <= 0:
                            all_full = False
                            continue
                        recv_qty = min(recv_qty, remaining)
                        heat_lot = request.form.get(f"heat_{item_id}", "").strip() or None
                        mtc_ref = request.form.get(f"mtc_{item_id}", "").strip() or None

                        cur.execute(
                            """
                            INSERT INTO grn_items (grn_id, item_id, quantity,
                                                   heat_lot_no, mtc_reference)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (grn_id, item_id, recv_qty, heat_lot, mtc_ref),
                        )
                        add_stock(
                            cur, item_id, location_id, recv_qty, unit_price,
                            heat_lot, mtc_ref, "RECEIPT",
                            f"GRN-{grn_id} for PO-{po_id}",
                        )
                        any_received = True
                        if recv_qty < remaining:
                            all_full = False

                    if not any_received:
                        raise ValueError("Nothing to receive — all lines are complete.")

                    if all_full:
                        cur.execute(
                            "UPDATE purchase_orders SET status = 'RECEIVED' WHERE po_id = %s",
                            (po_id,),
                        )
                    else:
                        cur.execute(
                            "UPDATE purchase_orders SET status = 'PARTIALLY_RECEIVED' "
                            "WHERE po_id = %s",
                            (po_id,),
                        )
            flash(f"GRN-{grn_id} created for PO-{po_id}.")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not receive purchase order: {e}")
    return redirect(url_for("purchasing"))


@app.route("/generate-reorder-po", methods=["POST"])
@login_required(role="Store Manager")
def generate_reorder_po():
    """Generate draft purchase orders for items below reorder level."""
    try:
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT i.item_id, i.item_name, i.reorder_level,
                               COALESCE(SUM(s.quantity), 0) AS stock,
                               i.unit_cost
                        FROM items i
                        LEFT JOIN inventory_stock s ON s.item_id = i.item_id
                        WHERE i.item_type = 'RAW_MATERIAL'
                        GROUP BY i.item_id
                        HAVING COALESCE(SUM(s.quantity), 0) < i.reorder_level
                        """
                    )
                    low_items = cur.fetchall()
                    if not low_items:
                        flash("No raw materials are below reorder level.")
                        return redirect(url_for("purchasing"))

                    cur.execute("SELECT supplier_id FROM suppliers LIMIT 1")
                    sup = cur.fetchone()
                    if not sup:
                        flash("Add a supplier before generating reorder POs.")
                        return redirect(url_for("purchasing"))
                    supplier_id = sup[0]

                    for item_id, name, reorder, stock, unit_cost in low_items:
                        needed = reorder - stock
                        cur.execute(
                            """
                            INSERT INTO purchase_orders
                                (supplier_id, status, order_date, total_amount)
                            VALUES (%s, 'DRAFT', CURRENT_DATE, %s)
                            RETURNING po_id
                            """,
                            (supplier_id, needed * (unit_cost or 0)),
                        )
                        po_id = cur.fetchone()[0]
                        cur.execute(
                            """
                            INSERT INTO po_items (po_id, item_id, quantity, unit_price)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (po_id, item_id, needed, unit_cost or 0),
                        )
            flash(f"Generated {len(low_items)} draft reorder PO(s).")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not generate reorder POs: {e}")
    return redirect(url_for("purchasing"))


# ======================================================================
# MANUFACTURING
# ======================================================================
@app.route("/manufacturing")
@login_required
def manufacturing():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT item_id, item_code, item_name FROM items ORDER BY item_name")
            items = cur.fetchall()

            cur.execute("SELECT work_center_id, center_name, machine_type FROM work_centers ORDER BY center_name")
            work_centers = cur.fetchall()

            cur.execute(
                """
                SELECT b.bom_id, p.item_name AS parent, c.item_name AS component,
                       b.quantity_required, b.kerf_loss, b.scrap_factor
                FROM bill_of_materials b
                JOIN items p ON p.item_id = b.parent_item_id
                JOIN items c ON c.item_id = b.component_item_id
                ORDER BY p.item_name
                """
            )
            boms = cur.fetchall()

            cur.execute(
                """
                SELECT j.job_order_id, i.item_name, j.planned_quantity, j.status, j.created_at
                FROM job_orders j JOIN items i ON i.item_id = j.product_item_id
                ORDER BY j.job_order_id DESC
                """
            )
            job_orders = cur.fetchall()

            cur.execute(
                """
                SELECT r.routing_id, r.job_order_id, wc.center_name,
                       r.operation_sequence, r.status
                FROM job_order_routing r
                JOIN work_centers wc ON wc.work_center_id = r.work_center_id
                ORDER BY r.job_order_id, r.operation_sequence
                """
            )
            routings = cur.fetchall()

            cur.execute(
                """
                SELECT s.scrap_id, j.job_order_id, i.item_name, s.scrap_quantity,
                       s.recovery_value, s.notes, s.created_at
                FROM scrap_recovery_logs s
                JOIN job_orders j ON j.job_order_id = s.job_order_id
                JOIN items i ON i.item_id = s.item_id
                ORDER BY s.scrap_id DESC
                """
            )
            scrap_logs = cur.fetchall()
    finally:
        conn.close()
    return render_template(
        "manufacturing.html", items=items, work_centers=work_centers,
        boms=boms, job_orders=job_orders, routings=routings, scrap_logs=scrap_logs,
    )


@app.route("/add-bom", methods=["POST"])
@login_required(role="Production Manager")
def add_bom():
    try:
        parent_id = int(request.form["parent_item_id"])
        component_id = int(request.form["component_item_id"])
        qty = float(request.form["quantity_required"])
        kerf = float(request.form.get("kerf_loss") or 0)
        scrap_factor = float(request.form.get("scrap_factor") or 0)
        if parent_id == component_id:
            raise ValueError("Parent and component cannot be the same item.")
        if qty <= 0:
            raise ValueError("Quantity required must be greater than zero.")
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO bill_of_materials
                            (parent_item_id, component_item_id, quantity_required,
                             kerf_loss, scrap_factor)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (parent_id, component_id, qty, kerf, scrap_factor),
                    )
            flash("BOM entry added successfully.")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not add BOM entry: {e}")
    return redirect(url_for("manufacturing"))


@app.route("/create-job-order", methods=["POST"])
@login_required(role="Production Manager")
def create_job_order():
    try:
        product_id = int(request.form["product_item_id"])
        planned_qty = float(request.form["planned_quantity"])
        if planned_qty <= 0:
            raise ValueError("Planned quantity must be greater than zero.")
        work_center_ids = request.form.getlist("work_center_id[]")
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO job_orders
                            (product_item_id, planned_quantity, status, created_at)
                        VALUES (%s, %s, 'PLANNED', CURRENT_TIMESTAMP)
                        RETURNING job_order_id
                        """,
                        (product_id, planned_qty),
                    )
                    jo_id = cur.fetchone()[0]
                    for seq, wc_id in enumerate(work_center_ids, start=1):
                        if wc_id:
                            cur.execute(
                                """
                                INSERT INTO job_order_routing
                                    (job_order_id, work_center_id, operation_sequence, status)
                                VALUES (%s, %s, %s, 'PENDING')
                                """,
                                (jo_id, int(wc_id), seq),
                            )
            flash(f"Job Order JO-{jo_id} created.")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not create job order: {e}")
    return redirect(url_for("manufacturing"))


@app.route("/complete-job-order", methods=["POST"])
@login_required(role="Production Manager")
def complete_job_order():
    try:
        job_order_id = int(request.form["job_order_id"])
        scrap_quantity = float(request.form.get("scrap_quantity") or 0)
        recovery_value = float(request.form.get("recovery_value") or 0)
        scrap_notes = request.form.get("notes", "").strip()
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT product_item_id, planned_quantity, status "
                        "FROM job_orders WHERE job_order_id = %s",
                        (job_order_id,),
                    )
                    jo = cur.fetchone()
                    if not jo:
                        raise ValueError("Job order not found.")
                    product_item_id, planned_qty, status = jo
                    if status == "COMPLETED":
                        raise ValueError("Job order already completed.")

                    cur.execute(
                        "SELECT component_item_id, quantity_required, kerf_loss, scrap_factor "
                        "FROM bill_of_materials WHERE parent_item_id = %s",
                        (product_item_id,),
                    )
                    components = cur.fetchall()
                    if not components:
                        raise ValueError(
                            "No BOM defined for this product. Add a BOM first."
                        )

                    location_id = default_location(cur)

                    # Consume each component: needed = planned * (qty + kerf) * (1 + scrap_factor)
                    for comp_id, qty_req, kerf, scrap_f in components:
                        needed = planned_qty * (qty_req + (kerf or 0)) * (1 + (scrap_f or 0))
                        needed = round(needed, 3)
                        remove_stock(
                            cur, comp_id, location_id, needed,
                            "CONSUMPTION", f"Job Order JO-{job_order_id}",
                        )

                    # Produce finished goods
                    add_stock(
                        cur, product_item_id, location_id, planned_qty,
                        0, None, None, "PRODUCTION", f"Job Order JO-{job_order_id}",
                    )

                    # Scrap recovery
                    if scrap_quantity > 0:
                        cur.execute(
                            """
                            INSERT INTO scrap_recovery_logs
                                (job_order_id, item_id, scrap_quantity, recovery_value, notes)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (job_order_id, product_item_id, scrap_quantity,
                             recovery_value, scrap_notes),
                        )

                    # Mark routing operations complete
                    cur.execute(
                        "UPDATE job_order_routing SET status = 'COMPLETED' "
                        "WHERE job_order_id = %s",
                        (job_order_id,),
                    )

                    cur.execute(
                        "UPDATE job_orders SET status = 'COMPLETED' WHERE job_order_id = %s",
                        (job_order_id,),
                    )
            flash(
                f"Job Order JO-{job_order_id} completed. "
                f"Produced {planned_qty} units."
            )
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not complete job order: {e}")
    return redirect(url_for("manufacturing"))


# ======================================================================
# CUSTOMERS & SALES ORDERS
# ======================================================================
@app.route("/customers")
@login_required
def customers():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM customers ORDER BY customer_name")
            customers_list = cur.fetchall()

            cur.execute("SELECT item_id, item_code, item_name FROM items ORDER BY item_name")
            items = cur.fetchall()

            cur.execute(
                """
                SELECT so.sales_order_id, c.customer_name, so.status, so.order_date,
                       so.total_amount
                FROM sales_orders so
                JOIN customers c ON c.customer_id = so.customer_id
                ORDER BY so.sales_order_id DESC
                """
            )
            sales_orders = cur.fetchall()

            cur.execute(
                """
                SELECT soi.sales_order_id, i.item_name, soi.quantity, soi.unit_price
                FROM sales_order_items soi
                JOIN items i ON i.item_id = soi.item_id
                ORDER BY soi.sales_order_id
                """
            )
            so_items = cur.fetchall()
    finally:
        conn.close()
    return render_template(
        "customers.html", customers=customers_list, items=items,
        sales_orders=sales_orders, so_items=so_items,
    )


@app.route("/add-customer", methods=["POST"])
@login_required(role="Store Manager")
def add_customer():
    name = request.form.get("customer_name", "").strip()
    if not name:
        flash("Customer name is required.")
        return redirect(url_for("customers"))
    contact = request.form.get("contact_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    address = request.form.get("address", "").strip()
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO customers (customer_name, contact_name, phone, email, address)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (name, contact, phone, email, address),
                )
        flash("Customer added successfully.")
    except Exception as e:
        flash(f"Could not add customer: {e}")
    finally:
        conn.close()
    return redirect(url_for("customers"))


@app.route("/create-sales-order", methods=["POST"])
@login_required(role="Store Manager")
def create_sales_order():
    try:
        customer_id = int(request.form["customer_id"])
        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity[]")
        unit_prices = request.form.getlist("unit_price[]")
        if not item_ids:
            raise ValueError("At least one line item is required.")
        lines = []
        for i in range(len(item_ids)):
            qty = float(quantities[i])
            price = float(unit_prices[i] or 0)
            if qty <= 0:
                raise ValueError("All quantities must be greater than zero.")
            lines.append((int(item_ids[i]), qty, price))
        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    total = sum(q * p for _, q, p in lines)
                    cur.execute(
                        """
                        INSERT INTO sales_orders (customer_id, status, order_date, total_amount)
                        VALUES (%s, 'PENDING', CURRENT_DATE, %s)
                        RETURNING sales_order_id
                        """,
                        (customer_id, total),
                    )
                    so_id = cur.fetchone()[0]
                    for item_id, qty, price in lines:
                        cur.execute(
                            """
                            INSERT INTO sales_order_items
                                (sales_order_id, item_id, quantity, unit_price)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (so_id, item_id, qty, price),
                        )
            flash(f"Sales Order SO-{so_id} created with {len(lines)} line(s).")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not create sales order: {e}")
    return redirect(url_for("customers"))


# ======================================================================
# DISPATCH
# ======================================================================
@app.route("/dispatch")
@login_required
def dispatch_page():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT so.sales_order_id, c.customer_name, so.status,
                       so.total_amount
                FROM sales_orders so
                JOIN customers c ON c.customer_id = so.customer_id
                WHERE so.status <> 'DISPATCHED'
                ORDER BY so.sales_order_id DESC
                """
            )
            open_sos = cur.fetchall()

            cur.execute(
                """
                SELECT soi.sales_order_id, soi.item_id, i.item_name,
                       soi.quantity, soi.unit_price
                FROM sales_order_items soi
                JOIN items i ON i.item_id = soi.item_id
                ORDER BY soi.sales_order_id
                """
            )
            so_items = cur.fetchall()

            cur.execute(
                """
                SELECT d.challan_id, d.sales_order_id, c.customer_name,
                       d.dispatched_at, d.warranty_months
                FROM dispatch_challans d
                JOIN sales_orders so ON so.sales_order_id = d.sales_order_id
                JOIN customers c ON c.customer_id = so.customer_id
                ORDER BY d.challan_id DESC
                """
            )
            challans = cur.fetchall()

            cur.execute(
                """
                SELECT di.challan_id, i.item_name, di.quantity,
                       di.serial_number, di.warranty_months
                FROM dispatch_items di
                JOIN items i ON i.item_id = di.item_id
                ORDER BY di.challan_id
                """
            )
            challan_items = cur.fetchall()
    finally:
        conn.close()
    return render_template(
        "dispatch.html", open_sos=open_sos, so_items=so_items,
        challans=challans, challan_items=challan_items,
    )


@app.route("/create-dispatch", methods=["POST"])
@login_required(role="Store Manager")
def create_dispatch():
    try:
        sales_order_id = int(request.form["sales_order_id"])
        warranty = int(request.form.get("warranty_months") or 0)
        item_ids = request.form.getlist("item_id[]")
        quantities = request.form.getlist("quantity[]")
        serials = request.form.getlist("serial_number[]")

        conn = get_db()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status FROM sales_orders WHERE sales_order_id = %s",
                        (sales_order_id,),
                    )
                    so = cur.fetchone()
                    if not so:
                        raise ValueError("Sales order not found.")
                    if so[0] == "DISPATCHED":
                        raise ValueError("This sales order is already fully dispatched.")

                    location_id = default_location(cur)
                    cur.execute(
                        "INSERT INTO dispatch_challans (sales_order_id, warranty_months) "
                        "VALUES (%s, %s) RETURNING challan_id",
                        (sales_order_id, warranty),
                    )
                    challan_id = cur.fetchone()[0]

                    for i in range(len(item_ids)):
                        item_id = int(item_ids[i])
                        qty = float(quantities[i])
                        serial = serials[i].strip() if i < len(serials) else ""
                        if qty <= 0:
                            continue
                        remove_stock(
                            cur, item_id, location_id, qty,
                            "DISPATCH", f"Dispatch Challan DC-{challan_id}",
                        )
                        cur.execute(
                            """
                            INSERT INTO dispatch_items
                                (challan_id, item_id, quantity, serial_number, warranty_months)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (challan_id, item_id, qty, serial or None, warranty),
                        )

                    # Check if sales order is fully dispatched
                    cur.execute(
                        """
                        SELECT
                          (SELECT COALESCE(SUM(quantity),0) FROM sales_order_items
                           WHERE sales_order_id = %s),
                          (SELECT COALESCE(SUM(di.quantity),0) FROM dispatch_items di
                           JOIN dispatch_challans dc ON dc.challan_id = di.challan_id
                           WHERE dc.sales_order_id = %s)
                        """,
                        (sales_order_id, sales_order_id),
                    )
                    ordered, dispatched = cur.fetchone()
                    if dispatched >= ordered:
                        cur.execute(
                            "UPDATE sales_orders SET status = 'DISPATCHED' "
                            "WHERE sales_order_id = %s",
                            (sales_order_id,),
                        )
                    else:
                        cur.execute(
                            "UPDATE sales_orders SET status = 'PARTIALLY_DISPATCHED' "
                            "WHERE sales_order_id = %s",
                            (sales_order_id,),
                        )
            flash(f"Dispatch Challan DC-{challan_id} created.")
        finally:
            conn.close()
    except Exception as e:
        flash(f"Could not create dispatch: {e}")
    return redirect(url_for("dispatch_page"))


# ======================================================================
# REPORTS
# ======================================================================
@app.route("/reports")
@login_required
def reports():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Inventory valuation: weighted average
            cur.execute(
                """
                SELECT i.item_id, i.item_code, i.item_name, i.item_type, i.unit,
                       COALESCE(SUM(s.quantity), 0) AS stock,
                       COALESCE(SUM(s.quantity * s.unit_cost), 0) AS wa_value
                FROM items i
                LEFT JOIN inventory_stock s ON s.item_id = i.item_id
                GROUP BY i.item_id
                ORDER BY i.item_name
                """
            )
            valuation = cur.fetchall()

            # FIFO layers
            cur.execute(
                """
                SELECT b.batch_id, i.item_name, b.remaining_quantity, b.unit_cost,
                       b.heat_lot_no, b.mtc_reference, b.created_at
                FROM stock_batches b
                JOIN items i ON i.item_id = b.item_id
                WHERE b.remaining_quantity > 0
                ORDER BY b.item_id, b.created_at
                """
            )
            fifo_layers = cur.fetchall()

            # Full transaction history
            cur.execute(
                """
                SELECT t.transaction_id, i.item_name, t.transaction_type, t.quantity,
                       t.unit_cost, t.heat_lot_no, t.mtc_reference, t.reference_note, t.created_at
                FROM stock_transactions t
                JOIN items i ON i.item_id = t.item_id
                ORDER BY t.transaction_id DESC
                LIMIT 100
                """
            )
            transactions = cur.fetchall()

            # Scrap summary
            cur.execute(
                """
                SELECT s.scrap_id, j.job_order_id, i.item_name, s.scrap_quantity,
                       s.recovery_value, s.created_at
                FROM scrap_recovery_logs s
                JOIN job_orders j ON j.job_order_id = s.job_order_id
                JOIN items i ON i.item_id = s.item_id
                ORDER BY s.scrap_id DESC
                """
            )
            scrap_logs = cur.fetchall()

            # Sales summary
            cur.execute(
                """
                SELECT so.sales_order_id, c.customer_name, so.status, so.total_amount,
                       so.order_date
                FROM sales_orders so
                JOIN customers c ON c.customer_id = so.customer_id
                ORDER BY so.sales_order_id DESC
                """
            )
            sales_summary = cur.fetchall()

            # Totals
            cur.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales_orders")
            total_sales = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(recovery_value),0) FROM scrap_recovery_logs")
            total_scrap_recovery = cur.fetchone()[0]
            cur.execute(
                "SELECT COALESCE(SUM(quantity * unit_cost),0) FROM inventory_stock"
            )
            total_inventory_value = cur.fetchone()[0]
    finally:
        conn.close()
    return render_template(
        "reports.html",
        valuation=valuation, fifo_layers=fifo_layers,
        transactions=transactions, scrap_logs=scrap_logs,
        sales_summary=sales_summary,
        total_sales=total_sales, total_scrap_recovery=total_scrap_recovery,
        total_inventory_value=total_inventory_value,
    )


# ======================================================================
# Startup
# ======================================================================
if __name__ == "__main__":
    ensure_admin_account()
    app.run(host="127.0.0.1", port=5000, debug=True)
