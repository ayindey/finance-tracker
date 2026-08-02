from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .auth import login_required, log_activity, role_required
from .db import get_db, now_str

bp = Blueprint('purchase_orders', __name__, url_prefix='/purchase-orders')


def calculate_po_total(items):
    return sum(item['quantity'] * item['unit_cost'] for item in items)


@bp.route('/')
@login_required
def list_orders():
    db = get_db()
    orders = db.execute(
        '''SELECT purchase_orders.*, suppliers.name AS supplier_name, users.name AS created_by_name,
                  COALESCE(SUM(purchase_order_items.quantity * purchase_order_items.unit_cost), 0) AS total
           FROM purchase_orders
           LEFT JOIN suppliers ON purchase_orders.supplier_id = suppliers.id
           LEFT JOIN users ON purchase_orders.created_by = users.id
           LEFT JOIN purchase_order_items ON purchase_order_items.purchase_order_id = purchase_orders.id
           GROUP BY purchase_orders.id
           ORDER BY purchase_orders.date DESC, purchase_orders.id DESC'''
    ).fetchall()
    return render_template('purchase_orders/list.html', orders=orders)


@bp.route('/new', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def new_order():
    db = get_db()
    suppliers = db.execute('SELECT * FROM suppliers ORDER BY name').fetchall()
    inventory_items = db.execute('SELECT * FROM inventory_items WHERE is_combo = 0 ORDER BY name').fetchall()
    item_units = db.execute('SELECT * FROM item_units ORDER BY inventory_item_id').fetchall()

    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id') or None
        date = request.form.get('date', '').strip()
        notes = request.form.get('notes', '').strip()

        descriptions = request.form.getlist('description[]')
        quantities = request.form.getlist('quantity[]')
        unit_costs = request.form.getlist('unit_cost[]')
        inventory_ids = request.form.getlist('inventory_item_id[]')

        error = None
        if not supplier_id:
            error = 'Select a supplier.'
        elif not date:
            error = 'Date is required.'

        line_items = []
        if error is None:
            for desc, qty, cost, inv_id in zip(descriptions, quantities, unit_costs, inventory_ids):
                desc = desc.strip()
                if not desc and not inv_id:
                    continue
                if not inv_id:
                    error = f'Each line needs an inventory item selected.'
                    break
                try:
                    qty_val = float(qty)
                    cost_val = float(cost)
                    if qty_val <= 0 or cost_val < 0:
                        raise ValueError
                except (ValueError, TypeError):
                    error = f'Invalid quantity or cost for line "{desc}".'
                    break
                line_items.append((desc or None, qty_val, cost_val, int(inv_id)))

            if not line_items and error is None:
                error = 'Add at least one line item.'

        if error is None:
            cursor = db.execute(
                'INSERT INTO purchase_orders (supplier_id, date, notes, status, created_by) VALUES (?, ?, ?, ?, ?)',
                (supplier_id, date, notes, 'draft', g.user['id'])
            )
            po_id = cursor.lastrowid
            for desc, qty_val, cost_val, inv_id in line_items:
                item_name = db.execute('SELECT name FROM inventory_items WHERE id = ?', (inv_id,)).fetchone()['name']
                db.execute(
                    'INSERT INTO purchase_order_items (purchase_order_id, inventory_item_id, description, quantity, unit_cost) VALUES (?, ?, ?, ?, ?)',
                    (po_id, inv_id, desc or item_name, qty_val, cost_val)
                )
            db.commit()
            log_activity(f"{g.user['name']} created purchase order #{po_id}")
            flash('Purchase order created.')
            return redirect(url_for('purchase_orders.view_order', po_id=po_id))

        flash(error)

    return render_template('purchase_orders/form.html', suppliers=suppliers, inventory_items=inventory_items, item_units=item_units)


@bp.route('/<int:po_id>')
@login_required
def view_order(po_id):
    db = get_db()
    order = db.execute(
        '''SELECT purchase_orders.*, suppliers.name AS supplier_name, suppliers.id AS supplier_id_ref
           FROM purchase_orders LEFT JOIN suppliers ON purchase_orders.supplier_id = suppliers.id
           WHERE purchase_orders.id = ?''', (po_id,)
    ).fetchone()
    if order is None:
        flash('Purchase order not found.')
        return redirect(url_for('purchase_orders.list_orders'))

    items = db.execute(
        '''SELECT purchase_order_items.*, inventory_items.name AS item_name
           FROM purchase_order_items
           LEFT JOIN inventory_items ON purchase_order_items.inventory_item_id = inventory_items.id
           WHERE purchase_order_id = ?''', (po_id,)
    ).fetchall()
    total = calculate_po_total(items)

    return render_template('purchase_orders/view.html', order=order, items=items, total=total)


@bp.route('/<int:po_id>/status', methods=('POST',))
@role_required('owner', 'manager')
def update_status(po_id):
    db = get_db()
    order = db.execute('SELECT * FROM purchase_orders WHERE id = ?', (po_id,)).fetchone()
    if order is None:
        flash('Purchase order not found.')
        return redirect(url_for('purchase_orders.list_orders'))

    new_status = request.form.get('status')

    if new_status == 'ordered':
        if order['status'] != 'draft':
            flash('Only a draft order can be marked as ordered.')
            return redirect(url_for('purchase_orders.view_order', po_id=po_id))
        db.execute('UPDATE purchase_orders SET status = ? WHERE id = ?', ('ordered', po_id))
        db.commit()
        log_activity(f"{g.user['name']} marked purchase order #{po_id} as ordered")
        flash('Marked as ordered.')

    elif new_status == 'cancelled':
        if order['status'] in ('received', 'paid'):
            flash('This order has already been received or paid and can\'t be cancelled — it affects real stock/expense records.')
            return redirect(url_for('purchase_orders.view_order', po_id=po_id))
        db.execute('UPDATE purchase_orders SET status = ? WHERE id = ?', ('cancelled', po_id))
        db.commit()
        log_activity(f"{g.user['name']} cancelled purchase order #{po_id}")
        flash('Purchase order cancelled.')

    else:
        flash('Invalid status change.')

    return redirect(url_for('purchase_orders.view_order', po_id=po_id))


@bp.route('/<int:po_id>/receive', methods=('POST',))
@role_required('owner', 'manager')
def receive_order(po_id):
    """Marks a PO received: adds the ordered quantities to inventory stock, logs a
    stock movement per item, and updates each item's cost price to what was just paid."""
    db = get_db()
    order = db.execute('SELECT * FROM purchase_orders WHERE id = ?', (po_id,)).fetchone()
    if order is None:
        flash('Purchase order not found.')
        return redirect(url_for('purchase_orders.list_orders'))

    if order['status'] not in ('draft', 'ordered'):
        flash('This order has already been received.')
        return redirect(url_for('purchase_orders.view_order', po_id=po_id))

    items = db.execute('SELECT * FROM purchase_order_items WHERE purchase_order_id = ?', (po_id,)).fetchall()
    supplier = db.execute('SELECT name FROM suppliers WHERE id = ?', (order['supplier_id'],)).fetchone()
    supplier_name = supplier['name'] if supplier else 'supplier'

    for item in items:
        db.execute(
            'UPDATE inventory_items SET stock_qty = stock_qty + ?, cost_price = ? WHERE id = ?',
            (item['quantity'], item['unit_cost'], item['inventory_item_id'])
        )
        db.execute(
            'INSERT INTO stock_movements (inventory_item_id, change_qty, reason, movement_type, created_by) VALUES (?, ?, ?, ?, ?)',
            (item['inventory_item_id'], item['quantity'], f'Received via PO #{po_id} from {supplier_name}', 'restock', g.user['id'])
        )

    db.execute(
        "UPDATE purchase_orders SET status = 'received', received_at = ? WHERE id = ?",
        (now_str(), po_id)
    )
    db.commit()
    log_activity(f"{g.user['name']} received purchase order #{po_id} — stock updated, cost prices refreshed")
    flash('Order received. Stock levels and cost prices have been updated.')
    return redirect(url_for('purchase_orders.view_order', po_id=po_id))


@bp.route('/<int:po_id>/pay', methods=('POST',))
@role_required('owner', 'manager')
def pay_order(po_id):
    """Marks a PO paid and records the cost as a business expense."""
    db = get_db()
    order = db.execute('SELECT * FROM purchase_orders WHERE id = ?', (po_id,)).fetchone()
    if order is None:
        flash('Purchase order not found.')
        return redirect(url_for('purchase_orders.list_orders'))

    if order['status'] != 'received':
        flash('An order must be received before it can be marked as paid.')
        return redirect(url_for('purchase_orders.view_order', po_id=po_id))

    items = db.execute('SELECT * FROM purchase_order_items WHERE purchase_order_id = ?', (po_id,)).fetchall()
    total = calculate_po_total(items)
    supplier = db.execute('SELECT name FROM suppliers WHERE id = ?', (order['supplier_id'],)).fetchone()
    supplier_name = supplier['name'] if supplier else 'supplier'

    supplies_category = db.execute(
        "SELECT id FROM categories WHERE type = 'expense' AND name = 'Supplies'"
    ).fetchone()
    category_id = supplies_category['id'] if supplies_category else None

    db.execute(
        '''INSERT INTO expenses (amount, category_id, date, note, purchase_order_id, created_by)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (total, category_id, order['date'], f'PO #{po_id} - {supplier_name}', po_id, g.user['id'])
    )
    db.execute(
        "UPDATE purchase_orders SET status = 'paid', paid_at = ? WHERE id = ?",
        (now_str(), po_id)
    )
    db.commit()
    log_activity(f"{g.user['name']} marked purchase order #{po_id} as paid (₦{total:.2f} recorded as an expense)")
    flash(f'Marked as paid. ₦{total:.2f} recorded as an expense.')
    return redirect(url_for('purchase_orders.view_order', po_id=po_id))


@bp.route('/<int:po_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_order(po_id):
    db = get_db()
    order = db.execute('SELECT * FROM purchase_orders WHERE id = ?', (po_id,)).fetchone()
    if order is None:
        flash('Purchase order not found.')
        return redirect(url_for('purchase_orders.list_orders'))

    if order['status'] in ('received', 'paid'):
        flash('This order has already affected stock/expenses and can\'t be deleted.')
        return redirect(url_for('purchase_orders.view_order', po_id=po_id))

    db.execute('DELETE FROM purchase_order_items WHERE purchase_order_id = ?', (po_id,))
    db.execute('DELETE FROM purchase_orders WHERE id = ?', (po_id,))
    db.commit()
    log_activity(f"{g.user['name']} deleted purchase order #{po_id}")
    flash('Purchase order deleted.')
    return redirect(url_for('purchase_orders.list_orders'))
