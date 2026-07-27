from flask import (
    Blueprint, flash, g, redirect, render_template, request, url_for
)

from .auth import login_required, log_activity, role_required
from .db import get_db
from . import printing
from .inventory import get_available_stock, deduct_stock_for_sale, restore_stock_for_sale

bp = Blueprint('invoices', __name__, url_prefix='/invoices')


def _print_kot(db, invoice_id, note=None, reprint_reason=None):
    """(Re)print the KOT for an invoice. Used on creation (no reason needed — it's the
    original print) and for manual "Print/Update KOT" clicks (reason required, printed
    on the ticket itself). Never raises."""
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    items = db.execute('SELECT * FROM invoice_items WHERE invoice_id = ?', (invoice_id,)).fetchall()
    if not printing.is_printer_configured('kot'):
        return
    kot_bytes = printing.build_kot_ticket(invoice, items, reprint_reason=reprint_reason)
    sent, msg = printing.send_to_printer(kot_bytes, 'kot')
    if sent:
        log_activity(f"KOT for invoice #{invoice_id} printed" + (f" ({note})" if note else ""))
    else:
        flash(f'Saved, but the KOT could not be printed automatically: {msg}')


def _print_bill(db, invoice_id, reprint_reason=None):
    """(Re)print the bill for an invoice. Used on payment (no reason needed — it's the
    original print) and for manual reprints (reason required, printed on the receipt).
    Never raises."""
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    items = db.execute('SELECT * FROM invoice_items WHERE invoice_id = ?', (invoice_id,)).fetchall()
    if not printing.is_printer_configured('bill'):
        return
    subtotal, discount_amount, total = calculate_totals(invoice, items)
    bill_bytes = printing.build_bill_receipt(invoice, items, subtotal, discount_amount, total, reprint_reason=reprint_reason)
    sent, msg = printing.send_to_printer(bill_bytes, 'bill')
    if sent:
        log_activity(f"Bill for invoice #{invoice_id} printed" + (f" (reprint: {reprint_reason})" if reprint_reason else ""))
    else:
        flash(f'Saved, but the bill could not be printed automatically: {msg}')


def calculate_totals(invoice, items):
    """Returns (subtotal, discount_amount, total) for an invoice + its line items."""
    subtotal = sum(item['quantity'] * item['unit_price'] for item in items)
    discount_amount = 0.0
    if invoice['discount_type'] == 'percentage':
        discount_amount = subtotal * (invoice['discount_value'] / 100.0)
    elif invoice['discount_type'] == 'fixed':
        discount_amount = min(invoice['discount_value'], subtotal)
    total = subtotal - discount_amount
    return subtotal, discount_amount, total


@bp.route('/')
@login_required
def list_invoices():
    db = get_db()
    rows = db.execute(
        '''SELECT invoices.*, users.name AS created_by_name,
                  COALESCE(SUM(invoice_items.quantity * invoice_items.unit_price), 0) AS subtotal
           FROM invoices
           LEFT JOIN users ON invoices.created_by = users.id
           LEFT JOIN invoice_items ON invoice_items.invoice_id = invoices.id
           GROUP BY invoices.id
           ORDER BY invoices.date DESC, invoices.id DESC'''
    ).fetchall()

    invoices_with_totals = []
    for inv in rows:
        discount_amount = 0.0
        if inv['discount_type'] == 'percentage':
            discount_amount = inv['subtotal'] * (inv['discount_value'] / 100.0)
        elif inv['discount_type'] == 'fixed':
            discount_amount = min(inv['discount_value'], inv['subtotal'])
        total = inv['subtotal'] - discount_amount
        invoices_with_totals.append({**dict(inv), 'total': total})

    return render_template('invoices/list.html', invoices=invoices_with_totals)


