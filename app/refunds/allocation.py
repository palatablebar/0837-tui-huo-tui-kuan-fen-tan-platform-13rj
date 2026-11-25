"""按原支付渠道拆分退款的纯领域逻辑。

金额在本模块内部统一使用整数分。HTTP 层可以通过 ``unit="yuan"`` 或
``*_yuan`` 字段接收元，再转换成分。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from app.domain.calculate import largest_remainder
from app.intake.normalize import canonical


CHANNEL_LABELS = {
    "gift_card": "礼品卡",
    "balance": "余额",
    "bank_card": "银行卡",
    "cash": "现金",
    "other": "其他渠道",
    "default": "默认渠道",
    "unallocated": "待补渠道",
}

_CHANNEL_ALIASES = {
    "gift": "gift_card",
    "giftcard": "gift_card",
    "gift_card": "gift_card",
    "gift-cards": "gift_card",
    "gift_cards": "gift_card",
    "礼品卡": "gift_card",
    "礼品卡支付": "gift_card",
    "balance": "balance",
    "account": "balance",
    "account_balance": "balance",
    "user_balance": "balance",
    "stored_balance": "balance",
    "store_credit": "balance",
    "余额": "balance",
    "账户余额": "balance",
    "bank": "bank_card",
    "bankcard": "bank_card",
    "bank_card": "bank_card",
    "bankcard_payment": "bank_card",
    "card": "bank_card",
    "credit_card": "bank_card",
    "debit_card": "bank_card",
    "银行卡": "bank_card",
    "储蓄卡": "bank_card",
    "信用卡": "bank_card",
    "cash": "cash",
    "现金": "cash",
    "other": "other",
    "other_channel": "other",
    "其他": "other",
    "其他渠道": "other",
    "default": "default",
    "default_channel": "default",
    "默认渠道": "default",
    "unallocated": "unallocated",
    "pending": "unallocated",
    "unknown": "unallocated",
    "待补渠道": "unallocated",
}

_PAID_KEYS = (
    "paid_amount",
    "payment_amount",
    "original_amount",
    "authorized_amount",
    "amount_paid",
    "paid",
    "amount_cents",
    "amount",
    "value",
    "total",
)
_CHANNEL_KEYS = ("channel", "channel_code", "method", "payment_method", "pay_channel", "type", "code", "name")
_LINE_KEYS = ("line_id", "line", "line_no", "line_number", "order_line_id")
_ISSUED_STATUSES = {"ISSUED", "PAID", "REFUNDED", "SUCCESS", "SUCCEEDED", "SETTLED", "APPROVED", "签发", "已签发"}


def channel_code(channel: Any) -> str:
    if channel is None:
        return "unallocated"
    text = str(channel).strip()
    if not text:
        return "unallocated"
    compact = text.lower().replace("-", "_").replace(" ", "_")
    if compact in _CHANNEL_ALIASES:
        return _CHANNEL_ALIASES[compact]
    if text in _CHANNEL_ALIASES:
        return _CHANNEL_ALIASES[text]
    # 保留未见过的渠道，但做一个稳定、适合作为 key 的规范化。
    return _CHANNEL_ALIASES.get(compact, compact)


def channel_label(code: str) -> str:
    return CHANNEL_LABELS.get(code, code)


def as_cents(value: Any, *, yuan: bool = False) -> int:
    """把金额转成整数分。

    纯 Python 调用通常已经传分；字符串小数（例如 ``"12.34"``）按元处理，
    避免调用方必须先了解内部单位。显式 ``yuan=True`` 时所有数值均按元。
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("BAD_AMOUNT")
    if isinstance(value, int):
        amount = Decimal(value) if not yuan else Decimal(value) * 100
    elif isinstance(value, float):
        amount = Decimal(str(value))
        if yuan:
            amount *= 100
    elif isinstance(value, Decimal):
        amount = value
        if yuan:
            amount *= 100
    else:
        text = canonical(value).replace(",", "").replace("￥", "").replace("¥", "").replace("元", "")
        if not text or text.lower() in {"-", "na", "null", "未提供"}:
            return 0
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("BAD_MONEY") from exc
        if yuan or "." in text:
            amount *= 100
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _first(mapping: dict, keys: Iterable[str], default=None):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _amount(mapping: dict, *keys: str, default: int = 0, yuan: bool = False) -> int:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return as_cents(mapping[key], yuan=yuan and not key.endswith("_cents"))
    return default


