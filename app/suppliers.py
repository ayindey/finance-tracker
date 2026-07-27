from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .auth import login_required, log_activity, role_required
from .db import get_db

bp = Blueprint('suppliers', __name__, url_prefix='/suppliers')


@bp.route('/')
@login_required
def list_suppliers():
    db = get_db()
    suppliers = db.execute(
        '''SELECT suppliers.*, COUNT(purchase_orders.id) AS po_count
           FROM suppliers
           LEFT JOIN purchase_orders ON purchase_orders.supplier_id = suppliers.id
                AND purchase_orders.status != 'cancelled'
           GROUP BY suppliers.id
           ORDER BY suppliers.name'''
    ).fetchall()
    return render_template('suppliers/list.html', suppliers=suppliers)


@bp.route('/new', methods=('GET', 'POST'))
@login_required
def new_supplier():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        address = request.form.get('address', '').strip()
        notes = request.form.get('notes', '').strip()
        error = None

        if not name:
            error = 'Name is required.'

        if error is None:
            db = get_db()
            cursor = db.execute(
                'INSERT INTO suppliers (name, phone, email, address, notes, created_by) VALUES (?, ?, ?, ?, ?, ?)',
                (name, phone, email, address, notes, g.user['id'])
            )
            db.commit()
            log_activity(f"{g.user['name']} added supplier '{name}'")
            flash(f'Supplier "{name}" added.')

            return_to = request.form.get('return_to')
            if return_to:
                return redirect(return_to)
            return redirect(url_for('suppliers.view_supplier', supplier_id=cursor.lastrowid))

        flash(error)

    return render_template('suppliers/form.html', supplier=None, return_to=request.args.get('return_to'))


@bp.route('/<int:supplier_id>')
@login_required
def view_supplier(supplier_id):
    db = get_db()
    supplier = db.execute('SELECT * FROM suppliers WHERE id = ?', (supplier_id,)).fetchone()
    if supplier is None:
        flash('Supplier not found.')
        return redirect(url_for('suppliers.list_suppliers'))

    orders = db.execute(
        '''SELECT purchase_orders.*,
                  COALESCE(SUM(purchase_order_items.quantity * purchase_order_items.unit_cost), 0) AS total
           FROM purchase_orders
           LEFT JOIN purchase_order_items ON purchase_order_items.purchase_order_id = purchase_orders.id
           WHERE purchase_orders.supplier_id = ?
           GROUP BY purchase_orders.id
           ORDER BY purchase_orders.date DESC, purchase_orders.id DESC''',
        (supplier_id,)
    ).fetchall()

    total_paid = sum(o['total'] for o in orders if o['status'] == 'paid')
    owed = sum(o['total'] for o in orders if o['status'] in ('ordered', 'received'))

    return render_template(
        'suppliers/view.html', supplier=supplier, orders=orders,
        total_paid=total_paid, owed=owed
    )


@bp.route('/<int:supplier_id>/edit', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def edit_supplier(supplier_id):
    db = get_db()
    supplier = db.execute('SELECT * FROM suppliers WHERE id = ?', (supplier_id,)).fetchone()
    if supplier is None:
        flash('Supplier not found.')
        return redirect(url_for('suppliers.list_suppliers'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        address = request.form.get('address', '').strip()
        notes = request.form.get('notes', '').strip()
        error = None

        if not name:
            error = 'Name is required.'

        if error is None:
            db.execute(
                'UPDATE suppliers SET name = ?, phone = ?, email = ?, address = ?, notes = ? WHERE id = ?',
                (name, phone, email, address, notes, supplier_id)
            )
            db.commit()
            log_activity(f"{g.user['name']} updated supplier '{name}'")
            flash('Supplier updated.')
            return redirect(url_for('suppliers.view_supplier', supplier_id=supplier_id))

        flash(error)

    return render_template('suppliers/form.html', supplier=supplier, return_to=None)


@bp.route('/<int:supplier_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_supplier(supplier_id):
    db = get_db()
    supplier = db.execute('SELECT * FROM suppliers WHERE id = ?', (supplier_id,)).fetchone()
    if supplier is None:
        flash('Supplier not found.')
        return redirect(url_for('suppliers.list_suppliers'))

    linked = db.execute('SELECT COUNT(*) AS c FROM purchase_orders WHERE supplier_id = ?', (supplier_id,)).fetchone()['c']
    if linked > 0:
        flash(f'"{supplier["name"]}" has {linked} purchase order(s) on record and can\'t be deleted.')
        return redirect(url_for('suppliers.view_supplier', supplier_id=supplier_id))

    db.execute('DELETE FROM suppliers WHERE id = ?', (supplier_id,))
    db.commit()
    log_activity(f"{g.user['name']} deleted supplier '{supplier['name']}'")
    flash('Supplier deleted.')
    return redirect(url_for('suppliers.list_suppliers'))
