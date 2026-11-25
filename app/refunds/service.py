from __future__ import annotations

import json
import uuid
from typing import Any

from app.errors import DomainIssue
from app.evidence.manifest import stable_digest
from app.refunds.allocation import as_cents, channel_label, normalize_payments, split_refund
from app.refunds.ledger import RefundLedger


class RefundService:
    def __init__(self, ledger: RefundLedger | None = None):
        self.ledger = ledger or RefundLedger(":memory:")

    @staticmethod
    def _convert_payment_units(payments: Any, unit: str) -> Any:
        if unit != "yuan" or payments is None:
            return payments
        if isinstance(payments, dict):
            return {key: RefundService._convert_payment_units(value, unit) for key, value in payments.items()}
        if not isinstance(payments, list):
            return payments
        converted = []
        money_keys = ("amount", "paid", "paid_amount", "payment_amount", "original_amount", "value", "total")
        for raw in payments:
            if not isinstance(raw, dict):
                converted.append(raw)
                continue
            item = dict(raw)
            for key in money_keys:
                if key in item and item[key] not in (None, ""):
                    item["amount_cents"] = as_cents(item[key], yuan=True)
                    break
            for key in ("line_amounts", "allocations"):
                if isinstance(item.get(key), dict):
                    item[key] = {line_id: as_cents(value, yuan=True) for line_id, value in item[key].items()}
            converted.append(item)
        return converted

    # ---------- 付款构成补数据 ----------

    def save_payments(self, order_id: str, payments: Any, *, expected_revision: int | None = None,
                     unit: str = "cents") -> dict[str, Any]:
        if not order_id:
            raise DomainIssue("ORDER_ID_REQUIRED")
        payments = self._convert_payment_units(payments, unit)
        try:
            normalized = normalize_payments(payments)
        except ValueError as exc:
            raise DomainIssue(str(exc) or "BAD_REQUEST") from exc
        if not normalized:
            raise DomainIssue("PAYMENTS_REQUIRED")
        for payment in normalized:
            if payment["amount"] <= 0:
                raise DomainIssue("BAD_AMOUNT")

        existing = self.ledger.get_payments(str(order_id))
        if expected_revision is not None and existing and int(expected_revision) != int(existing["revision"]):
            raise DomainIssue("STALE_PAYMENT_REVISION", 409)

        digest = stable_digest(normalized)
        row, changed = self.ledger.upsert_payments(str(order_id), json.dumps(normalized, ensure_ascii=False), digest)
        assert row is not None
        result = self._payment_json(row)
        result["changed"] = changed
        if changed:
            result["stale_refund_ids"] = [
                item["id"] for item in self.ledger.list_refunds(str(order_id)) if item.get("status") == "STALE"
            ]
        else:
            result["stale_refund_ids"] = []
        return result

    def add_payments(self, order_id: str, payments: Any, *, unit: str = "cents") -> dict[str, Any]:
        """追加一笔或多笔支付；整单构成替换请使用 save_payments。"""
        order_id = str(order_id)
        converted = self._convert_payment_units(payments, unit)
        try:
            incoming = normalize_payments(converted)
        except ValueError as exc:
            raise DomainIssue(str(exc) or "BAD_REQUEST") from exc
        row = self.ledger.get_payments(order_id)
        existing = json.loads(row["payments"]) if row else []
        return self.save_payments(order_id, existing + incoming, unit="cents")

    def get_payments(self, order_id: str) -> dict[str, Any]:
        row = self.ledger.get_payments(order_id)
        if row is None:
            raise DomainIssue("PAYMENTS_NOT_FOUND", 404)
        return self._payment_json(row)

    def list_payments(self) -> list[dict[str, Any]]:
        return [self._payment_json(row) for row in self.ledger.list_payments()]

    def reconcile(self) -> dict[str, Any]:
        refunds = self.ledger.list_refunds()
        entries = self.ledger.channel_entries()
        entries_by_refund: dict[str, int] = {}
        for entry in entries:
            entries_by_refund[entry["refund_id"]] = entries_by_refund.get(entry["refund_id"], 0) + int(entry["amount"])

        violations: list[dict[str, Any]] = []
        for refund in refunds:
            if refund.get("status") != "ISSUED":
                continue
            due = int(refund.get("due_amount", refund.get("refund_amount", 0)))
            allocated = int(refund.get("allocated_amount", 0))
            pending = int(refund.get("pending_amount", 0))
            if allocated + pending != due:
                violations.append({"code": "REFUND_CONSERVATION_FAILED", "refund_id": refund["id"]})
            if entries_by_refund.get(refund["id"], 0) != allocated:
                violations.append({"code": "CHANNEL_LEDGER_MISMATCH", "refund_id": refund["id"]})

        paid_from_refunds: dict[str, dict[str, int]] = {}
        for refund in refunds:
            order_id = refund["order_id"]
            bucket = paid_from_refunds.setdefault(order_id, {})
            for channel in refund.get("channels", []):
                code = channel.get("channel")
                bucket[code] = max(bucket.get(code, 0), int(channel.get("paid_amount", 0) or 0))

        compositions = {row["order_id"]: row for row in self.ledger.list_payments()}
        order_ids = set(compositions) | set(paid_from_refunds)
        for order_id in order_ids:
            composition = compositions.get(order_id)
            if composition:
                paid: dict[str, int] = {}
                for payment in json.loads(composition["payments"]):
                    paid[payment["channel"]] = paid.get(payment["channel"], 0) + int(payment["amount"])
            else:
                # 未补录渠道构成时，回退到拆分快照中的订单实付上限。
                paid = paid_from_refunds.get(order_id, {})
            refunded = self.ledger.issued_channel_totals(order_id)
            for channel, amount in refunded.items():
                if amount > paid.get(channel, 0):
                    violations.append({
                        "code": "CHANNEL_CAP_EXCEEDED", "order_id": order_id, "channel": channel,
                        "refunded_amount": amount, "paid_amount": paid.get(channel, 0),
                    })
        return {
            "ok": not violations,
            "balanced": not violations,
            "violations": violations,
            "issued_refund_count": sum(r.get("status") == "ISSUED" for r in refunds),
            "channel_entry_count": len(entries),
        }

    def channel_balances(self, order_id: str) -> dict[str, Any]:
        composition = self.ledger.get_payments(order_id)
        payments = json.loads(composition["payments"]) if composition else []
        paid: dict[str, int] = {}
        for payment in payments:
            paid[payment["channel"]] = paid.get(payment["channel"], 0) + int(payment["amount"])
        issued = self.ledger.issued_channel_totals(order_id)
        entries = self.ledger.channel_entries(order_id=order_id)
        pending: dict[str, int] = {}
        for refund in self.ledger.list_refunds(order_id):
            if refund.get("status") != "ISSUED":
                continue
            for channel in refund.get("channels", []):
                code = channel.get("channel")
                pending[code] = pending.get(code, 0) + int(channel.get("pending_amount", 0) or 0)
        channels = []
        for code in sorted(set(paid) | set(issued) | set(pending)):
            paid_amount = paid.get(code, 0)
            refunded_amount = issued.get(code, 0)
            channels.append({
                "channel": code,
                "channel_label": channel_label(code),
                "paid_amount": paid_amount,
                "refunded_amount": refunded_amount,
                "available_amount": max(0, paid_amount - refunded_amount),
                "pending_amount": pending.get(code, 0),
                "within_cap": refunded_amount <= paid_amount,
            })
        return {"order_id": order_id, "channels": channels, "entries": entries}

    @staticmethod
    def _payment_json(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["payments"] = json.loads(row["payments"])
        result["revision"] = int(row["revision"])
        return result

    def resolve_composition(self, order_id: str, data: dict[str, Any], unit: str = "cents") -> tuple[list[dict[str, Any]], int | None, str | None]:
        inline = data.get("payments")
        if inline is None:
            inline = data.get("payment_channels") or data.get("channels") or data.get("tenders")
        if inline is not None:
            normalized = normalize_payments(inline, yuan=unit == "yuan")
            return normalized, None, stable_digest(normalized)

        row = self.ledger.get_payments(order_id)
        if row:
            return json.loads(row["payments"]), int(row["revision"]), row["digest"]

        paid_hint = data.get("payment")
        if paid_hint is None:
            paid_hint = data.get("paid_amount") or data.get("order_payment")
        if paid_hint is not None:
            fallback = normalize_payments({"default": paid_hint})
            return fallback, None, stable_digest(fallback)
        return [], None, None

    # ---------- 试算与退款单 ----------

    def preview(self, data: dict[str, Any]) -> dict[str, Any]:
        order_id = str(data.get("order_id") or data.get("id") or "")
        if not order_id:
            raise DomainIssue("ORDER_ID_REQUIRED")
        data = dict(data)
        if data.get("unit") == "yuan":
            for key in ("payments", "payment_channels", "channels", "tenders"):
                if key in data:
                    data[key] = self._convert_payment_units(data[key], "yuan")
        payments, revision, digest = self.resolve_composition(order_id, data, data.get("unit", "cents"))
        prior = data.get("prior")
        if prior is None and data.get("include_issued", True):
            prior = self.ledger.issued_channel_totals(order_id)
        result = self._calculate(data, order_id, payments, prior)
        result["payment_revision"] = revision
        result["payment_digest"] = digest
        return result

    def _calculate(self, data: dict[str, Any], order_id: str, payments: list[dict[str, Any]], prior: Any) -> dict[str, Any]:
        amount = data.get("refund_amount")
        if amount is None:
            amount = data.get("due_amount", data.get("amount"))
        if amount is None:
            amount = data.get("refund")
        if amount is None:
            raise DomainIssue("REFUND_AMOUNT_REQUIRED")
        try:
            return split_refund(
                order_id=order_id,
                refund_amount=amount,
                payments=payments,
                prior=prior,
                lines=data.get("lines") or data.get("line_results"),
                line_refunds=data.get("line_refunds"),
                explicit_shares=data.get("explicit_shares") or data.get("channel_amounts") or data.get("shares"),
                freight_refund=data.get("freight_refund", data.get("freight", 0)),
                unit=data.get("unit", "cents"),
            )
        except ValueError as exc:
            raise DomainIssue(str(exc) or "BAD_REQUEST") from exc

    def create_split(self, data: dict[str, Any]) -> dict[str, Any]:
        order_id = str(data.get("order_id") or data.get("id") or "")
        key = data.get("refund_key") or data.get("idempotency_key") or data.get("key")

        # 已签发退款的重跑必须在参数不完整时也能返回原单。
        if key:
            existing = self.ledger.get_refund_by_key(str(key))
            if existing and existing.get("status") == "ISSUED":
                existing["replayed"] = True
                existing["duplicate"] = False
                return existing

        if not order_id:
            raise DomainIssue("ORDER_ID_REQUIRED")

        data = dict(data)
        if data.get("unit") == "yuan":
            for key in ("payments", "payment_channels", "channels", "tenders"):
                if key in data:
                    data[key] = self._convert_payment_units(data[key], "yuan")

        if data.get("save_payments") and data.get("payments") is not None:
            self.save_payments(order_id, data["payments"])

        payments, revision, digest = self.resolve_composition(order_id, data, data.get("unit", "cents"))
        prior = data.get("prior")
        if prior is None and not data.get("ignore_issued_totals"):
            prior = self.ledger.issued_channel_totals(order_id)

        result = self._calculate(data, order_id, payments, prior)
        if not key:
            identity = {
                "order_id": order_id,
                "refund_amount": result["refund_amount"],
                "kind": data.get("kind", "REFUND"),
                "parent_id": data.get("parent_id"),
                "lines": sorted((item.get("line_id"), item.get("refund_amount"))
                                for item in result.get("line_splits", [])),
            }
            key = "auto:" + order_id + ":" + stable_digest(identity)[:16]
        refund_key = str(key)
        record = dict(result)
        record.update({
            "id": data.get("refund_id") or data.get("id") or uuid.uuid4().hex[:12],
            "refund_key": refund_key,
            "order_id": order_id,
            "run_id": data.get("run_id"),
            "parent_id": data.get("parent_id"),
            "kind": data.get("kind", "REFUND"),
            "seq": int(data.get("seq", 1)),
            "payment_revision": revision,
            "payment_digest": digest,
            "comment": (data.get("comment") or "").strip(),
            "replayed": False,
        })
        saved = self.ledger.upsert_draft(record)
        if saved and saved.get("status") == "ISSUED":
            saved["replayed"] = True
            saved["duplicate"] = False
        return saved

    def get_refund(self, refund_id: str) -> dict[str, Any]:
        row = self.ledger.get_refund(refund_id)
        if row is None:
            raise DomainIssue("REFUND_NOT_FOUND", 404)
        return row

    def list_refunds(self, order_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        refunds = self.ledger.list_refunds(order_id=order_id, run_id=run_id)
        entries = self.ledger.channel_entries(order_id=order_id)
        total_due = sum(int(item.get("due_amount", 0)) for item in refunds if item.get("status") == "ISSUED")
        total_refunded = sum(int(item.get("allocated_amount", 0)) for item in refunds if item.get("status") == "ISSUED")
        total_pending = sum(int(item.get("pending_amount", 0)) for item in refunds if item.get("status") == "ISSUED")
        return {
            "items": refunds,
            "refunds": refunds,
            "channel_entries": entries,
            "total": len(refunds),
            "summary": {
                "count": len(refunds),
                "issued_count": sum(item.get("status") == "ISSUED" for item in refunds),
                "draft_count": sum(item.get("status") == "DRAFT" for item in refunds),
                "stale_count": sum(item.get("status") == "STALE" for item in refunds),
                "issued_due_amount": total_due,
                "issued_refunded_amount": total_refunded,
                "issued_pending_amount": total_pending,
            },
        }

    def issue(self, refund_id: str, comment: str | None = None) -> dict[str, Any]:
        current = self.ledger.get_refund(refund_id)
        if current is None:
            raise DomainIssue("REFUND_NOT_FOUND", 404)
        if current.get("status") == "ISSUED":
            current["replayed"] = True
            current["duplicate"] = False
            return current
        if current.get("status") == "STALE":
            raise DomainIssue("SPLIT_STALE", 409, "补数据后拆分已失效，请先重算")

        row, problem = self.ledger.issue_refund(refund_id, comment)
        if problem == "missing" or row is None:
            raise DomainIssue("REFUND_NOT_FOUND", 404)
        if problem == "stale":
            raise DomainIssue("SPLIT_STALE", 409)
        row["replayed"] = problem == "existing"
        row["duplicate"] = False
        row["channel_entries"] = self.ledger.channel_entries(refund_id=refund_id)
        return row

    def rerun(self, refund_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.get_refund(refund_id)
        if current.get("status") == "ISSUED":
            # 已签发退款完全不可变：不重新应用新付款数据，也不写渠道流水。
            current["replayed"] = True
            current["duplicate"] = False
            current["channel_entries"] = self.ledger.channel_entries(refund_id=refund_id)
            return current

        payload = dict(data or {})
        payload.setdefault("order_id", current["order_id"])
        payload.setdefault("refund_key", current["refund_key"])
        payload.setdefault("refund_amount", current["refund_amount"])
        payload.setdefault("lines", current.get("line_splits"))
        payload.setdefault("run_id", current.get("run_id"))
        payload.setdefault("parent_id", current.get("parent_id"))
        payload.setdefault("kind", current.get("kind", "REFUND"))
        payload.setdefault("seq", current.get("seq", 1))
        return self.create_split(payload)

    def create_supplement(self, parent_id: str, data: dict[str, Any]) -> dict[str, Any]:
        parent = self.get_refund(parent_id)
        if parent.get("status") != "ISSUED" and not data.get("allow_unissued"):
            raise DomainIssue("PARENT_REFUND_NOT_ISSUED", 409, "只有已签发退款才能另出差额单")

        explicit_delta = None
        for key in ("difference_amount", "additional_amount", "delta_amount", "delta", "amount"):
            if data.get(key) is not None:
                explicit_delta = data[key]
                break
        if explicit_delta is None and data.get("refund_amount") is not None and data.get("amount_mode") != "total":
            explicit_delta = data["refund_amount"]

        auto_pending = False
        if explicit_delta is not None:
            delta = as_cents(explicit_delta, yuan=data.get("unit") == "yuan")
        else:
            target = None
            for key in ("total_refund_amount", "new_refund_amount", "correct_refund_amount", "target_amount"):
                if data.get(key) is not None:
                    target = data[key]
                    break
            if target is None and data.get("refund_amount") is not None and data.get("amount_mode") == "total":
                target = data["refund_amount"]
            if target is None and (data.get("payments") or data.get("lines")):
                latest = self.preview({**data, "order_id": parent["order_id"]})
                target = latest["refund_amount"]
            if target is not None:
                delta = as_cents(target, yuan=data.get("unit") == "yuan") - int(parent["refund_amount"])
            elif data.get("settle_pending", True):
                # 原单因渠道额度不足留下的待处理金额，后续可单独出差额单补退。
                delta = int(parent.get("pending_amount", 0))
                auto_pending = True
            else:
                raise DomainIssue("DIFFERENCE_AMOUNT_REQUIRED")

        if delta <= 0:
            raise DomainIssue("NO_POSITIVE_DIFFERENCE", 409, "没有需要补退的正差额")

        siblings = [item for item in self.ledger.list_refunds(parent["order_id"]) if item.get("parent_id") == parent_id]
        seq = int(data.get("seq") or len(siblings) + 1)
        key = data.get("refund_key") or data.get("idempotency_key") or f'{parent["refund_key"]}:difference:{seq}'
        child_data = dict(data)
        child_data.update({
            "order_id": parent["order_id"],
            "refund_amount": delta,
            "refund_key": key,
            "parent_id": parent_id,
            "kind": "SUPPLEMENT",
            "seq": seq,
            "run_id": parent.get("run_id"),
        })
        if auto_pending and not child_data.get("explicit_shares"):
            pending_shares = {
                channel.get("channel"): int(channel.get("pending_amount", 0))
                for channel in parent.get("channels", [])
                if int(channel.get("pending_amount", 0)) > 0
            }
            if sum(pending_shares.values()) == delta:
                child_data["explicit_shares"] = pending_shares
        child_data.setdefault("comment", f"补退差额单 #{seq}")
        child = self.create_split(child_data)
        child["difference_amount"] = delta
        child["difference_order_id"] = child["id"]
        child["parent_refund_id"] = parent_id
        if data.get("issue") or data.get("auto_issue"):
            child = self.issue(child["id"], data.get("comment"))
            child["difference_amount"] = delta
            child["difference_order_id"] = child["id"]
            child["parent_refund_id"] = parent_id
        return child

    # ---------- 与批次签发集成 ----------

    def provision_run_refunds(self, run: dict[str, Any], *, issue: bool = False) -> list[dict[str, Any]]:
        results = run.get("order_results")
        if not results:
            results = [row for row in run.get("rows", []) if row.get("label") and row.get("refund") is not None]
        records: list[dict[str, Any]] = []
        run_id = run.get("run_id") or run.get("id")
        for index, item in enumerate(results):
            order_id = str(item.get("label") or item.get("order_id") or "")
            if not order_id:
                continue
            key = f"run:{run_id}:order:{order_id}" if run_id else f"pipeline:{run.get('run_key')}:order:{order_id}"
            data = {
                "order_id": order_id,
                "refund_amount": item.get("refund_amount", item.get("refund", item.get("metric", 0))),
                "lines": item.get("lines"),
                "freight_refund": item.get("freight_refund", 0),
                "payment": item.get("payment"),
                "run_id": run_id,
                "refund_key": key,
                "comment": f"批次 {run_id or ''} 自动拆分".strip(),
            }
            record = self.create_split(data)
            if issue and record.get("status") != "ISSUED":
                record = self.issue(record["id"])
            records.append(record)
        return records

    def attach_previews(self, payload: dict[str, Any]) -> dict[str, Any]:
        results = payload.get("order_results") or []
        previews = []
        for item in results:
            order_id = str(item.get("label") or item.get("order_id") or "")
            if not order_id:
                continue
            data = {
                "order_id": order_id,
                "refund_amount": item.get("refund", item.get("metric", 0)),
                "lines": item.get("lines"),
                "freight_refund": item.get("freight_refund", 0),
                "payment": item.get("payment"),
            }
            try:
                split = self.preview(data)
            except DomainIssue:
                continue
            item["channel_split"] = split
            item["payment_splits"] = split["channels"]
            previews.append({
                "order_id": order_id,
                "refund_amount": split["refund_amount"],
                "allocated_amount": split["allocated_amount"],
                "pending_amount": split["pending_amount"],
                "status": split["status"],
                "channels": split["channels"],
            })
        payload["refund_splits"] = previews
        for row in payload.get("rows", []):
            match = next((item for item in previews if item["order_id"] == row.get("label")), None)
            if match:
                row["channel_split"] = match
        return payload

    # 常见动词/命名兼容。
    add_payment_data = save_payments
    supplement_payment_data = save_payments
    create_refund = create_split
    calculate_split = create_split
    issue_refund = issue

    def recalculate(self, refund_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.rerun(refund_id, data)


RefundSplitter = RefundService
RefundManager = RefundService
