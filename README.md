# IMS-Mech — Inventory Management System

A web-based Inventory Management System designed for mechanical manufacturing enterprises.

## Features

- Inventory management for raw materials, work-in-progress (WIP), and finished goods
- Supplier and purchase order management
- Bill of Materials (BOM) and manufacturing workflow tracking
- Customer orders, dispatch, and traceability
- Stock valuation and inventory transactions
- Role-based access for different users

## Technologies Used

- Python
- Flask
- PostgreSQL
- HTML, CSS
- SQL

## Run Locally

1. Clone or download this repository.
2. Open a terminal in the project folder.
3. Install the required Python packages.
4. Configure the required database and environment variables.
5. Run the application:

   ```bash
   python app.py
   ```

6. Open `http://localhost:5000/inventory` in your browser.

**Note:** The application requires its database and configuration to be set up correctly before it can run.

## Project Structure

- `app.py` — Flask application
- `templates/` — HTML templates
- `static/` — CSS and other static assets
- `schema.sql` — Database schema
- `migrations.sql` — Database migrations
- `test_workflow.py` — Workflow tests

---

Developed as a mechanical manufacturing inventory management project.
