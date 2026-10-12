-- =====================================================================
-- IMS-Mech  -  Mechanical Manufacturing Inventory Management System
-- Full schema for fresh installs.  23 core tables + valuation batches.
-- Run with:  psql -U postgres -d ims_mech -f schema.sql
-- =====================================================================

-- 1. Roles & users ----------------------------------------------------
CREATE TABLE IF NOT EXISTS roles (
    role_id    SERIAL PRIMARY KEY,
    role_name  VARCHAR(40) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id       SERIAL PRIMARY KEY,
    username      VARCHAR(80) UNIQUE NOT NULL,
    full_name     VARCHAR(120),
    password_hash TEXT,
    role_id       INT REFERENCES roles(role_id),
    active        BOOLEAN DEFAULT TRUE
);

-- 2. Suppliers --------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id  SERIAL PRIMARY KEY,
    name         VARCHAR(120) NOT NULL,
    contact_name VARCHAR(80),
    phone        VARCHAR(30),
    email        VARCHAR(120),
    address      TEXT
);

-- 3. Items & raw-material specs --------------------------------------
CREATE TABLE IF NOT EXISTS items (
    item_id       SERIAL PRIMARY KEY,
    item_code     VARCHAR(40) UNIQUE NOT NULL,
    item_name     VARCHAR(120) NOT NULL,
    item_type     VARCHAR(30) DEFAULT 'RAW_MATERIAL',
    unit          VARCHAR(20) DEFAULT 'pcs',
    reorder_level NUMERIC(12,2) DEFAULT 10,
    description   TEXT,
    unit_cost     NUMERIC(12,2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_material_specs (
    spec_id        SERIAL PRIMARY KEY,
    item_id        INT UNIQUE REFERENCES items(item_id),
    grade          VARCHAR(80),
    standard_name  VARCHAR(80),
    dimensions     VARCHAR(120),
    heat_lot_no    VARCHAR(80),
    mtc_reference  VARCHAR(120)
);

-- 4. Warehouses & locations ------------------------------------------
CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id   SERIAL PRIMARY KEY,
    warehouse_name VARCHAR(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_locations (
    location_id   SERIAL PRIMARY KEY,
    warehouse_id  INT REFERENCES warehouses(warehouse_id),
    location_code VARCHAR(40) NOT NULL,
    UNIQUE(warehouse_id, location_code)
);

-- 5. Inventory stock (weighted-average cost stored in unit_cost) -----
CREATE TABLE IF NOT EXISTS inventory_stock (
    stock_id    SERIAL PRIMARY KEY,
    item_id     INT REFERENCES items(item_id),
    location_id INT REFERENCES storage_locations(location_id),
    quantity    NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    unit_cost   NUMERIC(12,2) DEFAULT 0,
    UNIQUE(item_id, location_id)
);

-- 6. Bill of materials ------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_of_materials (
    bom_id            SERIAL PRIMARY KEY,
    parent_item_id    INT REFERENCES items(item_id),
    component_item_id INT REFERENCES items(item_id),
    quantity_required NUMERIC(12,3) NOT NULL,
    kerf_loss         NUMERIC(12,3) DEFAULT 0,
    scrap_factor      NUMERIC(8,4) DEFAULT 0
);

-- 7. Work centers & routing ------------------------------------------
CREATE TABLE IF NOT EXISTS work_centers (
    work_center_id SERIAL PRIMARY KEY,
    center_name    VARCHAR(100) NOT NULL,
    machine_type   VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS job_orders (
    job_order_id     SERIAL PRIMARY KEY,
    product_item_id  INT REFERENCES items(item_id),
    planned_quantity NUMERIC(12,2) NOT NULL,
    status           VARCHAR(30) DEFAULT 'PLANNED',
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_order_routing (
    routing_id         SERIAL PRIMARY KEY,
    job_order_id       INT REFERENCES job_orders(job_order_id),
    work_center_id     INT REFERENCES work_centers(work_center_id),
    operation_sequence INT NOT NULL,
    status             VARCHAR(30) DEFAULT 'PENDING'
);

-- 8. Scrap recovery --------------------------------------------------
CREATE TABLE IF NOT EXISTS scrap_recovery_logs (
    scrap_id        SERIAL PRIMARY KEY,
    job_order_id    INT REFERENCES job_orders(job_order_id),
    item_id         INT REFERENCES items(item_id),
    scrap_quantity  NUMERIC(12,2) NOT NULL,
    recovery_value  NUMERIC(12,2) DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 9. Purchasing ------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id        SERIAL PRIMARY KEY,
    supplier_id  INT REFERENCES suppliers(supplier_id),
    status       VARCHAR(30) DEFAULT 'DRAFT',
    order_date   DATE DEFAULT CURRENT_DATE,
    expected_date DATE,
    total_amount NUMERIC(14,2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS po_items (
    po_item_id  SERIAL PRIMARY KEY,
    po_id       INT REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    item_id     INT REFERENCES items(item_id),
    quantity    NUMERIC(12,2) NOT NULL,
    unit_price  NUMERIC(12,2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS goods_receipt_notes (
    grn_id       SERIAL PRIMARY KEY,
    po_id        INT REFERENCES purchase_orders(po_id),
    received_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    received_by  VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS grn_items (
    grn_item_id   SERIAL PRIMARY KEY,
    grn_id        INT REFERENCES goods_receipt_notes(grn_id) ON DELETE CASCADE,
    item_id       INT REFERENCES items(item_id),
    quantity      NUMERIC(12,2) NOT NULL,
    heat_lot_no   VARCHAR(80),
    mtc_reference VARCHAR(120)
);

-- 10. Customers & sales ----------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    customer_id   SERIAL PRIMARY KEY,
    customer_name VARCHAR(120) NOT NULL,
    contact_name  VARCHAR(80),
    phone         VARCHAR(30),
    email         VARCHAR(120),
    address       TEXT
);

CREATE TABLE IF NOT EXISTS sales_orders (
    sales_order_id SERIAL PRIMARY KEY,
    customer_id    INT REFERENCES customers(customer_id),
    status         VARCHAR(30) DEFAULT 'PENDING',
    order_date     DATE DEFAULT CURRENT_DATE,
    expected_date  DATE,
    total_amount   NUMERIC(14,2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sales_order_items (
    sales_order_item_id SERIAL PRIMARY KEY,
    sales_order_id      INT REFERENCES sales_orders(sales_order_id) ON DELETE CASCADE,
    item_id             INT REFERENCES items(item_id),
    quantity            NUMERIC(12,2) NOT NULL,
    unit_price          NUMERIC(12,2) DEFAULT 0
);

-- 11. Dispatch -------------------------------------------------------
CREATE TABLE IF NOT EXISTS dispatch_challans (
    challan_id      SERIAL PRIMARY KEY,
    sales_order_id  INT REFERENCES sales_orders(sales_order_id),
    dispatched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    warranty_months INT DEFAULT 0,
    dispatched_by   VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS dispatch_items (
    dispatch_item_id SERIAL PRIMARY KEY,
    challan_id       INT REFERENCES dispatch_challans(challan_id) ON DELETE CASCADE,
    item_id          INT REFERENCES items(item_id),
    quantity         NUMERIC(12,2) NOT NULL,
    serial_number    VARCHAR(100),
    warranty_months  INT DEFAULT 0
);

-- 12. Stock transactions ---------------------------------------------
CREATE TABLE IF NOT EXISTS stock_transactions (
    transaction_id  SERIAL PRIMARY KEY,
    item_id         INT REFERENCES items(item_id),
    location_id     INT REFERENCES storage_locations(location_id),
    transaction_type VARCHAR(20) NOT NULL,
    quantity        NUMERIC(12,2) NOT NULL,
    unit_cost       NUMERIC(12,2),
    reference_note  TEXT,
    heat_lot_no     VARCHAR(80),
    mtc_reference   VARCHAR(120),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 13. FIFO valuation batches -----------------------------------------
CREATE TABLE IF NOT EXISTS stock_batches (
    batch_id          SERIAL PRIMARY KEY,
    item_id           INT REFERENCES items(item_id),
    transaction_id    INT REFERENCES stock_transactions(transaction_id),
    grn_item_id       INT REFERENCES grn_items(grn_item_id),
    original_quantity NUMERIC(12,2) NOT NULL,
    remaining_quantity NUMERIC(12,2) NOT NULL,
    unit_cost         NUMERIC(12,2) DEFAULT 0,
    heat_lot_no       VARCHAR(80),
    mtc_reference     VARCHAR(120),
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (remaining_quantity >= 0)
);

-- Indexes for performance --------------------------------------------
CREATE INDEX IF NOT EXISTS idx_stock_item           ON inventory_stock(item_id);
CREATE INDEX IF NOT EXISTS idx_tx_item              ON stock_transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_tx_type              ON stock_transactions(transaction_type);
CREATE INDEX IF NOT EXISTS idx_batches_item         ON stock_batches(item_id);
CREATE INDEX IF NOT EXISTS idx_batches_remaining    ON stock_batches(remaining_quantity) WHERE remaining_quantity > 0;
CREATE INDEX IF NOT EXISTS idx_po_status            ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS idx_so_status            ON sales_orders(status);
CREATE INDEX IF NOT EXISTS idx_jo_status            ON job_orders(status);
CREATE INDEX IF NOT EXISTS idx_bom_parent           ON bill_of_materials(parent_item_id);

-- =====================================================================
-- Seed data (idempotent)
-- =====================================================================
INSERT INTO roles (role_name)
VALUES ('Admin'), ('Store Manager'), ('Production Manager')
ON CONFLICT (role_name) DO NOTHING;

INSERT INTO warehouses (warehouse_name)
SELECT 'Main Warehouse'
WHERE NOT EXISTS (SELECT 1 FROM warehouses WHERE warehouse_name = 'Main Warehouse');

INSERT INTO storage_locations (warehouse_id, location_code)
SELECT warehouse_id, 'A-01' FROM warehouses WHERE warehouse_name = 'Main Warehouse'
ON CONFLICT (warehouse_id, location_code) DO NOTHING;

INSERT INTO work_centers (center_name, machine_type) VALUES
    ('Sawing',        'Band Saw'),
    ('CNC Turning',   'CNC Lathe'),
    ('VMC Milling',   'VMC Machine'),
    ('Heat Treatment','Furnace'),
    ('Inspection',    'Quality Lab')
ON CONFLICT DO NOTHING;

-- Seed an admin user (password: admin123). Change this in production.
-- Hash generated with werkzeug: generate_password_hash('admin123')
INSERT INTO users (username, full_name, password_hash, role_id, active)
SELECT 'admin', 'System Administrator',
       'pbkdf2:sha256:600000$placeholder$0000000000000000000000000000000000000000000000000000000000000000',
       role_id, TRUE
FROM roles WHERE role_name = 'Admin'
ON CONFLICT (username) DO NOTHING;
