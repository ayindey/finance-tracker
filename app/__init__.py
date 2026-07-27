import os
from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY='dev-change-this-in-production',
        DATABASE=os.path.join(app.instance_path, 'finance_tracker.sqlite'),
    )

    if test_config is not None:
        app.config.update(test_config)

    # make sure the instance folder exists (this is where the .sqlite file lives)
    os.makedirs(app.instance_path, exist_ok=True)

    from . import db
    db.init_app(app)

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

    # Initialize the database if it doesn't exist, otherwise migrate it.
    with app.app_context():
        if not os.path.exists(app.config['DATABASE']):
            db.init_db()
        else:
            db.migrate_db()
            db.seed_settings(db.get_db())

    return app
