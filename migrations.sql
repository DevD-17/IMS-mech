-- =====================================================================
-- IMS-Mech - Migration script for EXISTING databases.
-- Run after the original starter schema.sql. Safe to run multiple times.
-- psql -U postgres -d ims_mech -f migrations.sql
-- =====================================================================

-- Helper to add a column only if it does not already exist -------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='description') THEN
    ALTER TABLE items ADD COLUMN description TEXT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='unit_cost') THEN
    ALTER TABLE items ADD COLUMN unit_cost NUMERIC(12,2) DEFAULT 0;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='inventory_stock' AND column_name='unit_cost') THEN
    ALTER TABLE inventory_stock ADD COLUMN unit_cost NUMERIC(12,2) DEFAULT 0;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='stock_transactions' AND column_name='unit_cost') THEN
    ALTER TABLE stock_transactions ADD COLUMN unit_cost NUMERIC(12,2);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='stock_transactions' AND column_name='heat_lot_no') THEN
    ALTER TABLE stock_transactions ADD COLUMN heat_lot_no VARCHAR(80);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='stock_transactions' AND column_name='mtc_reference') THEN
    ALTER TABLE stock_transactions ADD COLUMN mtc_reference VARCHAR(120);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='raw_material_specs' AND column_name='heat_lot_no') THEN
    ALTER TABLE raw_material_specs ADD COLUMN heat_lot_no VARCHAR(80);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='raw_material_specs' AND column_name='mtc_reference') THEN
    ALTER TABLE raw_material_specs ADD COLUMN mtc_reference VARCHAR(120);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='grn_items' AND column_name='heat_lot_no') THEN
    ALTER TABLE grn_items ADD COLUMN heat_lot_no VARCHAR(80);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='grn_items' AND column_name='mtc_reference') THEN
    ALTER TABLE grn_items ADD COLUMN mtc_reference VARCHAR(120);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='customers' AND column_name='email') THEN
    ALTER TABLE customers ADD COLUMN email VARCHAR(120);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='customers' AND column_name='contact_name') THEN
    ALTER TABLE customers ADD COLUMN contact_name VARCHAR(80);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sales_orders' AND column_name='expected_date') THEN
    ALTER TABLE sales_orders ADD COLUMN expected_date DATE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sales_orders' AND column_name='total_amount') THEN
    ALTER TABLE sales_orders ADD COLUMN total_amount NUMERIC(14,2) DEFAULT 0;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='purchase_orders' AND column_name='expected_date') THEN
    ALTER TABLE purchase_orders ADD COLUMN expected_date DATE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='purchase_orders' AND column_name='total_amount') THEN
    ALTER TABLE purchase_orders ADD COLUMN total_amount NUMERIC(14,2) DEFAULT 0;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='dispatch_items' AND column_name='warranty_months') THEN
    ALTER TABLE dispatch_items ADD COLUMN warranty_months INT DEFAULT 0;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='dispatch_challans' AND column_name='dispatched_by') THEN
    ALTER TABLE dispatch_challans ADD COLUMN dispatched_by VARCHAR(80);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='scrap_recovery_logs' AND column_name='notes') THEN
    ALTER TABLE scrap_recovery_logs ADD COLUMN notes TEXT;
  END IF;
END $$;

-- stock_batches table for FIFO valuation ------------------------------
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

-- Indexes -------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_stock_item           ON inventory_stock(item_id);
CREATE INDEX IF NOT EXISTS idx_tx_item              ON stock_transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_tx_type              ON stock_transactions(transaction_type);
CREATE INDEX IF NOT EXISTS idx_batches_item         ON stock_batches(item_id);
CREATE INDEX IF NOT EXISTS idx_batches_remaining    ON stock_batches(remaining_quantity) WHERE remaining_quantity > 0;
CREATE INDEX IF NOT EXISTS idx_po_status            ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS idx_so_status            ON sales_orders(status);
CREATE INDEX IF NOT EXISTS idx_jo_status            ON job_orders(status);
CREATE INDEX IF NOT EXISTS idx_bom_parent           ON bill_of_materials(parent_item_id);

-- Seed work centers ---------------------------------------------------
INSERT INTO work_centers (center_name, machine_type) VALUES
    ('Sawing',        'Band Saw'),
    ('CNC Turning',   'CNC Lathe'),
    ('VMC Milling',   'VMC Machine'),
    ('Heat Treatment','Furnace'),
    ('Inspection',    'Quality Lab')
ON CONFLICT DO NOTHING;

-- Seed admin user (password: admin123) - updated by app on first login if placeholder
INSERT INTO users (username, full_name, password_hash, role_id, active)
SELECT 'admin', 'System Administrator',
       'pbkdf2:sha256:600000$placeholder$0000000000000000000000000000000000000000000000000000000000000000',
       role_id, TRUE
FROM roles WHERE role_name = 'Admin'
ON CONFLICT (username) DO NOTHING;
