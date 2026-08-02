from flask import Blueprint, jsonify

from .auth import login_required
from .db import get_db

bp = Blueprint('api', __name__, url_prefix='/api')


@bp.route('/version')
@login_required
def version():
    """Returns a cheap signature of app state that changes whenever invoices,
    inventory, or income/expenses change. The frontend polls this and only
    re-fetches full content when the signature changes, so pages can update
    live without the user manually refreshing."""
    db = get_db()
    row = db.execute('''
        SELECT
            (SELECT COUNT(*) FROM invoices) AS inv_count,
            (SELECT COALESCE(MAX(id), 0) FROM invoices) AS inv_max_id,
            (SELECT COALESCE(SUM(CASE status
                WHEN 'draft' THEN 1 WHEN 'sent' THEN 2 WHEN 'paid' THEN 3
                WHEN 'void' THEN 4 WHEN 'cancelled' THEN 5 ELSE 0 END), 0)
             FROM (SELECT status FROM invoices ORDER BY id DESC LIMIT 20) AS recent) AS inv_status_sum,
            (SELECT COUNT(*) FROM inventory_items) AS item_count,
            (SELECT COALESCE(SUM(stock_qty), 0) FROM inventory_items) AS stock_total,
            (SELECT COUNT(*) FROM income) AS income_count,
            (SELECT COUNT(*) FROM expenses) AS expense_count
    ''').fetchone()

    signature = '-'.join(str(v) for v in row)
    return jsonify({'version': signature})
