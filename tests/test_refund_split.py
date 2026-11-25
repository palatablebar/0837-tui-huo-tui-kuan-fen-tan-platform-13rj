from app.refunds.allocation import split_refund


PAYMENTS = [
    {"channel": "gift_card", "amount": 3000},
    {"channel": "balance", "amount": 2000},
    {"channel": "bank_card", "amount": 5000},
]


def test_order_level_composition_splits_by_original_paid_amount():
    result = split_refund(10000, PAYMENTS)

    amounts = {row["channel"]: row["amount"] for row in result["channels"]}
    assert amounts == {"gift_card": 3000, "balance": 2000, "bank_card": 5000}
    assert sum(row["requested_amount"] for row in result["channels"]) == result["refund_amount"]
    assert result["pending_amount"] == 0
    assert result["balanced"] is True


def test_missing_line_details_still_processes_as_order_level_split():
    result = split_refund({
        "order_id": "O-NO-LINES",
        "refund_amount": 333,
        "payments": {"礼品卡": 100, "余额": 100, "银行卡": 133},
    })

    assert result["status"] == "COMPLETE"
    assert result["allocated_amount"] == 333
    assert {row["channel"] for row in result["channels"]} == {"gift_card", "balance", "bank_card"}


def test_line_specific_gift_card_is_used_for_returned_item():
    result = split_refund(
        order_id="O-LINE",
        refund_amount=5000,
        payments=[
            {"channel": "gift_card", "amount": 5000, "line_id": "L1"},
            {"channel": "bank_card", "amount": 5000},
        ],
        lines=[{"line_id": "L1", "refund_amount": 5000}],
    )

    by_channel = {row["channel"]: row for row in result["channels"]}
    # 没有提供整行原始净额时，明确由礼品卡支付的退货直接留在礼品卡渠道。
    assert by_channel["gift_card"]["amount"] == 5000
    assert by_channel["bank_card"]["amount"] == 0


def test_gift_card_for_other_line_is_not_used_for_returned_line():
    result = split_refund(
        order_id="O-OTHER-LINE",
        refund_amount=4000,
        payments=[{"channel": "gift_card", "amount": 4000, "line_id": "L2"}],
        lines=[{"line_id": "L1", "refund_amount": 4000}],
    )
    by_channel = {row["channel"]: row for row in result["channels"]}
    assert by_channel["gift_card"]["amount"] == 0
    assert result["pending_amount"] == 4000
    assert any(issue["code"] == "RETURNED_LINE_CHANNEL_MISSING" for issue in result["issues"])


def test_channel_cap_excess_waits_and_is_not_rerouted():
    result = split_refund(8000, PAYMENTS, prior={"gift_card": 3000})

    by_channel = {row["channel"]: row for row in result["channels"]}
    assert by_channel["gift_card"]["amount"] == 0
    assert by_channel["gift_card"]["pending_amount"] == 2400
    # 礼品卡溢出的 2400 不能改走余额或银行卡。
    assert by_channel["balance"]["amount"] == 1600
    assert by_channel["bank_card"]["amount"] == 4000
    assert result["allocated_amount"] == 5600
    assert result["pending_amount"] == 2400
    assert result["allocated_amount"] + result["pending_amount"] == result["refund_amount"]
    for row in result["channels"]:
        assert row["prior_amount"] + row["amount"] <= row["paid_amount"] or row["channel"] == "unallocated"


def test_single_payment_posts_accumulate_until_replaced(client):
    client.post("/api/orders/O-MIX/payments", json={"channel": "gift_card", "amount": 3000})
    client.post("/api/orders/O-MIX/payments", json={"channel": "bank_card", "amount": 7000})
    composition = client.get("/api/orders/O-MIX/payments").get_json()
    assert [(p["channel"], p["amount"]) for p in composition["payments"]] == [
        ("gift_card", 3000), ("bank_card", 7000)
    ]

    replaced = client.put("/api/orders/O-MIX/payments", json={"payments": [{"channel": "balance", "amount": 10000}]})
    assert replaced.status_code == 200
    composition = client.get("/api/orders/O-MIX/payments").get_json()
    assert [(p["channel"], p["amount"]) for p in composition["payments"]] == [("balance", 10000)]