def normalize_payments(payments: Any, *, yuan: bool = False) -> list[dict]:
    """把 dict、二元组或常见支付记录格式统一为支付构成列表。"""
    if payments is None:
        return []
    if isinstance(payments, dict):
        items = []
        for key, value in payments.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("channel", key)
                items.append(item)
            else:
                items.append({"channel": key, "amount": value})
    elif isinstance(payments, (str, bytes)):
        raise ValueError("BAD_PAYMENTS")
    else:
        items = list(payments)

    normalized: list[dict] = []
    for index, raw in enumerate(items):
        if isinstance(raw, tuple) and len(raw) == 2:
            raw = {"channel": raw[0], "amount": raw[1]}
        if not isinstance(raw, dict):
            # 允许 ("gift_card", 3000) 之外的 [channel, amount] 列表。
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                raw = {"channel": raw[0], "amount": raw[1]}
            else:
                raise ValueError("BAD_PAYMENTS")

        channel_raw = _first(raw, _CHANNEL_KEYS, "other")
        code = channel_code(channel_raw)
        amount = _amount(raw, *_PAID_KEYS, default=0, yuan=yuan)
        if amount < 0:
            raise ValueError("BAD_AMOUNT")

        line_id = _first(raw, _LINE_KEYS)
        line_id = str(line_id) if line_id not in (None, "") else None

        line_amounts: dict[str, int] = {}
        raw_lines = _first(raw, ("line_amounts", "line_allocations", "allocations", "line_breakdown"))
        if isinstance(raw_lines, dict):
            line_amounts = {str(k): as_cents(v, yuan=yuan and not str(k).endswith("_cents")) for k, v in raw_lines.items()}
        elif isinstance(raw_lines, list):
            for alloc in raw_lines:
                if not isinstance(alloc, dict):
                    continue
                lid = _first(alloc, _LINE_KEYS)
                if lid is None:
                    continue
                line_amounts[str(lid)] = _amount(
                    alloc, "amount", "paid_amount", "allocated_amount", "cents", yuan=yuan
                )

        eligible_line_ids: list[str] = []
        raw_ids = _first(raw, ("line_ids", "applicable_lines", "eligible_lines", "lines"))
        if isinstance(raw_ids, (list, tuple)):
            for lid in raw_ids:
                if isinstance(lid, dict):
                    lid = _first(lid, _LINE_KEYS)
                elif not isinstance(lid, (str, int)):
                    lid = None
                if lid not in (None, ""):
                    eligible_line_ids.append(str(lid))
        elif raw_ids not in (None, ""):
            eligible_line_ids.append(str(raw_ids))

        # lines 也可能直接是 {line_id: amount}
        if isinstance(raw.get("lines"), dict) and not line_amounts:
            line_amounts = {str(k): as_cents(v, yuan=yuan) for k, v in raw["lines"].items()}

        prior_hint = _amount(raw, "refunded_amount", "prior_refunded", "already_refunded", "used_amount",
                             default=0, yuan=yuan)
        payment_id = _first(raw, ("payment_id", "id", "tender_id", "transaction_id"))
        normalized.append({
            "payment_id": str(payment_id) if payment_id not in (None, "") else f"P{index + 1}",
            "channel": code,
            "channel_label": CHANNEL_LABELS.get(code, str(channel_raw)),
            "amount": amount,
            "amount_cents": amount,
            "line_id": line_id,
            "line_amounts": line_amounts,
            "eligible_line_ids": sorted(set(eligible_line_ids)),
            "order_level": line_id is None and not line_amounts and not eligible_line_ids,
            "prior_hint": prior_hint,
        })

        if line_id and not line_amounts:
            normalized[-1]["line_amounts"][line_id] = amount
            normalized[-1]["order_level"] = False
    return normalized


