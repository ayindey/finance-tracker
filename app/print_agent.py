"""
API for the local print agent — a small standalone script that runs on a computer
inside the shop (see /local_print_agent/agent.py) and bridges the cloud-hosted app
to a printer on the shop's local network, which the server itself can't reach.

Authenticated with a shared API key (Settings → Local Print Agent) rather than a
user login, since this is a machine-to-machine connection with no browser involved.
"""
from flask import Blueprint, jsonify, request

from .db import get_db, get_setting, now_str

bp = Blueprint('print_agent', __name__, url_prefix='/api/print-agent')


def _check_key():
    key = request.headers.get('X-Agent-Key') or request.args.get('key', '')
    expected = get_setting('agent_api_key', '')
    return bool(expected) and key == expected


@bp.route('/pending')
def pending_jobs():
    if not _check_key():
        return jsonify({'error': 'Invalid or missing API key'}), 401

    db = get_db()
    jobs = db.execute(
        '''SELECT id, kind, printer_ip, printer_port, content
           FROM print_jobs WHERE status = 'pending' ORDER BY id LIMIT 20'''
    ).fetchall()
    return jsonify({'jobs': [dict(j) for j in jobs]})


@bp.route('/<int:job_id>/complete', methods=('POST',))
def complete_job(job_id):
    if not _check_key():
        return jsonify({'error': 'Invalid or missing API key'}), 401

    db = get_db()
    db.execute(
        "UPDATE print_jobs SET status = 'done', completed_at = ? WHERE id = ?",
        (now_str(), job_id)
    )
    db.commit()
    return jsonify({'ok': True})


@bp.route('/<int:job_id>/failed', methods=('POST',))
def fail_job(job_id):
    if not _check_key():
        return jsonify({'error': 'Invalid or missing API key'}), 401

    error = (request.get_json(silent=True) or {}).get('error', 'Unknown error')
    db = get_db()
    db.execute(
        "UPDATE print_jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
        (error[:500], now_str(), job_id)
    )
    db.commit()
    return jsonify({'ok': True})
