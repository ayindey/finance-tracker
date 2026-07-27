import functools
import sqlite3

from flask import (
    Blueprint, flash, g, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, get_setting

bp = Blueprint('auth', __name__, url_prefix='/auth')


def login_required(view):
    """Redirect to login page if the user isn't logged in."""
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for('auth.login'))
        return view(**kwargs)
    return wrapped_view


def role_required(*roles):
    """Restrict a view to specific roles, e.g. @role_required('owner')"""
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped_view(**kwargs):
            if g.user['role'] not in roles:
                flash("You don't have permission to do that.")
                return redirect(url_for('main.dashboard'))
            return view(**kwargs)
        return wrapped_view
    return decorator


def log_activity(action):
    db = get_db()
    user_id = g.user['id'] if g.user else None
    db.execute(
        'INSERT INTO activity_log (user_id, action) VALUES (?, ?)',
        (user_id, action)
    )
    db.commit()


@bp.before_app_request
def load_logged_in_user():
    """Runs before every request: attaches the current user to g.user."""
    user_id = session.get('user_id')
    if user_id is None:
        g.user = None
    else:
        g.user = get_db().execute(
            'SELECT * FROM users WHERE id = ? AND active = 1', (user_id,)
        ).fetchone()


@bp.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        db = get_db()
        error = None

        user = db.execute(
            'SELECT * FROM users WHERE email = ?', (email,)
        ).fetchone()

        if user is None:
            error = 'Incorrect email or password.'
        elif not user['active']:
            error = ('This account has been deactivated after too many failed login attempts. '
                      'Contact the owner (or whoever manages your account) to have it reactivated.')
        elif not check_password_hash(user['password_hash'], password):
            new_attempts = user['failed_attempts'] + 1
            if new_attempts >= 5:
                db.execute('UPDATE users SET failed_attempts = ?, active = 0 WHERE id = ?', (new_attempts, user['id']))
                db.commit()
                log_activity(f"{user['name']}'s account was auto-deactivated after 5 failed login attempts")
                error = ('Too many incorrect attempts — this account has been deactivated for security. '
                          'Contact the owner (or whoever manages your account) to have it reactivated.')
            else:
                db.execute('UPDATE users SET failed_attempts = ? WHERE id = ?', (new_attempts, user['id']))
                db.commit()
                remaining = 5 - new_attempts
                error = f'Incorrect email or password. {remaining} attempt(s) left before this account is locked.'

        if error is None:
            if user['failed_attempts'] > 0:
                db.execute('UPDATE users SET failed_attempts = 0 WHERE id = ?', (user['id'],))
                db.commit()
            session.clear()
            session['user_id'] = user['id']
            g.user = user
            log_activity(f"{user['name']} logged in")
            if user['role'] == 'staff':
                return redirect(url_for('invoices.list_invoices'))
            return redirect(url_for('main.dashboard'))

        flash(error)

    return render_template('auth/login.html')


@bp.route('/logout')
def logout():
    if g.user:
        log_activity(f"{g.user['name']} logged out")
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/users')
@role_required('owner')
def users():
    db = get_db()
    all_users = db.execute(
        'SELECT id, name, email, role, active, failed_attempts FROM users ORDER BY created_at'
    ).fetchall()
    return render_template('auth/users.html', users=all_users)