@bp.route('/new', methods=('GET', 'POST'))
@login_required
def new_invoice():
    db = get_db()
    inventory_items_raw = db.execute('SELECT * FROM inventory_items ORDER BY name').fetchall()
    inventory_items = []
    for it in inventory_items_raw:
        d = dict(it)
        d['display_stock'] = get_available_stock(db, it['id']) if it['is_combo'] else it['stock_qty']
        inventory_items.append(d)

    if request.method == 'POST':
        client_name = request.form.get('client_name', '').strip()
        client_contact = request.form.get('client_contact', '').strip()
        customer_id = request.form.get('customer_id') or None
        date = request.form.get('date', '').strip()

        descriptions = request.form.getlist('description[]')
        quantities = request.form.getlist('quantity[]')
        unit_prices = request.form.getlist('unit_price[]')
        inventory_ids = request.form.getlist('inventory_item_id[]')

        error = None
        if not client_name:
            error = 'Client name is required.'
        elif not date:
            error = 'Date is required.'

        line_items = []
        if error is None:
            for desc, qty, price, inv_id in zip(descriptions, quantities, unit_prices, inventory_ids):
                desc = desc.strip()
                if not desc:
                    continue
                try:
                    qty_val = float(qty)
                    price_val = float(price)
                    if qty_val <= 0 or price_val < 0:
                        raise ValueError
                except (ValueError, TypeError):
                    error = f'Invalid quantity or price for line "{desc}".'
                    break
                line_items.append((desc, qty_val, price_val, int(inv_id) if inv_id else None))

            if not line_items and error is None:
                error = 'Add at least one line item.'

        if error is None:
            requested = {}
            for desc, qty_val, price_val, inv_id in line_items:
                if inv_id is not None:
                    requested[inv_id] = requested.get(inv_id, 0) + qty_val
            for inv_id, qty_needed in requested.items():
                stock_item = db.execute(
                    'SELECT name FROM inventory_items WHERE id = ?', (inv_id,)
                ).fetchone()
                if stock_item is None:
                    error = 'One of the selected inventory items no longer exists.'
                    break
                available = get_available_stock(db, inv_id)
                if qty_needed > available:
                    error = (f'Not enough stock for "{stock_item["name"]}": '
                              f'requested {qty_needed:g}, only {available:g} available.')
                    break

        if error is None:
            cursor = db.execute(
                'INSERT INTO invoices (client_name, client_contact, customer_id, date, status, created_by) VALUES (?, ?, ?, ?, ?, ?)',
                (client_name, client_contact, customer_id, date, 'draft', g.user['id'])
            )
            invoice_id = cursor.lastrowid
            for desc, qty_val, price_val, inv_id in line_items:
                db.execute(
                    'INSERT INTO invoice_items (invoice_id, inventory_item_id, description, quantity, unit_price) VALUES (?, ?, ?, ?, ?)',
                    (invoice_id, inv_id, desc, qty_val, price_val)
                )
            db.commit()
            log_activity(f"{g.user['name']} created invoice #{invoice_id} for {client_name}")
            _print_kot(db, invoice_id)

            flash(f'Invoice for {client_name} created.')
            return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

        flash(error)

    customers = db.execute('SELECT * FROM customers ORDER BY name').fetchall()
    return render_template('invoices/form.html', inventory_items=inventory_items, customers=customers)


