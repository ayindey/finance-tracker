"""
Sends raw text to a network receipt printer over TCP (the standard "JetDirect" /
raw-printing protocol that almost all thermal/receipt printers support on port 9100).

IMPORTANT CAVEAT: this was written and tested against the documented protocol only —
there is no physical printer available to test against in development. Once you have
your printer's real IP address configured in Settings, print a test KOT/bill and check
the formatting/alignment on actual receipt paper. Thermal printers vary in character
width (typically 32, 42, or 48 characters for 58mm/80mm paper) — the ESC/POS init
command below resets the printer to its default width, and the templates here assume
a 42-column-wide printer (common for 80mm paper). If your receipts wrap oddly, tell me
your printer's paper width and I'll adjust the column widths.
"""
import socket

from .db import get_setting

KOT_REPRINT_REASONS = ['Order updated', 'Technical issues']
BILL_REPRINT_REASONS = ['Technical issues', 'Client Request']

ESC = b'\x1b'
INIT_PRINTER = ESC + b'@'          # reset printer to defaults
CUT_PAPER = ESC + b'i'             # partial cut (widely supported)
BOLD_ON = ESC + b'E' + b'\x01'
BOLD_OFF = ESC + b'E' + b'\x00'


def get_printer_config(kind):
    """kind is 'kot' or 'bill'. Returns (ip, port)."""
    ip = get_setting(f'{kind}_printer_ip', '').strip()
    port = get_setting(f'{kind}_printer_port', '9100').strip()
    return ip, port


def is_printer_configured(kind):
    ip, _ = get_printer_config(kind)
    return bool(ip)


def send_to_printer(text_bytes, kind):
    """Sends raw bytes to the configured printer (kind = 'kot' or 'bill') over TCP.
    Returns (success, message). Never raises — printer/network problems are common
    and should never break the app."""
    ip, port = get_printer_config(kind)

    if not ip:
        return False, f'No {kind.upper()} printer IP configured in Settings.'

    try:
        port_num = int(port)
    except ValueError:
        port_num = 9100

    try:
        with socket.create_connection((ip, port_num), timeout=5) as sock:
            sock.sendall(text_bytes)
        return True, 'Sent to printer.'
    except (socket.timeout, socket.error, OSError) as e:
        return False, f'Could not reach the {kind.upper()} printer at {ip}:{port_num} ({e}).'


def _line(left='', right='', width=42):
    """Left-and-right-justified line, like receipt printers commonly render."""
    if not right:
        return left
    space = width - len(left) - len(right)
    if space < 1:
        space = 1
    return left + (' ' * space) + right


def _row(cols):
    """Fixed-width column row. cols is a list of (text, col_width, align) tuples,
    align is 'l' or 'r'. Column widths should sum to the printer width — this is
    what actually keeps numbers lined up under their headers, since a plain
    left/right-justified line drifts out of alignment as digit counts vary
    row to row (e.g. 1500 vs 12000)."""
    parts = []
    for text, col_width, align in cols:
        text = str(text)[:col_width]
        parts.append(text.rjust(col_width) if align == 'r' else text.ljust(col_width))
    return ''.join(parts)


def build_kot_ticket(invoice, items, hotel_name='Riviera Hotel', reprint_reason=None):
    """Kitchen Order Ticket — printed the moment an order is punched, so kitchen/bar
    staff and whoever is packing the order can see what to prepare."""
    W = 42
    lines = []
    lines.append(hotel_name.center(W))
    lines.append('-' * W)
    if reprint_reason:
        marker = '[Updated KOT]' if reprint_reason == 'Order updated' else f'[Reprint: {reprint_reason}]'
        lines.append(marker.center(W))
        lines.append('-' * W)
    lines.append(f"Kot/Bot No: {invoice['id']}")
    lines.append(f"Table : t1")
    lines.append('-' * W)
    lines.append(_line(f"Kot/Bot No: {invoice['id']}", f"Table :t1", W))
    lines.append(f"Date: {invoice['date']}")
    lines.append(f"Cover:1  Steward:")
    lines.append('-' * W)
    lines.append(_row([('No', 4, 'l'), ('Item Name', 24, 'l'), ('Qty', 6, 'r'), ('Status', 8, 'r')]))
    lines.append('-' * W)
    for i, item in enumerate(items, start=1):
        lines.append(_row([
            (str(i), 4, 'l'),
            (item['description'], 24, 'l'),
            (f"{item['quantity']:g}", 6, 'r'),
            ('Add', 8, 'r'),
        ]))
    lines.append('-' * W)
    lines.append('Guest Name:')
    lines.append('Room No:')
    lines.append('Department:')
    lines.append('NCType:')
    lines.append('-' * W)
    lines.append('')
    lines.append('')

    text = '\n'.join(lines)
    return INIT_PRINTER + text.encode('ascii', errors='replace') + b'\n\n\n' + CUT_PAPER


def build_bill_receipt(invoice, items, subtotal, discount_amount, total,
                        hotel_name='Riviera Hotel', hotel_address='', outlet='', steward='',
                        reprint_reason=None):
    """The payment bill — printed automatically once an invoice is marked paid."""
    W = 42
    lines = []
    lines.append(hotel_name.center(W))
    if reprint_reason:
        lines.append(f'[Reprint: {reprint_reason}]'.center(W))
    if hotel_address:
        for addr_line in hotel_address.split('\n'):
            lines.append(addr_line.center(W))
    lines.append('')
    lines.append('Bill Type: NC')
    lines.append('NC Type: Entertainment')
    lines.append('')
    if outlet:
        lines.append(f"Outlet: {outlet}".center(W))
    lines.append('-' * W)
    lines.append(_line(f"DateTime: {invoice['created_at']}", f"Bill: {invoice['id']}", W))
    lines.append(_line(f"Table: t1", f"Kot/Bot: {invoice['id']}", W))
    if steward:
        lines.append(_line(f"Steward: {steward}", f"Cover: 1", W))
    lines.append('')
    lines.append('Room No:')
    lines.append(f"Guest Name: {invoice['client_name']}")
    lines.append('-' * W)
    lines.append(_row([('Menu Item', 18, 'l'), ('Qty', 6, 'r'), ('Rate', 8, 'r'), ('Amount', 10, 'r')]))
    lines.append('-' * W)
    for item in items:
        amount = item['quantity'] * item['unit_price']
        lines.append(_row([
            (item['description'], 18, 'l'),
            (f"{item['quantity']:g}", 6, 'r'),
            (f"{item['unit_price']:.0f}", 8, 'r'),
            (f"{amount:.0f}", 10, 'r'),
        ]))
    lines.append('-' * W)
    lines.append(_row([('Total Amount', 32, 'l'), (f"{subtotal:.2f}", 10, 'r')]))
    if discount_amount > 0:
        pct = f" ({invoice['discount_value']:g}%)" if invoice['discount_type'] == 'percentage' else ''
        lines.append(_row([(f"Discount{pct}", 32, 'l'), (f"-{discount_amount:.2f}", 10, 'r')]))
    lines.append(_row([('Round off', 32, 'l'), ('0.00', 10, 'r')]))
    lines.append('')
    lines.append(_row([('Net Amount NGN', 32, 'l'), (f"{total:.2f}", 10, 'r')]))
    lines.append('-' * W)
    lines.append('')
    lines.append('Guest signature'.center(W))
    lines.append('')
    lines.append('')

    text = '\n'.join(lines)
    return INIT_PRINTER + text.encode('ascii', errors='replace') + b'\n\n\n' + CUT_PAPER