def apply_line_allocations(payments: list[dict], allocations: Any) -> list[dict]:
    """把外部行级支付归属合并进支付构成。

    支持 ``[{"line_id":"L1","channel":"gift_card","amount":100}]``、
    ``{"L1": {"gift_card": 100}}`` 以及 ``{"gift_card": {"L1": 100}}``。
    """
    if not allocations:
        return payments
    result = [dict(payment, line_amounts=dict(payment.get("line_amounts", {}))) for payment in payments]

    def add(line_id: Any, channel_raw: Any, amount_value: Any):
        code = channel_code(channel_raw)
        amount = as_cents(amount_value)
        match = next((payment for payment in result if payment["channel"] == code), None)
        if match is None:
            match = {
                "payment_id": f"LINE-{code}", "channel": code,
                "channel_label": channel_label(code), "amount": 0, "amount_cents": 0,
                "line_id": None, "line_amounts": {}, "eligible_line_ids": [],
                "order_level": False, "prior_hint": 0,
            }
            result.append(match)
        match["line_amounts"][str(line_id)] = match["line_amounts"].get(str(line_id), 0) + amount
        match["order_level"] = False

    if isinstance(allocations, dict):
        for key, value in allocations.items():
            if isinstance(value, dict) and any(channel_code(k) in {p["channel"] for p in result} for k in value):
                # {line_id: {channel: amount}}
                for channel, amount in value.items():
                    add(key, channel, amount)
            elif isinstance(value, dict):
                # {channel: {line_id: amount}}
                for line_id, amount in value.items():
                    add(line_id, key, amount)
            else:
                add(key, "unallocated", value)
    elif isinstance(allocations, list):
        for item in allocations:
            if not isinstance(item, dict):
                continue
            line_id = _first(item, _LINE_KEYS)
            channel = _first(item, _CHANNEL_KEYS, "unallocated")
            amount = _amount(item, "amount", "paid_amount", "allocated_amount", "cents")
            if line_id is not None:
                add(line_id, channel, amount)
    return result


def normalize_prior(prior: Any, payments: list[dict] | None = None) -> dict[str, int]:
    """汇总该订单各渠道已经签发/退出的退款。"""
    consumed: dict[str, int] = {}
    if payments:
        for payment in payments:
            if payment.get("prior_hint"):
                consumed[payment["channel"]] = consumed.get(payment["channel"], 0) + payment["prior_hint"]

    if prior is None:
        return consumed
    if isinstance(prior, dict):
        for key, value in prior.items():
            # 支持 {"gift_card": {"amount": 3000}} 或嵌套 consumed/refunded。
            if isinstance(value, dict):
                value = _first(value, ("amount", "refunded_amount", "total", "cents"), 0)
            consumed[channel_code(key)] = consumed.get(channel_code(key), 0) + as_cents(value)
        return consumed

    for raw in prior:
        if not isinstance(raw, dict):
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                code, value = raw[0], raw[1]
            else:
                continue
        else:
            status = str(_first(raw, ("status", "state"), "ISSUED")).upper()
            if status and status not in _ISSUED_STATUSES and not raw.get("counts_as_issued"):
                continue
            code = _first(raw, _CHANNEL_KEYS, "unallocated")
            value = _amount(raw, "amount", "refund_amount", "refunded_amount", "cents", default=0)
        code = channel_code(code)
        consumed[code] = consumed.get(code, 0) + max(0, as_cents(value))
    return consumed


def normalize_line_refunds(lines: Any, line_refunds: Any = None) -> dict[str, dict]:
    result: dict[str, dict] = {}

    def add(line_id: Any, amount: Any, **extra):
        lid = str(line_id)
        item = result.setdefault(lid, {"line_id": lid, "refund": 0, "base": None, "qty": None, "return_qty": None})
        item["refund"] += max(0, as_cents(amount))
        if extra.get("base") is not None:
            item["base"] = as_cents(extra["base"])
        if extra.get("qty") is not None:
            item["qty"] = extra["qty"]
        if extra.get("return_qty") is not None:
            item["return_qty"] = extra["return_qty"]

    if isinstance(line_refunds, dict):
        for lid, value in line_refunds.items():
            if isinstance(value, dict):
                add(lid, _amount(value, "refund", "refund_amount", "amount"), **value)
            else:
                add(lid, value)
    elif isinstance(line_refunds, list):
        for index, value in enumerate(line_refunds):
            if isinstance(value, dict):
                lid = _first(value, _LINE_KEYS, f"L{index + 1}")
                add(lid, _amount(value, "refund", "refund_amount", "amount", "value"))
            else:
                add(f"L{index + 1}", value)

    if isinstance(lines, dict):
        for lid, value in lines.items():
            if isinstance(value, dict):
                refund = _amount(value, "refund", "refund_amount", "line_refund", "amount", default=0)
                add(lid, refund, base=_first(value, ("net_line", "net_amount", "line_net", "base", "funding_base")),
                    qty=value.get("qty"), return_qty=value.get("return_qty"))
            else:
                add(lid, value)
    elif isinstance(lines, list):
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                add(f"L{index + 1}", line)
                continue
            lid = _first(line, _LINE_KEYS + ("id",), f"L{index + 1}")
            refund = _amount(line, "refund", "refund_amount", "line_refund", "amount", "value", default=0)
            base = _first(line, ("net_line", "net_amount", "line_net", "funding_base", "base"))
            add(lid, refund, base=base, qty=line.get("qty"), return_qty=line.get("return_qty"))
    return result