@bp.route('/users/<int:user_id>/edit', methods=('GET', 'POST'))
@role_required('owner')
def edit_user(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user is None:
        flash('User not found.')
        return redirect(url_for('auth.users'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        role = request.form.get('role', '')
        new_password = request.form.get('new_password', '').strip()
        error = None

        if not name or not email:
            error = 'Name and email are required.'
        elif role not in ('owner', 'manager', 'staff'):
            error = 'Invalid role.'
        elif user['id'] == g.user['id'] and role != 'owner':
            error = "You can't remove your own owner role."
        else:
            existing = db.execute('SELECT id FROM users WHERE email = ? AND id != ?', (email, user_id)).fetchone()
            if existing:
                error = f'A user with email "{email}" already exists.'

        if error is None and new_password and len(new_password) < 6:
            error = 'New password must be at least 6 characters.'

        if error is None:
            if new_password:
                db.execute(
                    'UPDATE users SET name = ?, email = ?, role = ?, password_hash = ?, failed_attempts = 0 WHERE id = ?',
                    (name, email, role, generate_password_hash(new_password), user_id)
                )
            else:
                db.execute(
                    'UPDATE users SET name = ?, email = ?, role = ? WHERE id = ?',
                    (name, email, role, user_id)
                )
            db.commit()
            log_activity(f"{g.user['name']} edited user '{name}'" + (' (password changed)' if new_password else ''))
            flash(f'"{name}" updated.')
            if user['id'] == g.user['id']:
                # Editing your own account: refresh the session's cached user data.
                g.user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
            return redirect(url_for('auth.users'))

        flash(error)

    return render_template('auth/edit_user.html', edit_user=user)


@bp.route('/users/new', methods=('GET', 'POST'))
@role_required('owner')
def new_user():
    if request.method == 'POST':
        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        role = request.form['role']

        db = get_db()
        error = None

        if not name or not email:
            error = 'Name and email are required.'
        elif role not in ('owner', 'manager', 'staff'):
            error = 'Invalid role.'
        elif db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone():
            error = f'A user with email "{email}" already exists.'

        if error is None:
            default_password = get_setting('default_password', 'welcome123')
            db.execute(
                'INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)',
                (name, email, generate_password_hash(default_password), role)
            )
            db.commit()
            log_activity(f"{g.user['name']} created new user {name} ({role})")
            flash(f'User "{name}" created with the default password. They should change it after logging in.')
            return redirect(url_for('auth.users'))

        flash(error)

    return render_template('auth/new_user.html', default_password=get_setting('default_password', 'welcome123'))


@bp.route('/users/<int:user_id>/toggle-active', methods=('POST',))
@role_required('owner')
def toggle_active(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user is None:
        flash('User not found.')
    elif user['id'] == g.user['id']:
        flash("You can't deactivate your own account.")
    else:
        new_status = 0 if user['active'] else 1
        if new_status:
            db.execute('UPDATE users SET active = ?, failed_attempts = 0 WHERE id = ?', (new_status, user_id))
        else:
            db.execute('UPDATE users SET active = ? WHERE id = ?', (new_status, user_id))
        db.commit()
        log_activity(f"{g.user['name']} {'activated' if new_status else 'deactivated'} {user['name']}")
    return redirect(url_for('auth.users'))


@bp.route('/users/<int:user_id>/reset-password', methods=('POST',))
@role_required('owner')
def reset_password(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user is None:
        flash('User not found.')
        return redirect(url_for('auth.users'))

    default_password = get_setting('default_password', 'welcome123')
    db.execute(
        'UPDATE users SET password_hash = ?, failed_attempts = 0 WHERE id = ?',
        (generate_password_hash(default_password), user_id)
    )
    db.commit()
    log_activity(f"{g.user['name']} reset the password for {user['name']} to the default")
    flash(f"{user['name']}'s password has been reset to the default password.")
    return redirect(url_for('auth.users'))


@bp.route('/users/<int:user_id>/delete', methods=('POST',))
@role_required('owner')
def delete_user(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user is None:
        flash('User not found.')
    elif user['id'] == g.user['id']:
        flash("You can't delete your own account.")
    else:
        try:
            db.execute('DELETE FROM users WHERE id = ?', (user_id,))
            db.commit()
            log_activity(f"{g.user['name']} deleted user {user['name']}")
            flash(f"{user['name']} has been deleted.")
        except sqlite3.IntegrityError:
            db.rollback()
            flash(f"{user['name']} can't be deleted because they have existing activity "
                  f"(invoices, logged income/expenses, etc.) that needs to stay linked to them "
                  f"for accurate records. Deactivate the account instead to block their access.")
    return redirect(url_for('auth.users'))


@bp.route('/change-password', methods=('GET', 'POST'))
@bp.route('/my-account', methods=('GET', 'POST'))
@login_required
def my_account():
    db = get_db()

    if request.method == 'POST':
        form_type = request.form.get('form_type')

        if form_type == 'profile':
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip().lower()
            error = None

            if not name or not email:
                error = 'Name and email are required.'
            else:
                existing = db.execute('SELECT id FROM users WHERE email = ? AND id != ?', (email, g.user['id'])).fetchone()
                if existing:
                    error = f'A user with email "{email}" already exists.'

            if error is None:
                db.execute('UPDATE users SET name = ?, email = ? WHERE id = ?', (name, email, g.user['id']))
                db.commit()
                log_activity(f"{g.user['name']} updated their display name/email")
                flash('Account details updated.')
                return redirect(url_for('auth.my_account'))

            flash(error)

        elif form_type == 'password':
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            error = None

            if not check_password_hash(g.user['password_hash'], current_password):
                error = 'Current password is incorrect.'
            elif len(new_password) < 6:
                error = 'New password must be at least 6 characters.'
            elif new_password != confirm_password:
                error = 'New password and confirmation do not match.'

            if error is None:
                db.execute(
                    'UPDATE users SET password_hash = ? WHERE id = ?',
                    (generate_password_hash(new_password), g.user['id'])
                )
                db.commit()
                log_activity(f"{g.user['name']} changed their password")
                flash('Password changed.')
                return redirect(url_for('auth.my_account'))

            flash(error)

    return render_template('auth/my_account.html')
