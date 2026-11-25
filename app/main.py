import os

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import RequestEntityTooLarge

from app.catalog import SPEC
from app.config import MAX_FILE, ROOT
from app.errors import DomainIssue, error_response
from app.refunds.ledger import RefundLedger
from app.refunds.routes import refund_api
from app.refunds.service import RefundService
from app.review.store import Store
from app.routes import api


def create_app(database=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 64 * 1024
    run_store = Store(database or os.getenv("WORKBENCH_DB", str(ROOT / "instance" / "workbench.sqlite3")))
    app.extensions["runs"] = run_store
    refund_ledger = RefundLedger(run_store.path)
    app.extensions["refund_ledger"] = refund_ledger
    app.extensions["refund_service"] = RefundService(refund_ledger)
    app.register_error_handler(DomainIssue, error_response)
    app.register_error_handler(RequestEntityTooLarge, lambda _: error_response(DomainIssue("FILE_TOO_LARGE", 413)))
    app.register_blueprint(api)
    app.register_blueprint(refund_api)

    @app.get("/")
    def home():
        return render_template("index.html", p=SPEC)

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    return app


app = create_app()