def _aggregate_paid(payments: list[dict]) -> dict[str, int]:
    paid: dict[str, int] = {}
    for payment in payments:
        paid[payment["channel"]] = paid.get(payment["channel"], 0) + payment["amount"]
    return paid


def _eligible_specific_weights(payments: list[dict], line_ids: set[str], line_bases: dict[str, dict]) -> dict[str, dict[str, int]]:
    """返回 line -> channel -> 原始支付权重（分）。"""
    weights: dict[str, dict[str, int]] = {lid: {} for lid in line_ids}

    def add(lid: str, code: str, amount: int):
        if lid not in weights:
            weights[lid] = {}
        weights[lid][code] = weights[lid].get(code, 0) + max(0, amount)

    for payment in payments:
        code = payment["channel"]
        explicit = payment["line_amounts"]
        if explicit:
            for lid, amount in explicit.items():
                add(lid, code, amount)
            continue
        if payment["line_id"]:
            add(payment["line_id"], code, payment["amount"])
            continue
        eligible = payment["eligible_line_ids"]
        if eligible:
            target_ids = [lid for lid in eligible if lid in line_ids] or eligible
            bases = []
            for lid in target_ids:
                item = line_bases.get(lid, {})
                base = item.get("base")
                if base is None and item.get("qty") and item.get("return_qty"):
                    try:
                        base = as_cents(item.get("refund", 0)) * int(item["qty"]) // max(1, int(item["return_qty"]))
                    except (TypeError, ValueError):
                        base = None
                bases.append(max(1, as_cents(base or item.get("refund", 0))))
            shares = largest_remainder(payment["amount"], bases) if target_ids else []
            for lid, share in zip(target_ids, shares):
                add(lid, code, share)
    return weights


def _order_weights(payments: list[dict]) -> dict[str, int]:
    weights: dict[str, int] = {}
    for payment in payments:
        if payment["order_level"]:
            weights[payment["channel"]] = weights.get(payment["channel"], 0) + payment["amount"]
    return weights


