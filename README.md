# Finance Tracker

A web-based financial tracker for retail businesses: income, expenses, invoices, and inventory, with role-based access (owner / manager / staff).

## How to run it

1. Install Python 3.9+ if you don't already have it.
2. Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

3. Initialize the database (creates the tables):

```
flask --app app init-db
```

4. Start the server:

```
flask --app app run --debug
```

5. Open your browser to `http://127.0.0.1:5000`. You'll land on a one-time setup page to create the owner account. After that, you're taken to the login page.

## Customizing the KOT/Bill printout layout
Both layouts live in `app/printing.py`, in two functions: `build_kot_ticket()` and `build_bill_receipt()`. Each builds the receipt line by line as plain text (there's a `_line(left, right, width)` helper for left/right-justified rows like "Total Amount ... 900.00"). Edit the text there directly — add lines, remove lines, change wording — then place a real test order to see the result on paper. `W = 42` near the top of each function is the character width per line; lower it if your paper is narrower (58mm printers are usually ~32 characters) or your text is wrapping.

## What's built

**Roles**
- **Owner**: full access, plus user management (create/delete/activate/deactivate/reset password), Settings (default password, printers)
- **Manager**: everything except user management and Settings
- **Staff**: place orders (create invoices), settle bills (mark paid), add/remove items on an order before it's settled, view inventory (name/stock/sell price only) — nothing else. Staff land on Invoices after login.

**Accounts & security**
- New users are created with a configurable default password (Settings, owner only)
- Owner can reset any user's password back to the default
- Everyone can change their own password (account dropdown next to Logout → Edit account)

**Money tracking**
- Income & expenses with timestamps, restricted to owner/manager
- **Customers**: save a customer's contact details once, then pick them from a dropdown when placing future orders (auto-fills name/contact). Their page shows total spent (paid), outstanding balance (unpaid orders), and full purchase history. Anyone can add a customer or view the list; only owner/manager can edit or delete (blocked if they have invoices on record, to keep history intact).
- **Suppliers & Purchase Orders** (owner/manager only): mirrors the invoicing system for the buying side. Create a PO against a supplier with line items pulled from inventory → mark it **Ordered** → **Received** (adds the quantities to stock and updates each item's cost price to what was just paid, so COGS stays accurate) → **Paid** (automatically records the cost as a business expense, tagged back to the PO). Supplier pages show total paid and what's currently owed. Cancelling/deleting is blocked once an order's been received or paid, since that would desync real stock/expense records.
- **Barcode scanning**: give an item a barcode (Inventory → Edit/Add item — click the barcode field, then just scan; USB/Bluetooth scanners type the code like a keyboard). Then on the "New Invoice" page and the "Add item" panel of an open order, there's a "📷 Scan barcode" field — scan an item there and it's added to the order instantly (or its quantity bumped by one if it's already on the order). **Not yet verified against a real scanner** — I don't have one in this dev environment, but the mechanism (listening for Enter after text input) is how virtually all USB/Bluetooth barcode scanners work, since they emulate a keyboard. Try it with yours and let me know if anything needs adjusting.
- **Units of measure & combos**: give an item a base unit (piece, kg, g, litre...) and optionally define alternate selling units (Dozen = 12 pieces, 500g pack, "Scoop of nuts" ≈ 50g, etc.) with their own price — the system converts to base units automatically for stock accuracy. Purchase Orders have the same conversion helper for buying in packs (enter pack cost, it works out the per-piece cost for you — solves the "cost price is per pack but stock is per piece" mix-up). Separately, **combo/kit items** (like a gift basket) are built from other inventory items — they have no stock of their own; how many you can sell is worked out live from what their components can currently support, and selling one deducts the right amount from every component automatically (tested: 2 wine bottles + 1 chocolate box per basket, sold 2 baskets, both components deducted correctly, oversell blocked, void correctly restored everything).
- **User management**: owner can edit any user's display name, email, password, and role from the Users page; everyone (any role) can edit their own display name/email and change their own password from the account dropdown. **Login lockout**: 5 wrong password attempts auto-deactivates the account with a clear message directing them to contact whoever manages their account — only the owner can reactivate (which also clears the failed-attempt count).
- Inventory with stock, cost/sell price, low-stock alerts, full stock movement history, and **search/sort** on the list
- **Damaged/spoiled stock tracking**, and it's correctable: owner/manager can go back and fix a mis-entered quantity or reason on a manually-logged movement (stock and reports recalculate automatically). System-generated entries (sales, void-restores) stay locked so they can't drift out of sync with the invoice that created them.
- Invoicing: multi-line items, stock-checked so you can't oversell, discounts (owner/manager, before settlement), **items can be added or removed from an order any time before it's settled** — each change reprints the KOT automatically so whoever's packing sees the update
- **Void** (paid, requires a reason) is distinct from **Cancel** (never paid, requires a reason)
- P&L report includes COGS, Gross Profit, and Damaged/Spoiled Loss; CSV export
- **Adjust Stock** (mark items damaged/spoiled, restock, or correct a count) is now a direct link right on the Inventory list — no need to click into an item first
- **Search/sort** on Invoices (plus a status filter), Income, Expenses, and Inventory
- Dashboard charts sit side-by-side on wider screens

**Live updates**
- Dashboard stats, invoice list/detail, inventory, income, and expenses all poll for changes every 5 seconds and refresh in place. Dashboard charts specifically don't live-refresh (a Chart.js technical limitation — swapping a canvas via innerHTML breaks the chart instance) but do update on a normal page load. Pages that are primarily data-entry forms (new invoice, settings, etc.) intentionally don't auto-refresh, so your typing never gets wiped out from under you.
- Flash messages (the little banners after an action) fade out on their own after 10 seconds.

**Printing**
- **Two separate printers**: a KOT printer (in the store) and a Bill printer (at the cashier), each with its own IP in Settings. Enter the same IP in both if you only have one, or one is down.
- KOT prints automatically the moment an order is placed, and **again every time items are added or removed** before the bill is settled.
- Bill prints automatically the moment an invoice is marked paid.
- Adding or removing items on an open order no longer auto-prints the KOT on every single change — instead there's an **"Update & Print KOT"** button you click once you're done editing, which prints the current full item list marked `[Updated KOT]`.
- Any manual KOT or Bill print requires picking a reason from a dropdown (KOT: Order updated, Technical issues; Bill: Technical issues, Client Request), and that reason is printed right on the ticket as `[Reprint: reason]` so it's clear to whoever's holding it why they're getting a second copy. The very first, automatic print of an order/bill has no such marker — only reprints do.
- On the invoice page: **"Print (browser)"** opens your OS print dialog and works with any printer set up on that computer — network or USB — even if the KOT/Bill printer's network is down. **"Reprint KOT"** and **"Reprint Bill"** resend the ticket to the configured network printer on demand. There's no PDF download anymore — reprinting replaced it.
- If a printer is unreachable, the order/payment still saves — you get a warning instead of a crash.

## Not included (out of scope for now)
Payroll, multi-currency, and automated bank reconciliation are common in larger accounting suites but are overkill for a single retail shop — happy to add any of them if the business actually needs it.

## Deploying (e.g. to Render)

The app initializes its own database automatically on first boot — no need to run `flask init-db` by hand on a server.

**Local development vs. production are automatically separated.** The app uses SQLite unless a `DATABASE_URL` environment variable is set, in which case it uses PostgreSQL instead — with zero other code changes needed. This means:
- Running locally with no `DATABASE_URL` set → always uses your local SQLite file, exactly as before. It can never accidentally touch production data.
- Render (or anywhere else) with `DATABASE_URL` set to a Postgres connection string → uses that Postgres database.

### Option A: PostgreSQL (recommended for real use)

1. On Render: create a new **PostgreSQL** instance (Dashboard → New → PostgreSQL). Free tier is fine to start.
2. Copy its **Internal Database URL** from the Postgres instance's page.
3. On your web service → **Environment** → add `DATABASE_URL` = that connection string.
4. Also set `SECRET_KEY` to a long random string (keeps everyone logged in across restarts).
5. Redeploy. The app creates all its tables automatically on first boot.

This is the durable option — Postgres has its own persistent storage, so there's no risk of losing data on restart, and no separate disk to manage.

### Option B: SQLite with a persistent disk (simpler, fine for a single small shop)

Skip this if you're using Option A. Most cloud platforms (Render included, on a standard Web Service) use an *ephemeral* filesystem: anything written to disk gets wiped on every restart or redeploy. To keep using SQLite safely:

1. On Render: go to your service → **Disks** → **Add Disk**. Give it a mount path like `/var/data`.
2. Add an environment variable: `DATABASE_PATH` = `/var/data/finance_tracker.sqlite`.
3. Set `SECRET_KEY` as above.
4. Redeploy.

### Printing when the app is cloud-hosted

The KOT/Bill auto-printing sends data to your printer's IP address. When the app and printer are on the same local network (e.g. running on your own computer), that connection just works. When the app is hosted in the cloud (Railway, Render, etc.), it **cannot** reach a printer on your shop's private local network directly — the same way nothing on the internet can reach your home router by its `192.168.x.x` address. This isn't a bug; it's how private networks work.

**This is what `local_print_agent/` solves.** It's a small standalone script (Python standard library only, no extra installs) that you run on any always-on computer inside your shop. It checks in with the cloud app over the internet, and whenever there's a print job waiting, forwards it to the printer over your shop's own network — which it *can* reach, since it's running right there. See `local_print_agent/README.md` for setup, and Settings → Local print agent in the app for the URL/API key it needs.

If you don't run the agent, KOT/Bill jobs just sit queued (harmlessly) whenever the printer isn't directly reachable, and the **"Print (browser)"** button on every page still works regardless — it opens the browser's own print dialog on whichever computer you're using, agent or not.

## Project structure

```
finance-tracker/
├── app/
│   ├── __init__.py       # app factory
│   ├── db.py              # database connection + init
│   ├── auth.py             # login, logout, user management
│   ├── main.py             # dashboard, first-time setup
│   └── templates/          # HTML pages
├── schema.sql               # database table definitions
├── requirements.txt
└── README.md
```
