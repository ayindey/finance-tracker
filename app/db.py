import sqlite3
import click
from flask import current_app, g


def get_db():
    """Open a new database connection if one doesn't already exist for this request."""
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row  # lets us access columns by name, e.g. row['amount']
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def close_db(e=None):
    """Close the database connection at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Create tables from schema.sql, then seed default categories/settings. Safe to run multiple times."""
    db = get_db()
    with current_app.open_resource('../schema.sql') as f:
        db.executescript(f.read().decode('utf8'))

    existing = db.execute('SELECT COUNT(*) AS c FROM categories').fetchone()['c']
    if existing == 0:
        expense_categories = ['Rent', 'Utilities', 'Supplies', 'Salaries', 'Transport', 'Marketing', 'Miscellaneous']
        income_categories = ['Sales', 'Services', 'Other Income']
        for name in expense_categories:
            db.execute('INSERT INTO categories (name, type) VALUES (?, ?)', (name, 'expense'))
        for name in income_categories:
            db.execute('INSERT INTO categories (name, type) VALUES (?, ?)', (name, 'income'))
        db.commit()

    migrate_db(db)
    seed_settings(db)


def seed_settings(db):
    defaults = {
        'default_password': 'welcome123',
        'kot_printer_ip': '',
        'kot_printer_port': '9100',
        'bill_printer_ip': '',
        'bill_printer_port': '9100',
    }
    for key, value in defaults.items():
        exists = db.execute('SELECT 1 FROM settings WHERE key = ?', (key,)).fetchone()
        if not exists:
            db.execute('INSERT INTO settings (key, value) VALUES (?, ?)', (key, value))

    # Carry forward the old single-printer setting (from before KOT/Bill were split)
    # so anyone who already configured a printer doesn't lose that on upgrade.
    old_ip = db.execute("SELECT value FROM settings WHERE key = 'printer_ip'").fetchone()
    if old_ip and old_ip['value']:
        old_port_row = db.execute("SELECT value FROM settings WHERE key = 'printer_port'").fetchone()
        old_port = old_port_row['value'] if old_port_row else '9100'
        for prefix in ('kot', 'bill'):
            current = db.execute(f"SELECT value FROM settings WHERE key = '{prefix}_printer_ip'").fetchone()
            if not current or not current['value']:
                db.execute(
                    'INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                    (f'{prefix}_printer_ip', old_ip['value'])
                )
                db.execute(
                    'INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                    (f'{prefix}_printer_port', old_port)
                )
        db.execute("DELETE FROM settings WHERE key IN ('printer_ip', 'printer_port')")

    db.commit()


def get_setting(key, default=None):
    db = get_db()
    row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, value)
    )
    db.commit()


def _rebuild_invoices_table(db):
    """Rebuild the invoices table to the current target schema, WITHOUT ever renaming
    the live 'invoices' table itself. Renaming the original table (even temporarily) is
    what caused a real bug earlier: SQLite silently rewrites other tables' FOREIGN KEY
    clauses to follow a renamed table, and if that renamed table is later dropped, the
    referencing tables are left pointing at nothing. Building the replacement under a new
    name and swapping it in at the very end avoids that failure class entirely."""
    db.execute('PRAGMA foreign_keys = OFF')
    db.execute('''CREATE TABLE invoices_rebuilt (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_name TEXT NOT NULL,
        client_contact TEXT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'sent', 'paid', 'void', 'cancelled')),
        date TEXT NOT NULL,
        discount_type TEXT CHECK(discount_type IN ('percentage', 'fixed') OR discount_type IS NULL),
        discount_value REAL NOT NULL DEFAULT 0,
        discounted_by INTEGER,
        discounted_at TEXT,
        void_reason TEXT,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (created_by) REFERENCES users(id),
        FOREIGN KEY (discounted_by) REFERENCES users(id)
    )''')

    existing_cols = {row['name'] for row in db.execute('PRAGMA table_info(invoices)').fetchall()}
    target_cols = ['id', 'client_name', 'client_contact', 'status', 'date', 'discount_type',
                    'discount_value', 'discounted_by', 'discounted_at', 'void_reason',
                    'created_by', 'created_at']
    common_cols = [c for c in target_cols if c in existing_cols]
    col_list = ', '.join(common_cols)
    db.execute(f'INSERT INTO invoices_rebuilt ({col_list}) SELECT {col_list} FROM invoices')

    db.execute('DROP TABLE invoices')
    db.execute('ALTER TABLE invoices_rebuilt RENAME TO invoices')
    db.execute('PRAGMA foreign_keys = ON')


