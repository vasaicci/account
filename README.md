# Stockroom – Sales & Inventory Management (Flask)

A Zoho-Inventory-style app: items & stock, customers, vendors, sales orders,
invoices with payments, purchase orders with receiving, dashboard and reports.

## Run
```bash
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
flask --app app seed-demo      # optional: sample items, customers, vendors
flask --app app run --debug
```
Open http://127.0.0.1:5000 and log in with **admin / admin123** (change it, see below).

## Workflow
- **Sales:** Sales Order (draft → confirmed) → *Convert to invoice* (stock deducted) → *Record payment*.
  Invoices can also be created directly. Voiding an invoice returns stock.
- **Purchases:** Purchase Order (draft → issued) → *Receive stock* (stock added, cost price updated).
- **Stock:** every change is logged in the item's stock history. Use *Adjust stock* for counts/damage.
- **Reports:** monthly sales vs purchases, top items, receivables ageing, inventory valuation, CSV exports.

## Config (environment variables)
- `SECRET_KEY` – set to a long random string in production
- `DATABASE_URL` – e.g. `postgresql://user:pass@host/db` (default: SQLite file `instance/inventory.db`)
- `ADMIN_PASSWORD` – initial admin password (used only when the DB is first created)

## Deploy
`pip install gunicorn && gunicorn app:app`

## Tests
`python -m pytest tests` (or `python tests/test_smoke.py`)
