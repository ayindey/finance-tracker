from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .auth import login_required, log_activity, role_required
from .db import get_db

bp = Blueprint('customers', __name__, url_prefix='/customers')


@bp.route('/')
@login_required
def list_customers():
    db = get_db()
    customers = db.execute(
        '''SELECT customers.*,
                  COUNT(invoices.id) AS invoice_count,
                  COALESCE(SUM(CASE WHEN invoices.status = 'paid' THEN 1 ELSE 0 END), 0) AS paid_count
           FROM customers
           LEFT JOIN invoices ON invoices.customer_id = customers.id AND invoices.status NOT IN ('void', 'cancelled')
           GROUP BY customers.id
           ORDER BY customers.name'''
    ).fetchall()
    return render_template('customers/list.html', customers=customers)


@bp.route('/new', methods=('GET', 'POST'))
@login_required
def new_customer():
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
                'INSERT INTO customers (name, phone, email, address, notes, created_by) VALUES (?, ?, ?, ?, ?, ?)',
                (name, phone, email, address, notes, g.user['id'])
            )
            db.commit()
            log_activity(f"{g.user['name']} added customer '{name}'")
            flash(f'Customer "{name}" added.')

            # If we got here from the invoice form's "quick add" flow, send them
            # straight back so they can pick the customer they just created.
            return_to = request.form.get('return_to')
            if return_to:
                return redirect(return_to)
            return redirect(url_for('customers.view_customer', customer_id=cursor.lastrowid))

        flash(error)

    return render_template('customers/form.html', customer=None, return_to=request.args.get('return_to'))


@bp.route('/<int:customer_id>')
@login_required
def view_customer(customer_id):
    db = get_db()
    customer = db.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    if customer is None:
        flash('Customer not found.')
        return redirect(url_for('customers.list_customers'))

    invoices = db.execute(
        '''SELECT invoices.*,
                  COALESCE(SUM(invoice_items.quantity * invoice_items.unit_price), 0) AS subtotal
           FROM invoices
           LEFT JOIN invoice_items ON invoice_items.invoice_id = invoices.id
           WHERE invoices.customer_id = ?
           GROUP BY invoices.id
           ORDER BY invoices.date DESC, invoices.id DESC''',
        (customer_id,)
    ).fetchall()

    total_spent = 0.0
    outstanding = 0.0
    invoices_with_totals = []
    for inv in invoices:
        discount_amount = 0.0
        if inv['discount_type'] == 'percentage':
            discount_amount = inv['subtotal'] * (inv['discount_value'] / 100.0)
        elif inv['discount_type'] == 'fixed':
            discount_amount = min(inv['discount_value'], inv['subtotal'])
        total = inv['subtotal'] - discount_amount
        invoices_with_totals.append({**dict(inv), 'total': total})
        if inv['status'] == 'paid':
            total_spent += total
        elif inv['status'] in ('draft', 'sent'):
            outstanding += total

    return render_template(
        'customers/view.html', customer=customer, invoices=invoices_with_totals,
        total_spent=total_spent, outstanding=outstanding
    )


@bp.route('/<int:customer_id>/edit', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def edit_customer(customer_id):
    db = get_db()
    customer = db.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    if customer is None:
        flash('Customer not found.')
        return redirect(url_for('customers.list_customers'))

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
                'UPDATE customers SET name = ?, phone = ?, email = ?, address = ?, notes = ? WHERE id = ?',
                (name, phone, email, address, notes, customer_id)
            )
            db.commit()
            log_activity(f"{g.user['name']} updated customer '{name}'")
            flash('Customer updated.')
            return redirect(url_for('customers.view_customer', customer_id=customer_id))

        flash(error)

    return render_template('customers/form.html', customer=customer, return_to=None)


@bp.route('/<int:customer_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_customer(customer_id):
    db = get_db()
    customer = db.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    if customer is None:
        flash('Customer not found.')
        return redirect(url_for('customers.list_customers'))

    linked = db.execute('SELECT COUNT(*) AS c FROM invoices WHERE customer_id = ?', (customer_id,)).fetchone()['c']
    if linked > 0:
        flash(f'"{customer["name"]}" has {linked} invoice(s) on record and can\'t be deleted — '
              f'that would break their purchase history.')
        return redirect(url_for('customers.view_customer', customer_id=customer_id))

    db.execute('DELETE FROM customers WHERE id = ?', (customer_id,))
    db.commit()
    log_activity(f"{g.user['name']} deleted customer '{customer['name']}'")
    flash('Customer deleted.')
    return redirect(url_for('customers.list_customers'))
