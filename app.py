import os
from flask import Flask, jsonify
from argus.db import close_db, init_db


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'argus-dev-key')

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()

    @app.route('/health')
    def health():
        return jsonify({"status": "ok", "system": "ARGUS", "version": "1.0"})

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