def ideal_channel_shares(
    refund_amount: int,
    payments: list[dict],
    lines: dict[str, dict] | None = None,
    freight_amount: int = 0,
) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    """计算不受累计上限约束的理想渠道份额。

    返回 ``(channel -> cents, line_results, issues)``。理想份额之和恒等于
    ``refund_amount``；缺少权重时对应金额进入 ``unallocated`` 渠道。
    """
    issues: list[dict[str, Any]] = []
    refund_amount = max(0, int(refund_amount))
    lines = lines or {}
    line_refund_total = sum(item["refund"] for item in lines.values())
    common_amount = max(0, refund_amount - line_refund_total)
    if freight_amount:
        # 调用方通常已把运费包含在 refund_amount 中；仅在总额未给定时使用。
        common_amount = max(common_amount, int(freight_amount)) if refund_amount == 0 else common_amount

    specific = _eligible_specific_weights(payments, set(lines), lines) if lines else {}
    order_weights = _order_weights(payments)
    all_weights = _aggregate_paid(payments)
    ideal: dict[str, int] = {}
    line_results: list[dict[str, Any]] = []

    def add_ideal(shares: dict[str, int]):
        for code, amount in shares.items():
            ideal[code] = ideal.get(code, 0) + amount

    if not lines or not any(specific.values()):
        weights = order_weights or all_weights
        if refund_amount and not weights:
            ideal["unallocated"] = refund_amount
            issues.append({"code": "PAYMENT_COMPOSITION_MISSING", "amount": refund_amount})
        else:
            codes = list(weights)
            amounts = largest_remainder(refund_amount, [weights[c] for c in codes])
            add_ideal(dict(zip(codes, amounts)))
        return ideal, line_results, issues

    for lid, item in sorted(lines.items()):
        amount = item["refund"]
        spec_weights = specific.get(lid, {})
        spec_total = sum(spec_weights.values())
        common_total_weight = sum(order_weights.values())
        base = item.get("base")
        effective_base = max(as_cents(base) if base is not None else 0, spec_total, amount)
        common_funding_weight = max(0, effective_base - spec_total)

        if spec_total <= 0:
            # 不能把该商品行的退款改走只支付了其他行的礼品卡；没有订单级
            # 渠道时进入 unallocated/pending，等待补数据或人工处理。
            weights = order_weights
            if amount and not weights:
                shares = {"unallocated": amount}
                issues.append({"code": "RETURNED_LINE_CHANNEL_MISSING", "line_id": lid, "amount": amount})
            else:
                codes = list(weights)
                shares = dict(zip(codes, largest_remainder(amount, [weights[c] for c in codes])))
            add_ideal(shares)
            line_results.append({"line_id": lid, "refund_amount": amount, "shares": shares,
                                "basis": "order" if weights else "missing"})
            continue

        if common_total_weight > 0 and common_funding_weight > 0:
            bucket_values = largest_remainder(amount, [spec_total, common_funding_weight])
            spec_bucket, common_bucket = bucket_values
            spec_codes = list(spec_weights)
            spec_shares = dict(zip(spec_codes, largest_remainder(spec_bucket, [spec_weights[c] for c in spec_codes])))
            common_codes = list(order_weights)
            common_shares = dict(zip(common_codes, largest_remainder(common_bucket, [order_weights[c] for c in common_codes])))
            shares = dict(spec_shares)
            for code, value in common_shares.items():
                shares[code] = shares.get(code, 0) + value
            basis = "line_and_order"
        else:
            spec_codes = list(spec_weights)
            shares = dict(zip(spec_codes, largest_remainder(amount, [spec_weights[c] for c in spec_codes])))
            if common_funding_weight > 0 and not order_weights:
                # 知道订单层面还有未明细付款，但拿不到渠道构成；不擅自改道。
                shares["unallocated"] = shares.get("unallocated", 0) + 0
            basis = "line"
        add_ideal(shares)
        line_results.append({"line_id": lid, "refund_amount": amount, "shares": shares, "basis": basis})

    if common_amount > 0:
        weights = order_weights or all_weights
        if not weights:
            ideal["unallocated"] = ideal.get("unallocated", 0) + common_amount
            issues.append({"code": "ORDER_CHANNEL_MISSING", "amount": common_amount})
        else:
            codes = list(weights)
            shares = dict(zip(codes, largest_remainder(common_amount, [weights[c] for c in codes])))
            add_ideal(shares)

    return ideal, line_results, issues