@bp.route('/<int:invoice_id>')
@login_required
def view_invoice(invoice_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    items = db.execute(
        'SELECT * FROM invoice_items WHERE invoice_id = ?', (invoice_id,)
    ).fetchall()
    subtotal, discount_amount, total = calculate_totals(invoice, items)

    discounter = None
    if invoice['discounted_by']:
        discounter = db.execute('SELECT name FROM users WHERE id = ?', (invoice['discounted_by'],)).fetchone()

    inventory_items_raw = db.execute('SELECT * FROM inventory_items ORDER BY name').fetchall()
    inventory_items = []
    for it in inventory_items_raw:
        d = dict(it)
        d['display_stock'] = get_available_stock(db, it['id']) if it['is_combo'] else it['stock_qty']
        inventory_items.append(d)

    customer = None
    if invoice['customer_id']:
        customer = db.execute('SELECT * FROM customers WHERE id = ?', (invoice['customer_id'],)).fetchone()

    return render_template(
        'invoices/view.html', invoice=invoice, items=items,
        subtotal=subtotal, discount_amount=discount_amount, total=total,
        discounter=discounter, inventory_items=inventory_items, customer=customer
    )


@bp.route('/<int:invoice_id>/discount', methods=('POST',))
@role_required('owner', 'manager')
def set_discount(invoice_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    if invoice['status'] in ('paid', 'void'):
        flash('This invoice is already settled — discounts can only be applied before an invoice is paid.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    discount_type = request.form.get('discount_type')
    discount_value = request.form.get('discount_value', '').strip()

    if discount_type == 'none':
        db.execute(
            'UPDATE invoices SET discount_type = NULL, discount_value = 0, discounted_by = ?, discounted_at = datetime("now") WHERE id = ?',
            (g.user['id'], invoice_id)
        )
        db.commit()
        log_activity(f"{g.user['name']} removed the discount on invoice #{invoice_id}")
        flash('Discount removed.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    if discount_type not in ('percentage', 'fixed'):
        flash('Invalid discount type.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    try:
        value = float(discount_value)
        if value < 0:
            raise ValueError
        if discount_type == 'percentage' and value > 100:
            raise ValueError
    except ValueError:
        flash('Enter a valid discount amount.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    db.execute(
        'UPDATE invoices SET discount_type = ?, discount_value = ?, discounted_by = ?, discounted_at = datetime("now") WHERE id = ?',
        (discount_type, value, g.user['id'], invoice_id)
    )
    db.commit()
    label = f"{value:g}%" if discount_type == 'percentage' else f"NGN {value:.2f}"
    log_activity(f"{g.user['name']} applied a {label} discount to invoice #{invoice_id}")
    flash(f'Discount of {label} applied.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


@bp.route('/<int:invoice_id>/status', methods=('POST',))
@login_required
def update_status(invoice_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    if invoice['status'] in ('void', 'cancelled'):
        flash(f"This invoice has been {invoice['status']} and can no longer be changed.")
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    new_status = request.form.get('status')
    if new_status not in ('draft', 'sent', 'paid'):
        flash('Invalid status.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    all_items = db.execute('SELECT * FROM invoice_items WHERE invoice_id = ?', (invoice_id,)).fetchall()
    linked_items = [item for item in all_items if item['inventory_item_id'] is not None]

    if new_status == 'paid' and invoice['status'] != 'paid':
        needed = {}
        for item in linked_items:
            needed[item['inventory_item_id']] = needed.get(item['inventory_item_id'], 0) + item['quantity']
        for inv_item_id, qty_needed in needed.items():
            stock_item = db.execute(
                'SELECT name FROM inventory_items WHERE id = ?', (inv_item_id,)
            ).fetchone()
            available = get_available_stock(db, inv_item_id) if stock_item else 0
            if stock_item is None or qty_needed > available:
                name = stock_item['name'] if stock_item else 'an item'
                flash(f'Cannot mark as paid: not enough stock for "{name}" '
                      f'(needs {qty_needed:g}, only {available:g} available). Adjust the invoice or restock first.')
                return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

        for item in linked_items:
            deduct_stock_for_sale(db, item['inventory_item_id'], item['quantity'],
                                   f'Sold via invoice #{invoice_id}', 'sale', g.user['id'])

        _, _, invoice_total = calculate_totals(invoice, all_items)
        sales_category = db.execute(
            "SELECT id FROM categories WHERE type = 'income' AND name = 'Sales'"
        ).fetchone()
        sales_category_id = sales_category['id'] if sales_category else None

        db.execute(
            '''INSERT INTO income (amount, category_id, date, note, invoice_id, created_by)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (invoice_total, sales_category_id, invoice['date'],
             f"Invoice #{invoice_id} - {invoice['client_name']}", invoice_id, g.user['id'])
        )

    db.execute('UPDATE invoices SET status = ? WHERE id = ?', (new_status, invoice_id))
    db.commit()
    log_activity(f"{g.user['name']} marked invoice #{invoice_id} as {new_status}")

    if new_status == 'paid':
        _print_bill(db, invoice_id)

    flash(f'Invoice marked as {new_status}.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


@bp.route('/<int:invoice_id>/void', methods=('POST',))
@role_required('owner', 'manager')
def void_invoice(invoice_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    if invoice['status'] != 'paid':
        flash('Only a paid invoice can be voided.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('A reason is required to void an invoice.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    items = db.execute(
        'SELECT * FROM invoice_items WHERE invoice_id = ? AND inventory_item_id IS NOT NULL',
        (invoice_id,)
    ).fetchall()

    for item in items:
        restore_stock_for_sale(db, item['inventory_item_id'], item['quantity'],
                                f'Restored: invoice #{invoice_id} voided', 'void_restore', g.user['id'])

    db.execute('DELETE FROM income WHERE invoice_id = ?', (invoice_id,))

    db.execute('UPDATE invoices SET status = ?, void_reason = ? WHERE id = ?', ('void', reason, invoice_id))
    db.commit()
    log_activity(f"{g.user['name']} voided invoice #{invoice_id} (stock restored, income reversed): {reason}")
    flash('Invoice voided. Stock has been restored and the associated income entry removed.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


@bp.route('/<int:invoice_id>/cancel', methods=('POST',))
@role_required('owner', 'manager')
def cancel_invoice(invoice_id):
    """Cancel an invoice that was never paid — distinct from voiding a paid one,
    since no stock or income ever needs reversing here."""
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    if invoice['status'] in ('paid', 'void', 'cancelled'):
        flash('Only a draft or sent invoice can be cancelled.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('A reason is required to cancel an invoice.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    db.execute('UPDATE invoices SET status = ?, void_reason = ? WHERE id = ?', ('cancelled', reason, invoice_id))
    db.commit()
    log_activity(f"{g.user['name']} cancelled invoice #{invoice_id}: {reason}")
    flash('Invoice cancelled.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


@bp.route('/<int:invoice_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_invoice(invoice_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice and invoice['status'] == 'paid':
        flash('This invoice is paid — void it first if you need to remove it, so stock and income stay accurate.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    db.execute('DELETE FROM invoice_items WHERE invoice_id = ?', (invoice_id,))
    db.execute('DELETE FROM invoices WHERE id = ?', (invoice_id,))
    db.commit()
    log_activity(f"{g.user['name']} deleted invoice #{invoice_id}")
    flash('Invoice deleted.')
    return redirect(url_for('invoices.list_invoices'))


@bp.route('/<int:invoice_id>/reprint-kot', methods=('POST',))
@login_required
def reprint_kot(invoice_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    reason = request.form.get('reason', '')
    if reason not in printing.KOT_REPRINT_REASONS:
        flash('Select a valid reason.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    _print_kot(db, invoice_id, note=reason, reprint_reason=reason)
    flash('KOT sent to the printer.' if printing.is_printer_configured('kot') else
          'No KOT printer configured in Settings.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


@bp.route('/<int:invoice_id>/reprint-bill', methods=('POST',))
@login_required
def reprint_bill(invoice_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    reason = request.form.get('reason', '')
    if reason not in printing.BILL_REPRINT_REASONS:
        flash('Select a valid reason.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    _print_bill(db, invoice_id, reprint_reason=reason)
    flash('Bill sent to the printer.' if printing.is_printer_configured('bill') else
          'No bill printer configured in Settings.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


@bp.route('/<int:invoice_id>/add-item', methods=('POST',))
@login_required
def add_item(invoice_id):
    """Add a line item to an invoice that hasn't been settled yet — reprints the KOT
    automatically so whoever is packing the order sees the updated list."""
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    if invoice['status'] not in ('draft', 'sent'):
        flash('Items can only be added before an invoice is settled.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    description = request.form.get('description', '').strip()
    quantity = request.form.get('quantity', '').strip()
    unit_price = request.form.get('unit_price', '').strip()
    inventory_item_id = request.form.get('inventory_item_id') or None
    error = None

    if not description:
        error = 'Description is required.'

    try:
        qty_val = float(quantity)
        price_val = float(unit_price)
        if qty_val <= 0 or price_val < 0:
            raise ValueError
    except ValueError:
        error = 'Enter a valid quantity and price.'

    if error is None and inventory_item_id:
        stock_item = db.execute(
            'SELECT name FROM inventory_items WHERE id = ?', (inventory_item_id,)
        ).fetchone()
        if stock_item is None:
            error = 'Selected inventory item no longer exists.'
        else:
            available = get_available_stock(db, inventory_item_id)
            if qty_val > available:
                error = f'Not enough stock for "{stock_item["name"]}": only {available:g} available.'

    if error:
        flash(error)
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    db.execute(
        'INSERT INTO invoice_items (invoice_id, inventory_item_id, description, quantity, unit_price) VALUES (?, ?, ?, ?, ?)',
        (invoice_id, inventory_item_id, description, qty_val, price_val)
    )
    db.commit()
    log_activity(f"{g.user['name']} added '{description}' x{qty_val:g} to invoice #{invoice_id}")
    flash(f'Added "{description}" to the order. Click "Update & Print KOT" once you\'re done adding/removing items.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


@bp.route('/<int:invoice_id>/items/<int:item_id>/remove', methods=('POST',))
@login_required
def remove_item(invoice_id, item_id):
    db = get_db()
    invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if invoice is None:
        flash('Invoice not found.')
        return redirect(url_for('invoices.list_invoices'))

    if invoice['status'] not in ('draft', 'sent'):
        flash('Items can only be removed before an invoice is settled.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    remaining = db.execute('SELECT COUNT(*) AS c FROM invoice_items WHERE invoice_id = ?', (invoice_id,)).fetchone()['c']
    if remaining <= 1:
        flash("Can't remove the last item on an invoice — delete the invoice instead if it's no longer needed.")
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    item = db.execute('SELECT * FROM invoice_items WHERE id = ? AND invoice_id = ?', (item_id, invoice_id)).fetchone()
    if item is None:
        flash('Item not found on this invoice.')
        return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))

    db.execute('DELETE FROM invoice_items WHERE id = ?', (item_id,))
    db.commit()
    log_activity(f"{g.user['name']} removed '{item['description']}' from invoice #{invoice_id}")
    flash(f'Removed "{item["description"]}" from the order. Click "Update & Print KOT" once you\'re done adding/removing items.')
    return redirect(url_for('invoices.view_invoice', invoice_id=invoice_id))


