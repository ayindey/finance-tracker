import csv
import io
from datetime import date

from flask import Blueprint, render_template, request, send_file

from .auth import role_required
from .db import get_db

bp = Blueprint('reports', __name__, url_prefix='/reports')


def _date_range():
    """Read from/to dates from query params, defaulting to the current month."""
    today = date.today()
    default_from = today.replace(day=1).isoformat()
    default_to = today.isoformat()
    date_from = request.args.get('from', default_from)
    date_to = request.args.get('to', default_to)
    return date_from, date_to


@bp.route('/')
@role_required('owner', 'manager')
def profit_and_loss():
    date_from, date_to = _date_range()
    db = get_db()

    income_rows = db.execute(
        '''SELECT COALESCE(categories.name, 'Uncategorized') AS name, SUM(income.amount) AS total
           FROM income LEFT JOIN categories ON income.category_id = categories.id
           WHERE income.date BETWEEN ? AND ?
           GROUP BY categories.id ORDER BY total DESC''',
        (date_from, date_to)
    ).fetchall()
    expense_rows = db.execute(
        '''SELECT COALESCE(categories.name, 'Uncategorized') AS name, SUM(expenses.amount) AS total
           FROM expenses LEFT JOIN categories ON expenses.category_id = categories.id
           WHERE expenses.date BETWEEN ? AND ?
           GROUP BY categories.id ORDER BY total DESC''',
        (date_from, date_to)
    ).fetchall()

    total_income = sum(r['total'] for r in income_rows)
    total_expenses = sum(r['total'] for r in expense_rows)

    # Cost of Goods Sold: the cost-price value of everything sold via paid invoices in this period.
    cogs_row = db.execute(
        '''SELECT COALESCE(SUM(invoice_items.quantity * inventory_items.cost_price), 0) AS total
           FROM invoice_items
           JOIN invoices ON invoice_items.invoice_id = invoices.id
           JOIN inventory_items ON invoice_items.inventory_item_id = inventory_items.id
           WHERE invoices.status = 'paid' AND invoices.date BETWEEN ? AND ?''',
        (date_from, date_to)
    ).fetchone()
    cogs = cogs_row['total']
    gross_profit = total_income - cogs

    # Damaged/spoiled goods: cost-price value of stock written off in this period.
    # created_at is a full timestamp ('YYYY-MM-DD HH:MM:SS'); SUBSTR pulls out just the
    # date part so it compares against date_from/date_to — works on SQLite and Postgres
    # alike, unlike SQLite's date() function.
    damage_row = db.execute(
        '''SELECT COALESCE(SUM(-stock_movements.change_qty * inventory_items.cost_price), 0) AS total
           FROM stock_movements
           JOIN inventory_items ON stock_movements.inventory_item_id = inventory_items.id
           WHERE stock_movements.movement_type = 'damage'
             AND SUBSTR(stock_movements.created_at, 1, 10) BETWEEN ? AND ?''',
        (date_from, date_to)
    ).fetchone()
    inventory_loss = damage_row['total']

    paid_invoices = db.execute(
        "SELECT COUNT(*) AS c FROM invoices WHERE status = 'paid' AND date BETWEEN ? AND ?",
        (date_from, date_to)
    ).fetchone()['c']

    return render_template(
        'reports/profit_and_loss.html',
        date_from=date_from, date_to=date_to,
        income_rows=income_rows, expense_rows=expense_rows,
        total_income=total_income, total_expenses=total_expenses,
        cogs=cogs, gross_profit=gross_profit, inventory_loss=inventory_loss,
        net_profit=total_income - total_expenses - cogs - inventory_loss,
        paid_invoices=paid_invoices,
    )


@bp.route('/export/<kind>.csv')
@role_required('owner', 'manager')
def export_csv(kind):
    date_from, date_to = _date_range()
    db = get_db()

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    if kind == 'income':
        writer.writerow(['Date', 'Amount', 'Category', 'Note', 'Logged by', 'Logged at'])
        rows = db.execute(
            '''SELECT income.date, income.amount, categories.name AS category, income.note,
                      users.name AS logged_by, income.created_at
               FROM income
               LEFT JOIN categories ON income.category_id = categories.id
               LEFT JOIN users ON income.created_by = users.id
               WHERE income.date BETWEEN ? AND ? ORDER BY income.date''',
            (date_from, date_to)
        ).fetchall()
        for r in rows:
            writer.writerow([r['date'], r['amount'], r['category'], r['note'], r['logged_by'], r['created_at']])

    elif kind == 'expenses':
        writer.writerow(['Date', 'Amount', 'Category', 'Note', 'Logged by', 'Logged at'])
        rows = db.execute(
            '''SELECT expenses.date, expenses.amount, categories.name AS category, expenses.note,
                      users.name AS logged_by, expenses.created_at
               FROM expenses
               LEFT JOIN categories ON expenses.category_id = categories.id
               LEFT JOIN users ON expenses.created_by = users.id
               WHERE expenses.date BETWEEN ? AND ? ORDER BY expenses.date''',
            (date_from, date_to)
        ).fetchall()
        for r in rows:
            writer.writerow([r['date'], r['amount'], r['category'], r['note'], r['logged_by'], r['created_at']])

    elif kind == 'invoices':
        writer.writerow(['Invoice #', 'Client', 'Date', 'Status', 'Total', 'Created by'])
        rows = db.execute(
            '''SELECT invoices.id, invoices.client_name, invoices.date, invoices.status,
                      users.name AS created_by,
                      COALESCE(SUM(invoice_items.quantity * invoice_items.unit_price), 0) AS total
               FROM invoices
               LEFT JOIN users ON invoices.created_by = users.id
               LEFT JOIN invoice_items ON invoice_items.invoice_id = invoices.id
               WHERE invoices.date BETWEEN ? AND ?
               GROUP BY invoices.id, invoices.client_name, invoices.date, invoices.status, users.name
               ORDER BY invoices.date''',
            (date_from, date_to)
        ).fetchall()
        for r in rows:
            writer.writerow([r['id'], r['client_name'], r['date'], r['status'], r['total'], r['created_by']])

    else:
        return 'Unknown export type', 404

    mem = io.BytesIO(buffer.getvalue().encode('utf-8'))
    return send_file(
        mem, mimetype='text/csv', as_attachment=True,
        download_name=f'{kind}_{date_from}_to_{date_to}.csv'
    )
