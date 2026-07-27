from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from .auth import login_required, log_activity, role_required
from .db import get_db

bp = Blueprint('inventory', __name__, url_prefix='/inventory')


def get_available_stock(db, item_id):
    """How many units of this item can be sold right now. For a normal item that's
    just its stock_qty. For a combo/kit item, it's however many complete sets its
    components can currently support — a combo has no stock of its own."""
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if item is None:
        return 0
    if not item['is_combo']:
        return item['stock_qty']

    components = db.execute('SELECT * FROM combo_components WHERE combo_item_id = ?', (item_id,)).fetchall()
    if not components:
        return 0
    available = None
    for c in components:
        comp = db.execute('SELECT stock_qty FROM inventory_items WHERE id = ?', (c['component_item_id'],)).fetchone()
        comp_available = (comp['stock_qty'] // c['quantity_needed']) if comp and c['quantity_needed'] > 0 else 0
        if available is None or comp_available < available:
            available = comp_available
    return int(available or 0)


def deduct_stock_for_sale(db, item_id, qty, reason, movement_type, user_id):
    """Deducts stock for a sale. A combo explodes into its components — each
    component gets its own stock_movements row so the audit trail stays accurate
    down to the physical item, not just the combo's name."""
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if item is None:
        return
    if not item['is_combo']:
        db.execute('UPDATE inventory_items SET stock_qty = MAX(0, stock_qty - ?) WHERE id = ?', (qty, item_id))
        db.execute(
            'INSERT INTO stock_movements (inventory_item_id, change_qty, reason, movement_type, created_by) VALUES (?, ?, ?, ?, ?)',
            (item_id, -qty, reason, movement_type, user_id)
        )
    else:
        for c in db.execute('SELECT * FROM combo_components WHERE combo_item_id = ?', (item_id,)).fetchall():
            needed = c['quantity_needed'] * qty
            db.execute('UPDATE inventory_items SET stock_qty = MAX(0, stock_qty - ?) WHERE id = ?',
                       (needed, c['component_item_id']))
            db.execute(
                'INSERT INTO stock_movements (inventory_item_id, change_qty, reason, movement_type, created_by) VALUES (?, ?, ?, ?, ?)',
                (c['component_item_id'], -needed, f"{reason} (combo: {item['name']})", movement_type, user_id)
            )


def restore_stock_for_sale(db, item_id, qty, reason, movement_type, user_id):
    """Reverses deduct_stock_for_sale — used when voiding an invoice."""
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if item is None:
        return
    if not item['is_combo']:
        db.execute('UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?', (qty, item_id))
        db.execute(
            'INSERT INTO stock_movements (inventory_item_id, change_qty, reason, movement_type, created_by) VALUES (?, ?, ?, ?, ?)',
            (item_id, qty, reason, movement_type, user_id)
        )
    else:
        for c in db.execute('SELECT * FROM combo_components WHERE combo_item_id = ?', (item_id,)).fetchall():
            needed = c['quantity_needed'] * qty
            db.execute('UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?',
                       (needed, c['component_item_id']))
            db.execute(
                'INSERT INTO stock_movements (inventory_item_id, change_qty, reason, movement_type, created_by) VALUES (?, ?, ?, ?, ?)',
                (c['component_item_id'], needed, f"{reason} (combo: {item['name']})", movement_type, user_id)
            )


@bp.route('/')
@login_required
def list_items():
    db = get_db()
    items = db.execute(
        'SELECT * FROM inventory_items ORDER BY name'
    ).fetchall()
    items_with_stock = []
    for item in items:
        d = dict(item)
        if item['is_combo']:
            d['display_stock'] = get_available_stock(db, item['id'])
        else:
            d['display_stock'] = item['stock_qty']
        items_with_stock.append(d)
    total_stock_value = sum(item['stock_qty'] * item['cost_price'] for item in items if not item['is_combo'])
    return render_template('inventory/list.html', items=items_with_stock, total_stock_value=total_stock_value)


@bp.route('/new', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def new_item():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        sku = request.form.get('sku', '').strip() or None
        barcode = request.form.get('barcode', '').strip() or None
        unit_label = request.form.get('unit_label', '').strip() or 'piece'
        stock_qty = request.form.get('stock_qty', '').strip()
        cost_price = request.form.get('cost_price', '').strip()
        sell_price = request.form.get('sell_price', '').strip()
        low_stock_threshold = request.form.get('low_stock_threshold', '').strip() or '5'
        error = None

        if not name:
            error = 'Item name is required.'

        try:
            stock_qty_val = int(stock_qty)
            if stock_qty_val < 0:
                error = 'Stock quantity cannot be negative.'
        except ValueError:
            error = 'Stock quantity must be a whole number.'

        try:
            cost_price_val = float(cost_price)
            sell_price_val = float(sell_price)
            if cost_price_val < 0 or sell_price_val < 0:
                error = 'Prices cannot be negative.'
        except ValueError:
            error = 'Cost and sell price must be valid numbers.'

        try:
            threshold_val = int(low_stock_threshold)
        except ValueError:
            threshold_val = 5

        db = get_db()
        if error is None and sku:
            existing = db.execute('SELECT id FROM inventory_items WHERE sku = ?', (sku,)).fetchone()
            if existing:
                error = f'SKU "{sku}" is already in use.'

        if error is None and barcode:
            existing = db.execute('SELECT id FROM inventory_items WHERE barcode = ?', (barcode,)).fetchone()
            if existing:
                error = f'Barcode "{barcode}" is already assigned to another item.'

        unit_names = request.form.getlist('unit_name[]')
        unit_factors = request.form.getlist('unit_factor[]')
        unit_prices = request.form.getlist('unit_price[]')
        alt_units = []
        if error is None:
            for u_name, u_factor, u_price in zip(unit_names, unit_factors, unit_prices):
                u_name = u_name.strip()
                if not u_name:
                    continue
                try:
                    factor_val = float(u_factor)
                    if factor_val <= 0:
                        raise ValueError
                except ValueError:
                    error = f'Invalid conversion factor for unit "{u_name}".'
                    break
                price_val = None
                if u_price.strip():
                    try:
                        price_val = float(u_price)
                    except ValueError:
                        error = f'Invalid price for unit "{u_name}".'
                        break
                alt_units.append((u_name, factor_val, price_val))

        if error is None:
            cursor = db.execute(
                '''INSERT INTO inventory_items (name, sku, barcode, unit_label, stock_qty, cost_price, sell_price, low_stock_threshold)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (name, sku, barcode, unit_label, stock_qty_val, cost_price_val, sell_price_val, threshold_val)
            )
            item_id = cursor.lastrowid
            for u_name, factor_val, price_val in alt_units:
                db.execute(
                    'INSERT INTO item_units (inventory_item_id, unit_name, conversion_factor, sell_price) VALUES (?, ?, ?, ?)',
                    (item_id, u_name, factor_val, price_val)
                )
            db.commit()
            log_activity(f"{g.user['name']} added inventory item '{name}'")
            flash(f'"{name}" added to inventory.')
            return redirect(url_for('inventory.list_items'))

        flash(error)

    return render_template('inventory/form.html', item=None, item_units=[])


@bp.route('/<int:item_id>/edit', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def edit_item(item_id):
    db = get_db()
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if item is None:
        flash('Item not found.')
        return redirect(url_for('inventory.list_items'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        sku = request.form.get('sku', '').strip() or None
        barcode = request.form.get('barcode', '').strip() or None
        unit_label = request.form.get('unit_label', '').strip() or 'piece'
        stock_qty = request.form.get('stock_qty', '').strip()
        cost_price = request.form.get('cost_price', '').strip()
        sell_price = request.form.get('sell_price', '').strip()
        low_stock_threshold = request.form.get('low_stock_threshold', '').strip() or '5'
        error = None

        if not name:
            error = 'Item name is required.'

        try:
            stock_qty_val = int(stock_qty)
            if stock_qty_val < 0:
                error = 'Stock quantity cannot be negative.'
        except ValueError:
            error = 'Stock quantity must be a whole number.'

        try:
            cost_price_val = float(cost_price)
            sell_price_val = float(sell_price)
            if cost_price_val < 0 or sell_price_val < 0:
                error = 'Prices cannot be negative.'
        except ValueError:
            error = 'Cost and sell price must be valid numbers.'

        try:
            threshold_val = int(low_stock_threshold)
        except ValueError:
            threshold_val = 5

        if error is None and sku:
            existing = db.execute(
                'SELECT id FROM inventory_items WHERE sku = ? AND id != ?', (sku, item_id)
            ).fetchone()
            if existing:
                error = f'SKU "{sku}" is already in use.'

        if error is None and barcode:
            existing = db.execute(
                'SELECT id FROM inventory_items WHERE barcode = ? AND id != ?', (barcode, item_id)
            ).fetchone()
            if existing:
                error = f'Barcode "{barcode}" is already assigned to another item.'

        unit_names = request.form.getlist('unit_name[]')
        unit_factors = request.form.getlist('unit_factor[]')
        unit_prices = request.form.getlist('unit_price[]')
        alt_units = []
        if error is None:
            for u_name, u_factor, u_price in zip(unit_names, unit_factors, unit_prices):
                u_name = u_name.strip()
                if not u_name:
                    continue
                try:
                    factor_val = float(u_factor)
                    if factor_val <= 0:
                        raise ValueError
                except ValueError:
                    error = f'Invalid conversion factor for unit "{u_name}".'
                    break
                price_val = None
                if u_price.strip():
                    try:
                        price_val = float(u_price)
                    except ValueError:
                        error = f'Invalid price for unit "{u_name}".'
                        break
                alt_units.append((u_name, factor_val, price_val))

        if error is None:
            db.execute(
                '''UPDATE inventory_items
                   SET name = ?, sku = ?, barcode = ?, unit_label = ?, stock_qty = ?, cost_price = ?, sell_price = ?, low_stock_threshold = ?
                   WHERE id = ?''',
                (name, sku, barcode, unit_label, stock_qty_val, cost_price_val, sell_price_val, threshold_val, item_id)
            )
            db.execute('DELETE FROM item_units WHERE inventory_item_id = ?', (item_id,))
            for u_name, factor_val, price_val in alt_units:
                db.execute(
                    'INSERT INTO item_units (inventory_item_id, unit_name, conversion_factor, sell_price) VALUES (?, ?, ?, ?)',
                    (item_id, u_name, factor_val, price_val)
                )
            db.commit()
            log_activity(f"{g.user['name']} updated inventory item '{name}'")
            flash(f'"{name}" updated.')
            return redirect(url_for('inventory.list_items'))

        flash(error)

    item_units = db.execute('SELECT * FROM item_units WHERE inventory_item_id = ?', (item_id,)).fetchall()
    return render_template('inventory/form.html', item=item, item_units=item_units)


@bp.route('/new-combo', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def new_combo():
    db = get_db()
    available_items = db.execute(
        'SELECT * FROM inventory_items WHERE is_combo = 0 ORDER BY name'
    ).fetchall()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        sell_price = request.form.get('sell_price', '').strip()
        low_stock_threshold = request.form.get('low_stock_threshold', '').strip() or '5'
        component_ids = request.form.getlist('component_item_id[]')
        component_qtys = request.form.getlist('component_qty[]')
        error = None

        if not name:
            error = 'Name is required.'

        try:
            sell_price_val = float(sell_price)
            if sell_price_val < 0:
                raise ValueError
        except ValueError:
            error = 'Enter a valid sell price.'

        try:
            threshold_val = int(low_stock_threshold)
        except ValueError:
            threshold_val = 5

        components = []
        if error is None:
            for comp_id, comp_qty in zip(component_ids, component_qtys):
                if not comp_id:
                    continue
                try:
                    qty_val = float(comp_qty)
                    if qty_val <= 0:
                        raise ValueError
                except ValueError:
                    error = 'Enter a valid quantity for each component.'
                    break
                components.append((int(comp_id), qty_val))

            if not components and error is None:
                error = 'Add at least one component item.'

        if error is None:
            cursor = db.execute(
                '''INSERT INTO inventory_items (name, unit_label, is_combo, stock_qty, cost_price, sell_price, low_stock_threshold)
                   VALUES (?, 'set', 1, 0, 0, ?, ?)''',
                (name, sell_price_val, threshold_val)
            )
            combo_id = cursor.lastrowid
            for comp_id, qty_val in components:
                db.execute(
                    'INSERT INTO combo_components (combo_item_id, component_item_id, quantity_needed) VALUES (?, ?, ?)',
                    (combo_id, comp_id, qty_val)
                )
            db.commit()
            log_activity(f"{g.user['name']} created combo item '{name}' with {len(components)} component(s)")
            flash(f'Combo "{name}" created.')
            return redirect(url_for('inventory.list_items'))

        flash(error)

    return render_template('inventory/combo_form.html', combo=None, components=[], available_items=available_items)


@bp.route('/<int:item_id>/edit-combo', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def edit_combo(item_id):
    db = get_db()
    combo = db.execute('SELECT * FROM inventory_items WHERE id = ? AND is_combo = 1', (item_id,)).fetchone()
    if combo is None:
        flash('Combo item not found.')
        return redirect(url_for('inventory.list_items'))

    available_items = db.execute(
        'SELECT * FROM inventory_items WHERE is_combo = 0 ORDER BY name'
    ).fetchall()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        sell_price = request.form.get('sell_price', '').strip()
        low_stock_threshold = request.form.get('low_stock_threshold', '').strip() or '5'
        component_ids = request.form.getlist('component_item_id[]')
        component_qtys = request.form.getlist('component_qty[]')
        error = None

        if not name:
            error = 'Name is required.'

        try:
            sell_price_val = float(sell_price)
            if sell_price_val < 0:
                raise ValueError
        except ValueError:
            error = 'Enter a valid sell price.'

        try:
            threshold_val = int(low_stock_threshold)
        except ValueError:
            threshold_val = 5

        components = []
        if error is None:
            for comp_id, comp_qty in zip(component_ids, component_qtys):
                if not comp_id:
                    continue
                try:
                    qty_val = float(comp_qty)
                    if qty_val <= 0:
                        raise ValueError
                except ValueError:
                    error = 'Enter a valid quantity for each component.'
                    break
                components.append((int(comp_id), qty_val))

            if not components and error is None:
                error = 'Add at least one component item.'

        if error is None:
            db.execute(
                'UPDATE inventory_items SET name = ?, sell_price = ?, low_stock_threshold = ? WHERE id = ?',
                (name, sell_price_val, threshold_val, item_id)
            )
            db.execute('DELETE FROM combo_components WHERE combo_item_id = ?', (item_id,))
            for comp_id, qty_val in components:
                db.execute(
                    'INSERT INTO combo_components (combo_item_id, component_item_id, quantity_needed) VALUES (?, ?, ?)',
                    (item_id, comp_id, qty_val)
                )
            db.commit()
            log_activity(f"{g.user['name']} updated combo item '{name}'")
            flash(f'Combo "{name}" updated.')
            return redirect(url_for('inventory.list_items'))

        flash(error)

    components = db.execute(
        '''SELECT combo_components.*, inventory_items.name AS component_name
           FROM combo_components JOIN inventory_items ON combo_components.component_item_id = inventory_items.id
           WHERE combo_item_id = ?''', (item_id,)
    ).fetchall()
    return render_template('inventory/combo_form.html', combo=combo, components=components, available_items=available_items)


@bp.route('/lookup-barcode')
@login_required
def lookup_barcode():
    code = request.args.get('code', '').strip()
    if not code:
        return jsonify({'found': False})
    db = get_db()
    item = db.execute('SELECT * FROM inventory_items WHERE barcode = ?', (code,)).fetchone()
    if item is None:
        return jsonify({'found': False})
    return jsonify({
        'found': True,
        'id': item['id'],
        'name': item['name'],
        'price': item['sell_price'],
        'stock': item['stock_qty'],
    })


@bp.route('/movements')
@role_required('owner', 'manager')
def all_movements():
    db = get_db()
    movements = db.execute(
        '''SELECT stock_movements.*, users.name AS created_by_name, inventory_items.name AS item_name
           FROM stock_movements
           LEFT JOIN users ON stock_movements.created_by = users.id
           LEFT JOIN inventory_items ON stock_movements.inventory_item_id = inventory_items.id
           ORDER BY stock_movements.created_at DESC
           LIMIT 500'''
    ).fetchall()
    return render_template('inventory/all_movements.html', movements=movements)


@bp.route('/<int:item_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_item(item_id):
    db = get_db()
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    db.execute('DELETE FROM inventory_items WHERE id = ?', (item_id,))
    db.commit()
    if item:
        log_activity(f"{g.user['name']} deleted inventory item '{item['name']}'")
    flash('Item deleted.')
    return redirect(url_for('inventory.list_items'))


@bp.route('/<int:item_id>/adjust', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def adjust_stock(item_id):
    db = get_db()
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if item is None:
        flash('Item not found.')
        return redirect(url_for('inventory.list_items'))

    if request.method == 'POST':
        direction = request.form.get('direction')
        movement_type = request.form.get('movement_type', 'adjustment')
        amount = request.form.get('amount', '').strip()
        reason = request.form.get('reason', '').strip()
        error = None

        try:
            amount_val = float(amount)
            if amount_val <= 0:
                raise ValueError
        except ValueError:
            error = 'Enter a quantity greater than zero.'

        if not reason:
            error = 'A reason is required (e.g. "Restock", "Damaged", "Stock count correction").'

        if direction not in ('add', 'remove'):
            error = 'Invalid direction.'

        if movement_type not in ('restock', 'damage', 'correction'):
            movement_type = 'adjustment'

        if error is None:
            change = amount_val if direction == 'add' else -amount_val
            if direction == 'remove' and amount_val > item['stock_qty']:
                error = f'Cannot remove {amount_val:g} — only {item["stock_qty"]:g} in stock.'

        if error is None:
            db.execute(
                'UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?',
                (change, item_id)
            )
            db.execute(
                'INSERT INTO stock_movements (inventory_item_id, change_qty, reason, movement_type, created_by) VALUES (?, ?, ?, ?, ?)',
                (item_id, change, reason, movement_type, g.user['id'])
            )
            db.commit()
            log_activity(f"{g.user['name']} adjusted stock for '{item['name']}' by {change:+g} ({reason})")
            flash(f'Stock updated: {change:+g} ({reason}).')
            return redirect(url_for('inventory.stock_history', item_id=item_id))

        flash(error)

    return render_template('inventory/adjust.html', item=item)


@bp.route('/<int:item_id>/history')
@login_required
def stock_history(item_id):
    db = get_db()
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if item is None:
        flash('Item not found.')
        return redirect(url_for('inventory.list_items'))

    movements = db.execute(
        '''SELECT stock_movements.*, users.name AS created_by_name
           FROM stock_movements
           LEFT JOIN users ON stock_movements.created_by = users.id
           WHERE inventory_item_id = ?
           ORDER BY created_at DESC''',
        (item_id,)
    ).fetchall()
    return render_template('inventory/history.html', item=item, movements=movements)


# Movements the owner/manager entered by hand can be corrected later (e.g. a fruit
# seller logs 5 spoiled mangoes, then realizes it was actually 8). Movements that come
# from the invoice/order flow ('sale', 'void_restore') stay locked, since editing those
# would desync stock from the invoice records that generated them.
EDITABLE_MOVEMENT_TYPES = ('restock', 'damage', 'correction', 'adjustment')


@bp.route('/<int:item_id>/movements/<int:movement_id>/edit', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def edit_movement(item_id, movement_id):
    db = get_db()
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    movement = db.execute(
        'SELECT * FROM stock_movements WHERE id = ? AND inventory_item_id = ?', (movement_id, item_id)
    ).fetchone()
    if item is None or movement is None:
        flash('Stock movement not found.')
        return redirect(url_for('inventory.list_items'))

    if movement['movement_type'] not in EDITABLE_MOVEMENT_TYPES:
        flash('This entry was generated automatically from an order and can\'t be edited directly.')
        return redirect(url_for('inventory.stock_history', item_id=item_id))

    if request.method == 'POST':
        direction = request.form.get('direction')
        movement_type = request.form.get('movement_type', 'adjustment')
        amount = request.form.get('amount', '').strip()
        reason = request.form.get('reason', '').strip()
        error = None

        try:
            amount_val = float(amount)
            if amount_val <= 0:
                raise ValueError
        except ValueError:
            error = 'Enter a quantity greater than zero.'

        if not reason:
            error = 'A reason is required.'

        if direction not in ('add', 'remove'):
            error = 'Invalid direction.'

        if movement_type not in EDITABLE_MOVEMENT_TYPES:
            movement_type = 'adjustment'

        if error is None:
            new_change = amount_val if direction == 'add' else -amount_val
            # Reverse the old effect, then apply the new one, in a single step.
            adjustment = new_change - movement['change_qty']
            resulting_stock = item['stock_qty'] + adjustment
            if resulting_stock < 0:
                error = (f'That change would take stock below zero (would result in '
                         f'{resulting_stock:g}). Current stock: {item["stock_qty"]:g}.')

        if error is None:
            db.execute('UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?', (adjustment, item_id))
            db.execute(
                'UPDATE stock_movements SET change_qty = ?, reason = ?, movement_type = ? WHERE id = ?',
                (new_change, reason, movement_type, movement_id)
            )
            db.commit()
            log_activity(f"{g.user['name']} corrected a stock movement for '{item['name']}' "
                         f"(was {movement['change_qty']:+g}, now {new_change:+g}): {reason}")
            flash('Stock movement corrected.')
            return redirect(url_for('inventory.stock_history', item_id=item_id))

        flash(error)

    return render_template('inventory/edit_movement.html', item=item, movement=movement)


@bp.route('/<int:item_id>/movements/<int:movement_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_movement(item_id, movement_id):
    db = get_db()
    item = db.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    movement = db.execute(
        'SELECT * FROM stock_movements WHERE id = ? AND inventory_item_id = ?', (movement_id, item_id)
    ).fetchone()
    if item is None or movement is None:
        flash('Stock movement not found.')
        return redirect(url_for('inventory.list_items'))

    if movement['movement_type'] not in EDITABLE_MOVEMENT_TYPES:
        flash('This entry was generated automatically from an order and can\'t be deleted directly.')
        return redirect(url_for('inventory.stock_history', item_id=item_id))

    resulting_stock = item['stock_qty'] - movement['change_qty']
    if resulting_stock < 0:
        flash(f'Deleting this would take stock below zero (would result in {resulting_stock:g}). Fix it with "Edit" instead.')
        return redirect(url_for('inventory.stock_history', item_id=item_id))

    db.execute('UPDATE inventory_items SET stock_qty = stock_qty - ? WHERE id = ?', (movement['change_qty'], item_id))
    db.execute('DELETE FROM stock_movements WHERE id = ?', (movement_id,))
    db.commit()
    log_activity(f"{g.user['name']} deleted a stock movement for '{item['name']}' ({movement['change_qty']:+g}, {movement['reason']})")
    flash('Stock movement deleted and stock adjusted back.')
    return redirect(url_for('inventory.stock_history', item_id=item_id))
