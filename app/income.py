from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .auth import login_required, log_activity, role_required
from .db import get_db

bp = Blueprint('income', __name__, url_prefix='/income')


@bp.route('/')
@role_required('owner', 'manager')
def list_income():
    db = get_db()
    rows = db.execute(
        '''SELECT income.*, categories.name AS category_name, users.name AS created_by_name
           FROM income
           LEFT JOIN categories ON income.category_id = categories.id
           LEFT JOIN users ON income.created_by = users.id
           ORDER BY income.date DESC, income.id DESC'''
    ).fetchall()
    total = sum(row['amount'] for row in rows)
    return render_template('income/list.html', income=rows, total=total)


@bp.route('/new', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def new_income():
    db = get_db()
    categories = db.execute(
        "SELECT * FROM categories WHERE type = 'income' ORDER BY name"
    ).fetchall()

    if request.method == 'POST':
        amount = request.form.get('amount', '').strip()
        category_id = request.form.get('category_id') or None
        date = request.form.get('date', '').strip()
        note = request.form.get('note', '').strip()
        error = None

        try:
            amount_val = float(amount)
            if amount_val <= 0:
                error = 'Amount must be greater than zero.'
        except ValueError:
            error = 'Amount must be a valid number.'

        if not date:
            error = 'Date is required.'

        if error is None:
            db.execute(
                'INSERT INTO income (amount, category_id, date, note, created_by) VALUES (?, ?, ?, ?, ?)',
                (amount_val, category_id, date, note, g.user['id'])
            )
            db.commit()
            log_activity(f"{g.user['name']} logged income of {amount_val}")
            flash('Income logged.')
            return redirect(url_for('income.list_income'))

        flash(error)

    return render_template('income/form.html', categories=categories, income=None)


@bp.route('/<int:income_id>/edit', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def edit_income(income_id):
    db = get_db()
    entry = db.execute('SELECT * FROM income WHERE id = ?', (income_id,)).fetchone()
    if entry is None:
        flash('Income entry not found.')
        return redirect(url_for('income.list_income'))

    categories = db.execute(
        "SELECT * FROM categories WHERE type = 'income' ORDER BY name"
    ).fetchall()

    if request.method == 'POST':
        amount = request.form.get('amount', '').strip()
        category_id = request.form.get('category_id') or None
        date = request.form.get('date', '').strip()
        note = request.form.get('note', '').strip()
        error = None

        try:
            amount_val = float(amount)
            if amount_val <= 0:
                error = 'Amount must be greater than zero.'
        except ValueError:
            error = 'Amount must be a valid number.'

        if not date:
            error = 'Date is required.'

        if error is None:
            db.execute(
                'UPDATE income SET amount = ?, category_id = ?, date = ?, note = ? WHERE id = ?',
                (amount_val, category_id, date, note, income_id)
            )
            db.commit()
            log_activity(f"{g.user['name']} edited income #{income_id}")
            flash('Income entry updated.')
            return redirect(url_for('income.list_income'))

        flash(error)

    return render_template('income/form.html', categories=categories, income=entry)


@bp.route('/<int:income_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_income(income_id):
    db = get_db()
    db.execute('DELETE FROM income WHERE id = ?', (income_id,))
    db.commit()
    log_activity(f"{g.user['name']} deleted income #{income_id}")
    flash('Income entry deleted.')
    return redirect(url_for('income.list_income'))
