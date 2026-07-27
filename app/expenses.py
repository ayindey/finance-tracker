from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .auth import login_required, log_activity, role_required
from .db import get_db

bp = Blueprint('expenses', __name__, url_prefix='/expenses')


@bp.route('/')
@role_required('owner', 'manager')
def list_expenses():
    db = get_db()
    rows = db.execute(
        '''SELECT expenses.*, categories.name AS category_name, users.name AS created_by_name
           FROM expenses
           LEFT JOIN categories ON expenses.category_id = categories.id
           LEFT JOIN users ON expenses.created_by = users.id
           ORDER BY expenses.date DESC, expenses.id DESC'''
    ).fetchall()
    total = sum(row['amount'] for row in rows)
    return render_template('expenses/list.html', expenses=rows, total=total)


@bp.route('/new', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def new_expense():
    db = get_db()
    categories = db.execute(
        "SELECT * FROM categories WHERE type = 'expense' ORDER BY name"
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
                'INSERT INTO expenses (amount, category_id, date, note, created_by) VALUES (?, ?, ?, ?, ?)',
                (amount_val, category_id, date, note, g.user['id'])
            )
            db.commit()
            log_activity(f"{g.user['name']} logged an expense of {amount_val}")
            flash('Expense logged.')
            return redirect(url_for('expenses.list_expenses'))

        flash(error)

    return render_template('expenses/form.html', categories=categories, expense=None)


@bp.route('/<int:expense_id>/edit', methods=('GET', 'POST'))
@role_required('owner', 'manager')
def edit_expense(expense_id):
    db = get_db()
    expense = db.execute('SELECT * FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    if expense is None:
        flash('Expense not found.')
        return redirect(url_for('expenses.list_expenses'))

    categories = db.execute(
        "SELECT * FROM categories WHERE type = 'expense' ORDER BY name"
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
                'UPDATE expenses SET amount = ?, category_id = ?, date = ?, note = ? WHERE id = ?',
                (amount_val, category_id, date, note, expense_id)
            )
            db.commit()
            log_activity(f"{g.user['name']} edited expense #{expense_id}")
            flash('Expense updated.')
            return redirect(url_for('expenses.list_expenses'))

        flash(error)

    return render_template('expenses/form.html', categories=categories, expense=expense)


@bp.route('/<int:expense_id>/delete', methods=('POST',))
@role_required('owner', 'manager')
def delete_expense(expense_id):
    db = get_db()
    db.execute('DELETE FROM expenses WHERE id = ?', (expense_id,))
    db.commit()
    log_activity(f"{g.user['name']} deleted expense #{expense_id}")
    flash('Expense deleted.')
    return redirect(url_for('expenses.list_expenses'))
