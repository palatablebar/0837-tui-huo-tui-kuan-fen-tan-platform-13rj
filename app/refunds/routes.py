from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from app.config import SAMPLES
from app.errors import DomainIssue


refund_api = Blueprint("refund_api", __name__)


def service():
    return current_app.extensions["refund_service"]


def body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if isinstance(data, list):
        return {"payments": data}
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    return data


def payments_from(data: dict[str, Any]):
    payments = data.get("payments")
    if payments is None:
        payments = data.get("payment_channels")
    if payments is None:
        payments = data.get("channels")
    if payments is None and ("channel" in data or "amount" in data):
        payments = [data]
    return payments


# ---------------- 付款构成补数据 ----------------

@refund_api.get("/api/examples/payment-compositions")
def example_payment_compositions():
    path = SAMPLES / "payment_channels.json"
    return current_app.response_class(path.read_text(encoding="utf-8"), mimetype="application/json")


@refund_api.get("/api/orders/<order_id>/payments")
@refund_api.get("/api/orders/<order_id>/payment-channels")
def get_order_payments(order_id: str):
    return jsonify(service().get_payments(order_id))


@refund_api.put("/api/orders/<order_id>/payments")
@refund_api.post("/api/orders/<order_id>/payments")
@refund_api.put("/api/orders/<order_id>/payment-channels")
@refund_api.post("/api/orders/<order_id>/payment-channels")
def save_order_payments(order_id: str):
    data = body()
    payments = payments_from(data)
    if payments is None:
        raise DomainIssue("PAYMENTS_REQUIRED")
    collection = any(key in data for key in ("payments", "payment_channels", "channels"))
    append = bool(data.get("append")) or (request.method == "POST" and not collection)
    unit = data.get("unit", "cents")
    if append:
        result = service().add_payments(order_id, payments, unit=unit)
    else:
        result = service().save_payments(
            order_id,
            payments,
            expected_revision=data.get("expected_revision") or data.get("revision"),
            unit=unit,
        )
    return jsonify(result)


@refund_api.get("/api/payments")
@refund_api.get("/api/payment-compositions")
def list_payments():
    payments = service().list_payments()
    return jsonify({"items": payments, "payments": payments, "total": len(payments)})


@refund_api.post("/api/payments")
@refund_api.post("/api/payment-compositions")
def save_payments():
    data = body()
    order_id = str(data.get("order_id") or data.get("id") or "")
    if not order_id:
        raise DomainIssue("ORDER_ID_REQUIRED")
    payments = payments_from(data)
    if payments is None:
        raise DomainIssue("PAYMENTS_REQUIRED")
    collection = any(key in data for key in ("payments", "payment_channels", "channels"))
    if data.get("append") or not collection:
        result = service().add_payments(order_id, payments, unit=data.get("unit", "cents"))
    else:
        result = service().save_payments(
            order_id, payments, expected_revision=data.get("expected_revision") or data.get("revision"),
            unit=data.get("unit", "cents")
        )
    return jsonify(result)


# ---------------- 试算 ----------------

@refund_api.get("/api/orders/<order_id>/channel-balances")
@refund_api.get("/api/orders/<order_id>/balances")
def order_channel_balances(order_id: str):
    return jsonify(service().channel_balances(order_id))


@refund_api.get("/api/orders/<order_id>/refunds")
@refund_api.get("/api/orders/<order_id>/refund-splits")
def list_order_refunds(order_id: str):
    return jsonify(service().list_refunds(order_id=order_id))


@refund_api.post("/api/orders/<order_id>/refunds")
@refund_api.post("/api/orders/<order_id>/refund-splits")
@refund_api.post("/api/orders/<order_id>/refund-split")
def create_order_refund(order_id: str):
    data = body()
    data["order_id"] = order_id
    return jsonify(service().create_split(data))


@refund_api.post("/api/refunds/preview")
@refund_api.post("/api/refunds/split")
@refund_api.post("/api/refund-splits/preview")
@refund_api.post("/api/refund-splits/split")
@refund_api.post("/api/splits/preview")
def preview_split():
    return jsonify(service().preview(body()))


# ---------------- 退款单 / 拆分单 ----------------

@refund_api.get("/api/refunds")
@refund_api.get("/api/refund-splits")
@refund_api.get("/api/splits")
def list_refunds():
    order_id = request.args.get("order_id")
    run_id = request.args.get("run_id")
    return jsonify(service().list_refunds(order_id=order_id, run_id=run_id))


@refund_api.post("/api/refunds")
@refund_api.post("/api/refund-splits")
@refund_api.post("/api/refund-split")
@refund_api.post("/api/splits")
def create_refund():
    return jsonify(service().create_split(body()))