def test_refund_api_supplements_recomputes_issues_and_creates_difference_order(client):
    payments_response = client.post("/api/orders/O1/payments", json={"payments": PAYMENTS})
    assert payments_response.status_code == 200

    created = client.post("/api/refunds", json={
        "order_id": "O1",
        "refund_key": "refund-O1-1",
        "refund_amount": 10000,
    })
    assert created.status_code == 200
    draft = created.get_json()
    first_id = draft["id"]
    assert draft["status"] == "DRAFT"

    # 补数据后，旧草稿被标记失效；用同一个业务键重算会更新金额。
    changed_payments = PAYMENTS + [{"channel": "cash", "amount": 1000}]
    supplement = client.post("/api/orders/O1/payments", json={"payments": changed_payments})
    assert supplement.status_code == 200
    assert supplement.get_json()["changed"] is True

    stale = client.get(f"/api/refunds/{first_id}").get_json()
    assert stale["status"] == "STALE"

    recalculated = client.post("/api/refunds", json={
        "order_id": "O1",
        "refund_key": "refund-O1-1",
        "refund_amount": 11000,
    }).get_json()
    assert recalculated["id"] == first_id
    assert recalculated["status"] == "DRAFT"
    assert recalculated["refund_amount"] == 11000

    issued = client.post(f"/api/refunds/{first_id}/issue", json={"comment": "签发"}).get_json()
    assert issued["status"] == "ISSUED"

    rerun = client.post(f"/api/refunds/{first_id}/rerun").get_json()
    assert rerun["id"] == first_id
    assert rerun["refund_amount"] == 11000
    assert rerun["replayed"] is True

    listed = client.get("/api/refunds?order_id=O1").get_json()
    assert listed["summary"]["issued_count"] == 1
    entries = client.get(f"/api/refunds/{first_id}").get_json()["channel_entries"]
    assert len(entries) == len({entry["channel"] for entry in entries})

    difference = client.post(f"/api/refunds/{first_id}/supplement", json={
        "difference_amount": 500,
        "issue": True,
    })
    assert difference.status_code == 200
    diff = difference.get_json()
    assert diff["kind"] == "SUPPLEMENT"
    assert diff["parent_id"] == first_id
    assert diff["refund_amount"] == 500
    assert diff["status"] == "ISSUED"
    assert diff["id"] != first_id

    rerun_again = client.post(f"/api/refunds/{first_id}/rerun").get_json()
    assert rerun_again["refund_amount"] == 11000
    assert client.get("/api/refunds?order_id=O1").get_json()["summary"]["issued_count"] == 2


def test_stateful_second_refund_does_not_exceed_channel_cap(client):
    client.post("/api/orders/O-CAP/payments", json={"payments": [
        {"channel": "gift_card", "amount": 5000, "line_id": "L1"},
        {"channel": "bank_card", "amount": 5000},
    ]})
    first = client.post("/api/refunds", json={
        "order_id": "O-CAP", "refund_key": "cap-1", "refund_amount": 5000,
        "lines": [{"line_id": "L1", "refund_amount": 5000}],
    }).get_json()
    client.post(f"/api/refunds/{first['id']}/issue", json={})

    second = client.post("/api/refunds", json={
        "order_id": "O-CAP", "refund_key": "cap-2", "refund_amount": 5000,
        "lines": [{"line_id": "L1", "refund_amount": 5000}],
    }).get_json()
    issued = client.post(f"/api/refunds/{second['id']}/issue", json={}).get_json()
    by_channel = {row["channel"]: row for row in issued["channels"]}
    assert by_channel["gift_card"]["requested_amount"] == 5000
    assert by_channel["gift_card"]["amount"] == 0
    assert by_channel["gift_card"]["pending_amount"] == 5000
    assert by_channel["bank_card"]["amount"] == 0
    assert issued["balanced"] is True


def test_pending_supplement_keeps_exact_pending_channel(client):
    client.post("/api/orders/O-PEND/payments", json={"payments": [
        {"channel": "gift_card", "amount": 5000, "line_id": "L1"},
        {"channel": "bank_card", "amount": 5000},
    ]})
    first = client.post("/api/refunds", json={
        "order_id": "O-PEND", "refund_key": "pend-1", "refund_amount": 5000,
        "lines": [{"line_id": "L1", "refund_amount": 5000}],
    }).get_json()
    client.post(f"/api/refunds/{first['id']}/issue", json={})
    second = client.post("/api/refunds", json={
        "order_id": "O-PEND", "refund_key": "pend-2", "refund_amount": 5000,
        "lines": [{"line_id": "L1", "refund_amount": 5000}],
    }).get_json()
    second = client.post(f"/api/refunds/{second['id']}/issue", json={}).get_json()
    assert second["pending_amount"] == 5000

    difference = client.post(f"/api/refunds/{second['id']}/supplement", json={"issue": True}).get_json()
    by_channel = {row["channel"]: row for row in difference["channels"]}
    assert difference["kind"] == "SUPPLEMENT"
    assert by_channel["gift_card"]["requested_amount"] == 5000
    assert by_channel["gift_card"]["amount"] == 0
    assert by_channel["gift_card"]["pending_amount"] == 5000

    # 补数据证明礼品卡还有额度后，再开的差额单可以退到礼品卡，但不改写已签发单。
    client.post("/api/orders/O-PEND/payments", json={"payments": [
        {"channel": "gift_card", "amount": 10000, "line_id": "L1"},
        {"channel": "bank_card", "amount": 5000},
    ]})
    settled = client.post(f"/api/refunds/{difference['id']}/supplement", json={"issue": True}).get_json()
    assert settled["refund_amount"] == 5000
    assert settled["amounts"]["gift_card"] == 5000
    assert client.get(f"/api/refunds/{second['id']}").get_json()["pending_amount"] == 5000


def test_batch_approval_creates_idempotent_channel_refunds(client):
    run = client.post("/api/runs", json={"profile": "clean"}).get_json()
    approved = client.post(f"/api/runs/{run['run_id']}/review", json={
        "decision": "APPROVED",
        "comment": "通过",
        "expected_revision": 0,
    })
    assert approved.status_code == 200
    records = approved.get_json()["refund_records"]
    assert records
    assert all(record["status"] == "ISSUED" for record in records)

    rerun_response = client.post(f"/api/runs/{run['run_id']}/rerun")
    assert rerun_response.status_code == 200
    rerun = rerun_response.get_json()
    again_records = rerun["refund_records"]
    assert {record["refund_key"] for record in again_records} == {record["refund_key"] for record in records}
    assert all(record.get("replayed") for record in again_records)
