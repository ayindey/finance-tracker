import os
from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev-change-this-in-production'),
        DATABASE=os.environ.get('DATABASE_PATH', os.path.join(app.instance_path, 'finance_tracker.sqlite')),
    )

    if test_config is not None:
        app.config.update(test_config)

    from . import db
    db.init_app(app)

    # make sure the database's directory exists — only relevant for SQLite (the
    # default instance folder, or wherever DATABASE_PATH points, e.g. a Render
    # persistent disk mount). Postgres has no local file to prepare.
    if not db.is_postgres():
        os.makedirs(app.instance_path, exist_ok=True)
        os.makedirs(os.path.dirname(app.config['DATABASE']), exist_ok=True)

    from . import auth
    app.register_blueprint(auth.bp)

    from . import main
    app.register_blueprint(main.bp)

    from . import expenses
    app.register_blueprint(expenses.bp)

    from . import income
    app.register_blueprint(income.bp)

    from . import inventory
    app.register_blueprint(inventory.bp)

    from . import invoices
    app.register_blueprint(invoices.bp)

    from . import reports
    app.register_blueprint(reports.bp)

    from . import settings
    app.register_blueprint(settings.bp)

    from . import api
    app.register_blueprint(api.bp)

    from . import customers
    app.register_blueprint(customers.bp)

    from . import suppliers
    app.register_blueprint(suppliers.bp)

    from . import purchase_orders
    app.register_blueprint(purchase_orders.bp)

    from . import print_agent
    app.register_blueprint(print_agent.bp)

    # First boot on a fresh environment (e.g. a cloud deploy where nobody runs
    # 'flask init-db' by hand): create the schema automatically. Otherwise, upgrade
    # an existing database to the latest schema if needed.
    with app.app_context():
        if db.is_postgres():
            # Every statement in schema_postgres.sql and migrate_db_postgres() is
            # written as IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so it's always
            # safe to just run init_db() on every boot — no need to check first.
            db.init_db()
        elif not os.path.exists(app.config['DATABASE']):
            db.init_db()
        else:
            db.migrate_db()
            db.seed_settings(db.get_db())

    return app