@refund_api.post("/api/refunds/rerun")
@refund_api.post("/api/refund-splits/rerun")
@refund_api.post("/api/splits/rerun")
def rerun_by_key():
    data = body()
    refund_id = data.get("refund_id") or data.get("id")
    if not refund_id and data.get("refund_key"):
        existing = service().ledger.get_refund_by_key(str(data["refund_key"]))
        if not existing:
            raise DomainIssue("REFUND_NOT_FOUND", 404)
        refund_id = existing["id"]
    if not refund_id:
        raise DomainIssue("REFUND_ID_REQUIRED")
    return jsonify(service().rerun(str(refund_id), data))


@refund_api.post("/api/refunds/issue")
@refund_api.post("/api/refund-splits/issue")
def issue_by_key():
    data = body()
    refund_id = data.get("refund_id") or data.get("id")
    if not refund_id and data.get("refund_key"):
        existing = service().ledger.get_refund_by_key(str(data["refund_key"]))
        if not existing:
            raise DomainIssue("REFUND_NOT_FOUND", 404)
        refund_id = existing["id"]
    if not refund_id:
        raise DomainIssue("REFUND_ID_REQUIRED")
    return jsonify(service().issue(str(refund_id), data.get("comment")))


@refund_api.get("/api/refunds/reconcile")
@refund_api.get("/api/refund-splits/reconcile")
@refund_api.get("/api/refund-reconciliation")
def refund_reconciliation():
    return jsonify(service().reconcile())


@refund_api.get("/api/refunds/pending")
@refund_api.get("/api/refund-splits/pending")
@refund_api.get("/api/pending-refunds")
def pending_refunds():
    data = service().list_refunds()
    waiting = []
    for refund in data["refunds"]:
        if int(refund.get("pending_amount", 0)) > 0:
            waiting.append({
                "refund_id": refund["id"],
                "refund_key": refund["refund_key"],
                "order_id": refund["order_id"],
                "status": refund["status"],
                "pending_amount": refund["pending_amount"],
                "pending_channels": refund.get("pending_channels", []),
            })
    return jsonify({"pending": waiting, "count": len(waiting)})


@refund_api.get("/api/refunds/<refund_id>")
@refund_api.get("/api/refund-splits/<refund_id>")
@refund_api.get("/api/splits/<refund_id>")
def get_refund(refund_id: str):
    result = service().get_refund(refund_id)
    result["channel_entries"] = service().ledger.channel_entries(refund_id=refund_id)
    return jsonify(result)


@refund_api.post("/api/refunds/<refund_id>/issue")
@refund_api.post("/api/refunds/<refund_id>/sign")
@refund_api.post("/api/refund-splits/<refund_id>/issue")
@refund_api.post("/api/refund-splits/<refund_id>/sign")
@refund_api.post("/api/splits/<refund_id>/issue")
def issue_refund(refund_id: str):
    data = body()
    return jsonify(service().issue(refund_id, data.get("comment")))


@refund_api.post("/api/refunds/<refund_id>/rerun")
@refund_api.post("/api/refunds/<refund_id>/recalculate")
@refund_api.post("/api/refunds/<refund_id>/recompute")
@refund_api.post("/api/refund-splits/<refund_id>/rerun")
@refund_api.post("/api/refund-splits/<refund_id>/recalculate")
@refund_api.post("/api/splits/<refund_id>/rerun")
@refund_api.post("/api/splits/<refund_id>/recalculate")
def rerun_refund(refund_id: str):
    return jsonify(service().rerun(refund_id, body()))


@refund_api.put("/api/refunds/<refund_id>")
@refund_api.patch("/api/refunds/<refund_id>")
@refund_api.put("/api/refund-splits/<refund_id>")
@refund_api.patch("/api/refund-splits/<refund_id>")
def update_refund(refund_id: str):
    data = body()
    current = service().get_refund(refund_id)
    if current.get("status") == "ISSUED":
        # 已签发资源不允许通过 PUT 改写；幂等返回原单。
        current["replayed"] = True
        return jsonify(current)
    data.setdefault("refund_key", current["refund_key"])
    data.setdefault("order_id", current["order_id"])
    data.setdefault("refund_amount", current["refund_amount"])
    return jsonify(service().rerun(refund_id, data))


@refund_api.post("/api/refunds/<refund_id>/supplement")
@refund_api.post("/api/refunds/<refund_id>/difference")
@refund_api.post("/api/refund-splits/<refund_id>/supplement")
@refund_api.post("/api/refund-splits/<refund_id>/difference")
@refund_api.post("/api/splits/<refund_id>/supplement")
def supplement_refund(refund_id: str):
    return jsonify(service().create_supplement(refund_id, body()))
