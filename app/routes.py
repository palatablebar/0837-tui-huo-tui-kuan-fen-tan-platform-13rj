import hashlib

from flask import Blueprint, current_app, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def refund_service():
    return current_app.extensions["refund_service"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    payload = pipeline(profile, upto=upto)
    if STAGE_INDEX[stage_id] >= 3:
        refund_service().attach_previews(payload)
    return jsonify(payload)


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    refund_service().attach_previews(payload)
    payment_rows = refund_service().list_payments()
    if payment_rows:
        payment_digest = hashlib.sha256(
            "|".join(row["order_id"] + ":" + str(row["revision"]) + ":" + row["digest"]
                     for row in payment_rows).encode("utf-8")
        ).hexdigest()[:24]
        payload["payment_state_digest"] = payment_digest
        payload["run_key"] = hashlib.sha256((payload["run_key"] + payment_digest).encode("utf-8")).hexdigest()[:24]
    run = create_run(store(), payload)
    records = refund_service().provision_run_refunds(run, issue=run.get("status") == "APPROVED")
    run["refund_records"] = records
    return jsonify(run)


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    run["refund_records"] = refund_service().ledger.list_refunds(run_id=run_id)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    run = approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"]))
    if data["decision"] == "APPROVED" and run.get("status") == "APPROVED":
        run["refund_records"] = refund_service().provision_run_refunds(run, issue=True)
    else:
        run["refund_records"] = refund_service().ledger.list_refunds(run_id=run_id)
    return jsonify(run)


@api.post("/api/runs/<run_id>/rerun")
def rerun_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    # 已签发批次/退款保持原金额；未签发草稿按最新补数据重新计算拆分。
    issue = run.get("status") == "APPROVED"
    run["refund_records"] = refund_service().provision_run_refunds(run, issue=issue)
    run["replayed"] = issue
    return jsonify(run)


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    refund_data = refund_service().list_refunds(run_id=run_id)
    refund_data["reconciliation"] = refund_service().reconcile()
    run["refund_ledger"] = {
        "refunds": refund_data["refunds"],
        "channel_entries": refund_data["channel_entries"],
        "summary": refund_data["summary"],
        "reconciliation": refund_data["reconciliation"],
    }
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)
