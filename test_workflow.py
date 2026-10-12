"""End-to-end workflow test for IMS-Mech."""
import requests

BASE = "http://127.0.0.1:5000"
s = requests.Session()

def check(step, resp, expected_status=200, contains=None):
    ok = resp.status_code == expected_status
    if contains and contains not in resp.text:
        ok = False
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {step} (status={resp.status_code})")
    if not ok:
        if contains:
            print(f"       expected to contain: {contains!r}")
    return ok

# 1. Login
r = s.post(f"{BASE}/login", data={"username": "admin", "password": "admin123"})
check("Login", r, 302)

# 2. Dashboard loads
r = s.get(f"{BASE}/")
check("Dashboard", r, 200, "Dashboard")

# 3. Add items (raw material + finished good)
r = s.post(f"{BASE}/add-item", data={
    "item_code": "STEEL-EN8", "item_name": "EN8 Steel Round Bar",
    "item_type": "RAW_MATERIAL", "unit": "kg", "reorder_level": "50",
    "unit_cost": "80", "description": "Carbon steel"})
check("Add raw material item", r, 302)

r = s.post(f"{BASE}/add-item", data={
    "item_code": "SHAFT-001", "item_name": "Drive Shaft",
    "item_type": "FINISHED_GOOD", "unit": "pcs", "reorder_level": "5",
    "unit_cost": "0", "description": "Finished drive shaft"})
check("Add finished good item", r, 302)

# 4. Add supplier
r = s.post(f"{BASE}/add-supplier", data={
    "name": "SteelMart Ltd", "contact_name": "Ravi", "phone": "9876543210",
    "email": "ravi@steelmart.com", "address": "Pune"})
check("Add supplier", r, 302)

# 5. Create PO with single line (find item and supplier ids from inventory page)
r = s.get(f"{BASE}/inventory")
import re
steel_id = re.search(r'value="(\d+)"[^>]*>STEEL-EN8', r.text)
steel_id = steel_id.group(1) if steel_id else "1"
shaft_id = re.search(r'value="(\d+)"[^>]*>SHAFT-001', r.text)
shaft_id = shaft_id.group(1) if shaft_id else "2"

r = s.get(f"{BASE}/suppliers")
sup_id = re.search(r'<td>(\d+)</td>', r.text)
sup_id = sup_id.group(1) if sup_id else "1"

r = s.post(f"{BASE}/create-po", data={
    "supplier_id": sup_id,
    "item_id[]": [steel_id],
    "quantity[]": ["100"],
    "unit_price[]": ["80"],
})
check("Create purchase order", r, 302)

# 6. Receive PO (find po id from purchasing page)
r = s.get(f"{BASE}/purchasing")
po_id = re.search(r'PO-(\d+)', r.text)
po_id = po_id.group(1) if po_id else "1"

r = s.post(f"{BASE}/receive-po", data={"po_id": po_id})
check("Receive purchase order (GRN)", r, 302)

# 7. Verify stock increased
r = s.get(f"{BASE}/inventory")
check("Stock after GRN", r, 200, "100")

# 8. Define BOM (shaft needs 2 kg steel)
r = s.post(f"{BASE}/add-bom", data={
    "parent_item_id": shaft_id, "component_item_id": steel_id,
    "quantity_required": "2", "kerf_loss": "0.1", "scrap_factor": "0.05"})
check("Add BOM", r, 302)

# 9. Create job order for 10 shafts
r = s.post(f"{BASE}/create-job-order", data={
    "product_item_id": shaft_id, "planned_quantity": "10",
    "work_center_id[]": ["1", "2", "3"]})
check("Create job order", r, 302)

# 10. Complete job order (consume steel, produce shafts)
r = s.get(f"{BASE}/manufacturing")
jo_id = re.search(r'JO-(\d+)', r.text)
jo_id = jo_id.group(1) if jo_id else "1"

r = s.post(f"{BASE}/complete-job-order", data={
    "job_order_id": jo_id, "scrap_quantity": "0.5",
    "recovery_value": "10", "notes": "End cuts"})
check("Complete job order", r, 302)

# 11. Verify finished goods stock = 10
r = s.get(f"{BASE}/inventory")
check("Finished goods stock", r, 200, "10.00")

# 12. Verify raw material consumed (100 - 10*2.1*1.05 = 100 - 22.05 = 77.95)
check("Raw material consumed", r, 200, "77.95")

# 13. Prevent double completion
r = s.post(f"{BASE}/complete-job-order", data={
    "job_order_id": jo_id, "scrap_quantity": "0"}, allow_redirects=True)
check("Prevent double completion", r, 200, "already completed")

# 14. Add customer
r = s.post(f"{BASE}/add-customer", data={
    "customer_name": "AutoParts Co", "contact_name": "Amit",
    "phone": "9123456780", "email": "amit@autoparts.com", "address": "Mumbai"})
check("Add customer", r, 302)

# 15. Create sales order
r = s.get(f"{BASE}/customers")
cust_id = re.search(r'<td>(\d+)</td>', r.text)
cust_id = cust_id.group(1) if cust_id else "1"

r = s.post(f"{BASE}/create-sales-order", data={
    "customer_id": cust_id,
    "item_id[]": [shaft_id],
    "quantity[]": ["5"],
    "unit_price[]": ["500"],
})
check("Create sales order", r, 302)

# 16. Dispatch
r = s.get(f"{BASE}/dispatch")
so_id = re.search(r'SO-(\d+)', r.text)
so_id = so_id.group(1) if so_id else "1"

r = s.post(f"{BASE}/create-dispatch", data={
    "sales_order_id": so_id, "warranty_months": "12",
    "item_id[]": [shaft_id],
    "quantity[]": ["5"],
    "serial_number[]": ["SN-001"],
})
check("Create dispatch challan", r, 302)

# 17. Verify stock reduced to 5
r = s.get(f"{BASE}/inventory")
check("Stock after dispatch", r, 200, "5.00")

# 18. Reports page
r = s.get(f"{BASE}/reports")
check("Reports page", r, 200, "Valuation")

# 19. Transaction history
check("Transaction history", r, 200, "Stock Movement")

# 20. FIFO layers present
check("FIFO layers", r, 200, "FIFO")

print("\n=== Workflow test complete ===")