def migrate_db(db=None):
    """Upgrade an existing database in place to pick up schema changes added after
    the database was first created. Safe to run repeatedly, and repairs damage from
    an earlier (buggy) version of this migration — see the invoices_old handling below."""
    if db is None:
        db = get_db()

    tables = {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    # --- Repair legacy damage from an earlier migration bug (invoices_old lingering,
    # or invoice_items/income left referencing a since-dropped invoices_old table). ---
    stray_old_invoices = 'invoices_old' in tables
    if stray_old_invoices and 'invoices' in tables:
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute('''INSERT OR IGNORE INTO invoices (id, client_name, client_contact, status, date, created_by, created_at)
                       SELECT id, client_name, client_contact, status, date, created_by, created_at FROM invoices_old''')
        db.execute('DROP TABLE invoices_old')
        db.execute('PRAGMA foreign_keys = ON')
        tables.discard('invoices_old')

    invoice_items_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='invoice_items'"
    ).fetchone()
    if invoice_items_row and 'invoices_old' in (invoice_items_row['sql'] or ''):
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute('''CREATE TABLE invoice_items_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            inventory_item_id INTEGER,
            description TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            unit_price REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id),
            FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id)
        )''')
        db.execute('''INSERT INTO invoice_items_new (id, invoice_id, inventory_item_id, description, quantity, unit_price)
                       SELECT id, invoice_id, inventory_item_id, description, quantity, unit_price FROM invoice_items''')
        db.execute('DROP TABLE invoice_items')
        db.execute('ALTER TABLE invoice_items_new RENAME TO invoice_items')
        db.execute('PRAGMA foreign_keys = ON')

    income_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='income'"
    ).fetchone()
    if income_row and 'invoices_old' in (income_row['sql'] or ''):
        income_cols = {row['name'] for row in db.execute('PRAGMA table_info(income)').fetchall()}
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute('''CREATE TABLE income_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            category_id INTEGER,
            date TEXT NOT NULL,
            note TEXT,
            invoice_id INTEGER,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (invoice_id) REFERENCES invoices(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        )''')
        cols = 'id, amount, category_id, date, note, invoice_id, created_by, created_at' \
            if 'invoice_id' in income_cols else 'id, amount, category_id, date, note, created_by, created_at'
        target = 'id, amount, category_id, date, note, invoice_id, created_by, created_at' \
            if 'invoice_id' in income_cols else 'id, amount, category_id, date, note, created_by, created_at'
        db.execute(f'INSERT INTO income_new ({target}) SELECT {cols} FROM income')
        db.execute('DROP TABLE income')
        db.execute('ALTER TABLE income_new RENAME TO income')
        db.execute('PRAGMA foreign_keys = ON')

    # --- Forward migrations: bring invoices up to the current target schema. ---
    if 'invoices' in tables:
        cols = {row['name'] for row in db.execute('PRAGMA table_info(invoices)').fetchall()}
        invoices_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='invoices'"
        ).fetchone()['sql']
        needs_rebuild = ('discount_type' not in cols
                          or 'void_reason' not in cols
                          or 'cancelled' not in (invoices_sql or ''))
        if needs_rebuild:
            _rebuild_invoices_table(db)

    if 'income' in tables:
        income_cols = {row['name'] for row in db.execute('PRAGMA table_info(income)').fetchall()}
        if 'invoice_id' not in income_cols:
            db.execute('ALTER TABLE income ADD COLUMN invoice_id INTEGER REFERENCES invoices(id)')

    db.execute('''CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_item_id INTEGER NOT NULL,
        change_qty REAL NOT NULL,
        reason TEXT,
        movement_type TEXT NOT NULL DEFAULT 'adjustment',
        created_by INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')
    stock_movement_cols = {row['name'] for row in db.execute('PRAGMA table_info(stock_movements)').fetchall()}
    if 'movement_type' not in stock_movement_cols:
        db.execute("ALTER TABLE stock_movements ADD COLUMN movement_type TEXT NOT NULL DEFAULT 'adjustment'")

    db.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        address TEXT,
        notes TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    if 'invoices' in tables:
        invoice_cols = {row['name'] for row in db.execute('PRAGMA table_info(invoices)').fetchall()}
        if 'customer_id' not in invoice_cols:
            db.execute('ALTER TABLE invoices ADD COLUMN customer_id INTEGER REFERENCES customers(id)')

    db.execute('''CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        address TEXT,
        notes TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'ordered', 'received', 'paid', 'cancelled')),
        date TEXT NOT NULL,
        notes TEXT,
        received_at TEXT,
        paid_at TEXT,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS purchase_order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_order_id INTEGER NOT NULL,
        inventory_item_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        quantity REAL NOT NULL DEFAULT 1,
        unit_cost REAL NOT NULL DEFAULT 0,
        FOREIGN KEY (purchase_order_id) REFERENCES purchase_orders(id),
        FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id)
    )''')

    if 'expenses' in tables:
        expense_cols = {row['name'] for row in db.execute('PRAGMA table_info(expenses)').fetchall()}
        if 'purchase_order_id' not in expense_cols:
            db.execute('ALTER TABLE expenses ADD COLUMN purchase_order_id INTEGER REFERENCES purchase_orders(id)')

    if 'inventory_items' in tables:
        inv_cols = {row['name'] for row in db.execute('PRAGMA table_info(inventory_items)').fetchall()}
        if 'barcode' not in inv_cols:
            db.execute('ALTER TABLE inventory_items ADD COLUMN barcode TEXT')
            # SQLite can't add a UNIQUE column via ALTER TABLE, so enforce uniqueness
            # with an index instead — same guarantee, and NULLs (items with no barcode
            # yet) are allowed to repeat.
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_barcode ON inventory_items(barcode) WHERE barcode IS NOT NULL')
        if 'unit_label' not in inv_cols:
            db.execute("ALTER TABLE inventory_items ADD COLUMN unit_label TEXT NOT NULL DEFAULT 'piece'")
        if 'is_combo' not in inv_cols:
            db.execute('ALTER TABLE inventory_items ADD COLUMN is_combo INTEGER NOT NULL DEFAULT 0')

    db.execute('''CREATE TABLE IF NOT EXISTS item_units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_item_id INTEGER NOT NULL,
        unit_name TEXT NOT NULL,
        conversion_factor REAL NOT NULL,
        sell_price REAL,
        FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id)
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS combo_components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        combo_item_id INTEGER NOT NULL,
        component_item_id INTEGER NOT NULL,
        quantity_needed REAL NOT NULL DEFAULT 1,
        FOREIGN KEY (combo_item_id) REFERENCES inventory_items(id),
        FOREIGN KEY (component_item_id) REFERENCES inventory_items(id)
    )''')

    if 'users' in tables:
        user_cols = {row['name'] for row in db.execute('PRAGMA table_info(users)').fetchall()}
        if 'failed_attempts' not in user_cols:
            db.execute('ALTER TABLE users ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0')

    db.commit()


@click.command('init-db')
def init_db_command():
    """CLI command: flask --app app init-db"""
    init_db()
    click.echo('Database initialized.')


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
