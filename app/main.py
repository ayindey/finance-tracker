from flask import Blueprint, g, redirect, render_template, request, url_for, flash
from werkzeug.security import generate_password_hash
from datetime import datetime

from .auth import login_required, role_required
from .db import get_db

bp = Blueprint('main', __name__)


@bp.route('/setup', methods=('GET', 'POST'))
def setup():
    """One-time setup: create the first owner account.
    Locks itself once a user already exists, so it can't be used to
    create extra owner accounts later."""
    db = get_db()
    existing = db.execute('SELECT id FROM users LIMIT 1').fetchone()
    if existing is not None:
        flash('Setup already completed. Please log in.')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']
        error = None

        if not name or not email or not password:
            error = 'All fields are required.'
        elif len(password) < 6:
            error = 'Password should be at least 6 characters.'

        if error is None:
            db.execute(
                'INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)',
                (name, email, generate_password_hash(password), 'owner')
            )
            db.commit()
            flash('Owner account created. Please log in.')
            return redirect(url_for('auth.login'))

        flash(error)

    return render_template('setup.html')


@bp.route('/dashboard')
@role_required('owner', 'manager')
def dashboard():
    db = get_db()
    total_income = db.execute('SELECT COALESCE(SUM(amount), 0) AS total FROM income').fetchone()['total']
    total_expenses = db.execute('SELECT COALESCE(SUM(amount), 0) AS total FROM expenses').fetchone()['total']
    low_stock = db.execute(
        'SELECT * FROM inventory_items WHERE stock_qty <= low_stock_threshold'
    ).fetchall()
    unpaid_invoices = db.execute(
        "SELECT COUNT(*) AS c FROM invoices WHERE status IN ('draft', 'sent')"
    ).fetchone()['c']

    # Last 6 months of income vs expenses, for the trend chart.
    # SUBSTR(date, 1, 7) pulls out 'YYYY-MM' from a 'YYYY-MM-DD' string — this works
    # identically on SQLite and Postgres, unlike strftime() which is SQLite-only.
    monthly_income = db.execute(
        '''SELECT SUBSTR(date, 1, 7) AS month, SUM(amount) AS total
           FROM income GROUP BY month ORDER BY month DESC LIMIT 6'''
    ).fetchall()
    monthly_expenses = db.execute(
        '''SELECT SUBSTR(date, 1, 7) AS month, SUM(amount) AS total
           FROM expenses GROUP BY month ORDER BY month DESC LIMIT 6'''
    ).fetchall()

    months = sorted(set([r['month'] for r in monthly_income] + [r['month'] for r in monthly_expenses]))
    income_by_month = {r['month']: r['total'] for r in monthly_income}
    expenses_by_month = {r['month']: r['total'] for r in monthly_expenses}
    trend_labels = months
    trend_income = [income_by_month.get(m, 0) for m in months]
    trend_expenses = [expenses_by_month.get(m, 0) for m in months]

    # Expense breakdown by category, for the pie chart.
    category_breakdown = db.execute(
        '''SELECT COALESCE(categories.name, 'Uncategorized') AS name, SUM(expenses.amount) AS total
           FROM expenses LEFT JOIN categories ON expenses.category_id = categories.id
           GROUP BY categories.id ORDER BY total DESC'''
    ).fetchall()

    # Damaged/spoiled inventory loss this month. Current month computed in Python
    # (portable) rather than via a database-specific "now" function.
    current_month = datetime.utcnow().strftime('%Y-%m')
    inventory_loss_row = db.execute(
        '''SELECT COALESCE(SUM(-stock_movements.change_qty * inventory_items.cost_price), 0) AS total
           FROM stock_movements
           JOIN inventory_items ON stock_movements.inventory_item_id = inventory_items.id
           WHERE stock_movements.movement_type = 'damage'
             AND SUBSTR(stock_movements.created_at, 1, 7) = ?''',
        (current_month,)
    ).fetchone()
    inventory_loss_this_month = inventory_loss_row['total']

    return render_template(
        'dashboard.html',
        total_income=total_income,
        total_expenses=total_expenses,
        balance=total_income - total_expenses,
        low_stock=low_stock,
        unpaid_invoices=unpaid_invoices,
        trend_labels=trend_labels,
        trend_income=trend_income,
        trend_expenses=trend_expenses,
        category_labels=[c['name'] for c in category_breakdown],
        category_totals=[c['total'] for c in category_breakdown],
        inventory_loss_this_month=inventory_loss_this_month,
    )


@bp.route('/')
def index():
    if g.user:
        if g.user['role'] == 'staff':
            return redirect(url_for('invoices.list_invoices'))
        return redirect(url_for('main.dashboard'))

    db = get_db()
    existing = db.execute('SELECT id FROM users LIMIT 1').fetchone()
    if existing is None:
        return redirect(url_for('main.setup'))

    return redirect(url_for('auth.login'))
