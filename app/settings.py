from flask import Blueprint, flash, redirect, render_template, request, url_for

from .auth import role_required
from .db import get_setting, set_setting

bp = Blueprint('settings', __name__, url_prefix='/settings')


@bp.route('/', methods=('GET', 'POST'))
@role_required('owner')
def edit_settings():
    if request.method == 'POST':
        default_password = request.form.get('default_password', '').strip()
        kot_printer_ip = request.form.get('kot_printer_ip', '').strip()
        kot_printer_port = request.form.get('kot_printer_port', '').strip() or '9100'
        bill_printer_ip = request.form.get('bill_printer_ip', '').strip()
        bill_printer_port = request.form.get('bill_printer_port', '').strip() or '9100'
        error = None

        if not default_password or len(default_password) < 6:
            error = 'Default password must be at least 6 characters.'

        for port in (kot_printer_port, bill_printer_port):
            try:
                int(port)
            except ValueError:
                error = 'Printer ports must be numbers.'

        if error is None:
            set_setting('default_password', default_password)
            set_setting('kot_printer_ip', kot_printer_ip)
            set_setting('kot_printer_port', kot_printer_port)
            set_setting('bill_printer_ip', bill_printer_ip)
            set_setting('bill_printer_port', bill_printer_port)
            flash('Settings saved.')
            return redirect(url_for('settings.edit_settings'))

        flash(error)

    return render_template(
        'settings/edit.html',
        default_password=get_setting('default_password', 'welcome123'),
        kot_printer_ip=get_setting('kot_printer_ip', ''),
        kot_printer_port=get_setting('kot_printer_port', '9100'),
        bill_printer_ip=get_setting('bill_printer_ip', ''),
        bill_printer_port=get_setting('bill_printer_port', '9100'),
        agent_api_key=get_setting('agent_api_key', ''),
    )


@bp.route('/regenerate-agent-key', methods=('POST',))
@role_required('owner')
def regenerate_agent_key():
    import secrets
    set_setting('agent_api_key', secrets.token_hex(24))
    flash('Print agent API key regenerated. Update the key in your local print agent\'s config too, or it will stop working.')
    return redirect(url_for('settings.edit_settings'))