def split_refund(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """按原付款构成拆分一笔退款。

    支持以下调用：

    * ``split_refund(order_dict)``
    * ``split_refund(10000, [{"channel": "gift_card", "amount": 3000}, ...])``
    * ``split_refund([...payments...], 10000)``
    * 全部通过 ``refund_amount`` / ``payments`` 关键字传入。
    """
    order: dict[str, Any] = {}
    positional = list(args)

    if positional and isinstance(positional[0], dict) and not _looks_like_payment_list(positional[0]):
        order = positional.pop(0)

    while positional:
        value = positional.pop(0)
        if isinstance(value, (list, tuple)):
            kwargs.setdefault("payments", value)
        elif isinstance(value, dict):
            if _looks_like_payment_list(value):
                kwargs.setdefault("payments", value)
            else:
                order.update(value)
        else:
            kwargs.setdefault("refund_amount", value)

    refund_amount = kwargs.pop("refund_amount", None)
    if refund_amount is None:
        refund_amount = kwargs.pop("amount", None)
    if refund_amount is None:
        refund_amount = kwargs.pop("due_amount", None)
    payments = kwargs.pop("payments", None)
    if payments is None:
        payments = kwargs.pop("channels", None)
    prior = kwargs.pop("prior", None)
    if prior is None:
        prior = kwargs.pop("already_refunded", kwargs.pop("consumed", None))
    lines = kwargs.pop("lines", None)
    line_refunds = kwargs.pop("line_refunds", None)
    freight = kwargs.pop("freight_refund", kwargs.pop("freight", 0))
    line_allocations = kwargs.pop("line_allocations", kwargs.pop("payment_allocations", None))
    line_payments = kwargs.pop("line_payments", None)
    explicit_shares = kwargs.pop("explicit_shares", kwargs.pop("channel_amounts", kwargs.pop("shares", None)))

    if order:
        refund_amount = _first(order, ("refund_amount", "due_amount", "refund", "amount", "metric"), refund_amount)
        payments = _first(order, ("payments", "payment_channels", "channels", "tenders"), payments)
        prior = _first(order, ("prior", "already_refunded", "consumed", "issued_allocations"), prior)
        lines = _first(order, ("lines", "line_results", "items"), lines)
        line_refunds = order.get("line_refunds", line_refunds)
        line_allocations = _first(order, ("line_allocations", "payment_allocations", "line_payment_allocations"), line_allocations)
        line_payments = order.get("line_payments", line_payments)
        explicit_shares = _first(order, ("explicit_shares", "channel_amounts", "shares"), explicit_shares)
        freight = order.get("freight_refund", order.get("freight", freight))
        order_id = order.get("order_id") or order.get("id") or order.get("label")
        paid_total_hint = _first(order, ("payment", "paid_amount", "order_payment", "payment_total"))
    else:
        order_id = kwargs.pop("order_id", None)
        paid_total_hint = kwargs.pop("payment", kwargs.pop("paid_amount", None))

    unit = kwargs.pop("unit", "cents")
    yuan = unit == "yuan" or kwargs.pop("yuan", False)
    amount_provided = refund_amount is not None
    due = as_cents(refund_amount or 0, yuan=yuan)
    if due < 0:
        raise ValueError("BAD_REFUND_AMOUNT")

    normalized_payments = normalize_payments(payments, yuan=yuan)
    if not normalized_payments and paid_total_hint is not None:
        # 没有渠道构成时也不能让订单卡住：保留为 default，并给出补数据提示。
        normalized_payments = [{
            "payment_id": "DEFAULT",
            "channel": "default",
            "channel_label": CHANNEL_LABELS["default"],
            "amount": as_cents(paid_total_hint, yuan=yuan),
            "line_id": None,
            "line_amounts": {},
            "eligible_line_ids": [],
            "order_level": True,
            "prior_hint": 0,
        }]

    if line_payments:
        expanded_allocations = []
        source = line_payments.values() if isinstance(line_payments, dict) else line_payments
        for line in source:
            if not isinstance(line, dict):
                continue
            line_id = _first(line, _LINE_KEYS)
            payments_for_line = line.get("payments") or line.get("channels") or {}
            if isinstance(payments_for_line, dict):
                for channel, amount in payments_for_line.items():
                    expanded_allocations.append({"line_id": line_id, "channel": channel, "amount": amount})
            elif isinstance(payments_for_line, list):
                for tender in payments_for_line:
                    if isinstance(tender, dict):
                        tender = dict(tender)
                        tender.setdefault("line_id", line_id)
                        expanded_allocations.append(tender)
        line_allocations = list(line_allocations or []) + expanded_allocations

    normalized_payments = apply_line_allocations(normalized_payments, line_allocations)
    prior_totals = normalize_prior(prior, normalized_payments)
    line_map = normalize_line_refunds(lines, line_refunds)
    freight_cents = as_cents(freight or 0, yuan=yuan)
    if not amount_provided:
        due = sum(item["refund"] for item in line_map.values()) + freight_cents
    ideal, line_results, issues = ideal_channel_shares(due, normalized_payments, line_map, freight_cents)
    if explicit_shares:
        if isinstance(explicit_shares, dict):
            ideal = {channel_code(code): as_cents(value) for code, value in explicit_shares.items()}
        else:
            ideal = {}
            for item in explicit_shares:
                if isinstance(item, dict):
                    ideal[channel_code(_first(item, _CHANNEL_KEYS))] = _amount(
                        item, "requested_amount", "amount", "refund_amount", "cents"
                    )
        explicit_total = sum(ideal.values())
        if not amount_provided:
            due = explicit_total
        elif explicit_total != due:
            issues.append({"code": "EXPLICIT_SHARES_MISMATCH", "amount": due, "shares_total": explicit_total})

    paid = _aggregate_paid(normalized_payments)
    # 确保所有已出现渠道都有结果行，即使理想份额为 0。
    for code in paid:
        ideal.setdefault(code, 0)
    for code in prior_totals:
        ideal.setdefault(code, 0)

    channel_order: list[str] = []
    for payment in normalized_payments:
        if payment["channel"] not in channel_order:
            channel_order.append(payment["channel"])
    for code in ideal:
        if code not in channel_order and code != "unallocated":
            channel_order.append(code)
    if "unallocated" in ideal:
        channel_order.append("unallocated")

    channel_rows: list[dict[str, Any]] = []
    allocated_total = 0
    pending_total = 0
    cap_ok = True
    for code in channel_order:
        requested = ideal.get(code, 0)
        paid_amount = paid.get(code, 0)
        prior_amount = max(0, prior_totals.get(code, 0))
        available = max(0, paid_amount - prior_amount)
        amount = min(max(0, int(requested)), available) if code != "unallocated" else 0
        pending = max(0, int(requested) - amount)
        if code == "unallocated":
            pending = requested
        cumulative = prior_amount + amount
        within_cap = cumulative <= paid_amount if code != "unallocated" else False
        if not within_cap and code != "unallocated":
            cap_ok = False
            issues.append({"code": "CHANNEL_CAP_EXCEEDED", "channel": code, "paid_amount": paid_amount,
                           "cumulative_amount": cumulative})
        if prior_amount > paid_amount and paid_amount > 0:
            issues.append({"code": "PRIOR_REFUND_EXCEEDS_PAYMENT", "channel": code})
        allocated_total += amount
        pending_total += pending
        state = "WAITING" if pending else "READY"
        row = {
            "channel": code,
            "channel_code": code,
            "channel_label": channel_label(code),
            "paid_amount": paid_amount,
            "prior_amount": prior_amount,
            "available_amount": available,
            "requested_amount": int(requested),
            "amount": amount,
            "refund_amount": amount,
            "allocated_amount": amount,
            "pending_amount": pending,
            "waiting_amount": pending,
            "shortfall_amount": pending,
            "cumulative_amount": cumulative,
            "within_cap": within_cap,
            "status": state,
        }
        channel_rows.append(row)

    conservation_ok = allocated_total + pending_total == due
    if not conservation_ok:
        issues.append({"code": "SPLIT_CONSERVATION_FAILED"})
    status = "COMPLETE" if pending_total == 0 and conservation_ok and cap_ok else "PENDING"
    settlement_status = "COMPLETE" if pending_total == 0 and conservation_ok and cap_ok else "WAITING"
    result = {
        "order_id": str(order_id) if order_id is not None else None,
        "refund_amount": due,
        "due_amount": due,
        "total_amount": due,
        "allocated_amount": allocated_total,
        "refunded_amount": allocated_total,
        "pending_amount": pending_total,
        "waiting_amount": pending_total,
        "shortfall_amount": pending_total,
        "unallocated_amount": pending_total,
        "balanced": conservation_ok,
        "within_caps": cap_ok,
        "valid": conservation_ok and cap_ok,
        "status": status,
        "state": status,
        "settlement_status": settlement_status,
        "waiting": pending_total > 0,
        "channels": channel_rows,
        # 常用别名，方便不同调用方/测试读取。
        "splits": channel_rows,
        "allocations": [row for row in channel_rows if row["amount"] > 0],
        "amounts": {row["channel"]: row["amount"] for row in channel_rows},
        "amount_by_channel": {row["channel"]: row["amount"] for row in channel_rows},
        "requested_amounts": {row["channel"]: row["requested_amount"] for row in channel_rows},
        "pending_by_channel": {row["channel"]: row["pending_amount"] for row in channel_rows},
        "paid_by_channel": paid,
        "pending_channels": [row for row in channel_rows if row["pending_amount"] > 0],
        "line_splits": line_results,
        "issues": issues,
    }
    return result


def _looks_like_payment_list(value: dict) -> bool:
    if not isinstance(value, dict):
        return False
    if any(key in value for key in ("payments", "payment_channels", "channels", "refund_amount", "due_amount", "order_id")):
        return False
    # {"gift_card": 3000} 形式
    return all(not isinstance(v, (dict, list)) or isinstance(v, dict) for v in value.values())


# 常见命名别名。
allocate_refund = split_refund
allocate_refund_to_channels = split_refund
allocate_refund_payment = split_refund
split_refund_amount = split_refund
split_payment = split_refund
split_by_payment = split_refund
split_payment_by_channels = split_refund
split_refund_by_channels = split_refund
split_refund_by_payments = split_refund
refund_by_payment = split_refund
calculate_channel_splits = split_refund
compute_refund_split = split_refund
build_refund_split = split_refund
