from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RefundLedger:
    """退款单与渠道流水的持久化。

    一个 SQLite 文件中同时保存批次和退款数据；渠道流水表的主键保证同一退款
    单的同一渠道不会因为重跑而重复写入。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._memory_connection: sqlite3.Connection | None = None
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # 测试或嵌入式调用可能直接使用内存库；多连接会各自拥有独立 schema。
            self._memory_connection = sqlite3.connect(":memory:", timeout=8, check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        self.init()

    def connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS payment_compositions(
                  order_id TEXT PRIMARY KEY,
                  revision INTEGER NOT NULL,
                  digest TEXT NOT NULL,
                  payments TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS refund_orders(
                  id TEXT PRIMARY KEY,
                  refund_key TEXT UNIQUE NOT NULL,
                  order_id TEXT NOT NULL,
                  run_id TEXT,
                  parent_id TEXT,
                  kind TEXT NOT NULL DEFAULT 'REFUND',
                  seq INTEGER NOT NULL DEFAULT 1,
                  status TEXT NOT NULL,
                  version INTEGER NOT NULL DEFAULT 1,
                  payment_revision INTEGER,
                  due_amount INTEGER NOT NULL,
                  allocated_amount INTEGER NOT NULL DEFAULT 0,
                  pending_amount INTEGER NOT NULL DEFAULT 0,
                  digest TEXT,
                  payload TEXT NOT NULL,
                  comment TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  issued_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_refund_orders_order ON refund_orders(order_id);
                CREATE INDEX IF NOT EXISTS idx_refund_orders_run ON refund_orders(run_id);
                CREATE INDEX IF NOT EXISTS idx_refund_orders_parent ON refund_orders(parent_id);

                CREATE TABLE IF NOT EXISTS refund_channel_entries(
                  refund_id TEXT NOT NULL,
                  order_id TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  amount INTEGER NOT NULL,
                  requested_amount INTEGER NOT NULL DEFAULT 0,
                  pending_amount INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(refund_id, channel)
                );

                CREATE INDEX IF NOT EXISTS idx_channel_entries_order
                  ON refund_channel_entries(order_id, channel);
                """
            )

    # ---------- 付款构成 ----------

    def get_payments(self, order_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM payment_compositions WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def list_payments(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM payment_compositions ORDER BY order_id").fetchall()
        return [dict(row) for row in rows]

    def upsert_payments(self, order_id: str, payments_json: str, digest: str) -> tuple[dict[str, Any], bool]:
        existing = self.get_payments(order_id)
        timestamp = now_iso()
        if existing and existing["digest"] == digest:
            return existing, False

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM payment_compositions WHERE order_id=?", (order_id,)).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO payment_compositions
                       (order_id,revision,digest,payments,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (order_id, 1, digest, payments_json, timestamp, timestamp),
                )
                revision = 1
            else:
                revision = int(row["revision"]) + 1
                db.execute(
                    """UPDATE payment_compositions
                       SET revision=?, digest=?, payments=?, updated_at=?
                       WHERE order_id=?""",
                    (revision, digest, payments_json, timestamp, order_id),
                )
            # 只有未签发草稿会因补数据失效；已签发退款是不可变凭证。
            db.execute(
                "UPDATE refund_orders SET status='STALE', updated_at=? WHERE order_id=? AND status IN ('DRAFT','STALE')",
                (timestamp, order_id),
            )
        return self.get_payments(order_id), True

    # ---------- 退款单 ----------

    @staticmethod
    def _deserialize(row: sqlite3.Row | dict | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        if data.get("payload"):
            payload = json.loads(data["payload"])
            payload.update({k: v for k, v in data.items() if k != "payload"})
            payload["refund_id"] = data["id"]
            payload["split_id"] = data["id"]
            stale = data.get("status") == "STALE"
            payload["stale"] = stale
            payload["invalid"] = stale
            payload["issued"] = data.get("status") == "ISSUED"
            payload["editable"] = data.get("status") == "DRAFT"
            payload["valid"] = (not stale) and bool(payload.get("balanced", True))
            return payload
        data["refund_id"] = data["id"]
        data["split_id"] = data["id"]
        return data

    def get_refund(self, refund_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM refund_orders WHERE id=?", (refund_id,)).fetchone()
        return self._deserialize(row)

    def get_refund_by_key(self, refund_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM refund_orders WHERE refund_key=?", (refund_key,)).fetchone()
        return self._deserialize(row)

    def list_refunds(self, order_id: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM refund_orders WHERE 1=1"
        params: list[Any] = []
        if order_id:
            sql += " AND order_id=?"
            params.append(order_id)
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        sql += " ORDER BY created_at, id"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._deserialize(row) for row in rows]

    def upsert_draft(self, record: dict[str, Any]) -> dict[str, Any]:
        """按 refund_key 新建或重算未签发退款。已签发记录不得调用本方法覆盖。"""
        existing = self.get_refund_by_key(record["refund_key"])
        timestamp = now_iso()
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if existing and existing["status"] == "ISSUED":
            return existing

        if existing:
            refund_id = existing["id"]
            version = int(existing.get("version", 1)) + 1
            with self.connect() as db:
                db.execute(
                    """UPDATE refund_orders SET
                       order_id=?, run_id=?, parent_id=?, kind=?, seq=?, status='DRAFT', version=?,
                       payment_revision=?, due_amount=?, allocated_amount=?, pending_amount=?,
                       digest=?, payload=?, comment=?, updated_at=?, issued_at=NULL
                       WHERE id=?""",
                    (
                        record["order_id"], record.get("run_id"), record.get("parent_id"),
                        record.get("kind", "REFUND"), record.get("seq", 1), version,
                        record.get("payment_revision"), record["refund_amount"],
                        record["allocated_amount"], record["pending_amount"], record.get("digest"),
                        payload, record.get("comment", ""), timestamp, refund_id,
                    ),
                )
        else:
            refund_id = record.get("id") or uuid.uuid4().hex[:12]
            with self.connect() as db:
                db.execute(
                    """INSERT INTO refund_orders
                       (id,refund_key,order_id,run_id,parent_id,kind,seq,status,version,
                        payment_revision,due_amount,allocated_amount,pending_amount,digest,
                        payload,comment,created_at,updated_at,issued_at)
                       VALUES(?,?,?,?,?,?,?,'DRAFT',1,?,?,?,?,?,?,?,?,?,NULL)""",
                    (
                        refund_id, record["refund_key"], record["order_id"], record.get("run_id"),
                        record.get("parent_id"), record.get("kind", "REFUND"), record.get("seq", 1),
                        record.get("payment_revision"), record["refund_amount"],
                        record["allocated_amount"], record["pending_amount"], record.get("digest"),
                        payload, record.get("comment", ""), timestamp, timestamp,
                    ),
                )
        return self.get_refund(refund_id)

    def issue_refund(self, refund_id: str, comment: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
        timestamp = now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM refund_orders WHERE id=?", (refund_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["status"] == "ISSUED":
                return self._deserialize(row), "existing"
            if row["status"] != "DRAFT":
                return self._deserialize(row), "stale"

            payload = json.loads(row["payload"])
            totals_rows = db.execute(
                """SELECT channel, SUM(amount) AS amount FROM refund_channel_entries
                   WHERE order_id=? GROUP BY channel""",
                (row["order_id"],),
            ).fetchall()
            already_by_channel = {item["channel"]: int(item["amount"] or 0) for item in totals_rows}

            allocated_total = 0
            pending_total = 0
            for channel in payload.get("channels", []):
                code = channel["channel"]
                requested = int(channel.get("requested_amount", channel.get("amount", 0)) or 0)
                paid_amount = int(channel.get("paid_amount", 0) or 0)
                already = already_by_channel.get(code, 0)
                if code == "unallocated" or paid_amount <= 0:
                    amount = 0
                else:
                    amount = max(0, min(requested, paid_amount - already))
                pending = max(0, requested - amount)
                channel["amount"] = amount
                channel["refund_amount"] = amount
                channel["allocated_amount"] = amount
                channel["pending_amount"] = pending
                channel["waiting_amount"] = pending
                channel["shortfall_amount"] = pending
                channel["prior_amount"] = already
                channel["available_amount"] = max(0, paid_amount - already)
                channel["cumulative_amount"] = already + amount
                channel["within_cap"] = code == "unallocated" or already + amount <= paid_amount
                channel["status"] = "WAITING" if pending else "READY"
                allocated_total += amount
                pending_total += pending

            payload["allocated_amount"] = allocated_total
            payload["refunded_amount"] = allocated_total
            payload["pending_amount"] = pending_total
            payload["waiting_amount"] = pending_total
            payload["shortfall_amount"] = pending_total
            payload["unallocated_amount"] = pending_total
            payload["balanced"] = allocated_total + pending_total == int(row["due_amount"])
            payload["settlement_status"] = "COMPLETE" if pending_total == 0 and payload["balanced"] else "WAITING"
            payload["waiting"] = pending_total > 0
            payload["status"] = "COMPLETE" if pending_total == 0 and payload["balanced"] else "PENDING"
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

            db.execute(
                """UPDATE refund_orders SET status='ISSUED', comment=COALESCE(?, comment),
                   allocated_amount=?, pending_amount=?, payload=?, updated_at=?, issued_at=? WHERE id=?""",
                (comment, allocated_total, pending_total, payload_json, timestamp, timestamp, refund_id),
            )
            for channel in payload.get("channels", []):
                # 实际退出金额为 0 也保留渠道计算行，但渠道流水只写有钱款变化的行。
                if int(channel.get("amount", 0)) <= 0:
                    continue
                db.execute(
                    """INSERT OR IGNORE INTO refund_channel_entries
                       (refund_id,order_id,channel,amount,requested_amount,pending_amount,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        refund_id, row["order_id"], channel["channel"], int(channel["amount"]),
                        int(channel.get("requested_amount", channel["amount"])),
                        int(channel.get("pending_amount", 0)), timestamp,
                    ),
                )
            new_row = db.execute("SELECT * FROM refund_orders WHERE id=?", (refund_id,)).fetchone()
            return self._deserialize(new_row), "issued"

    def issued_channel_totals(self, order_id: str) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT e.channel AS channel, SUM(e.amount) AS amount
                   FROM refund_channel_entries e
                   JOIN refund_orders r ON r.id = e.refund_id
                   WHERE e.order_id=? AND r.status='ISSUED'
                   GROUP BY e.channel""",
                (order_id,),
            ).fetchall()
        return {row["channel"]: int(row["amount"] or 0) for row in rows}

    def channel_entries(self, refund_id: str | None = None, order_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM refund_channel_entries WHERE 1=1"
        params: list[Any] = []
        if refund_id:
            sql += " AND refund_id=?"
            params.append(refund_id)
        if order_id:
            sql += " AND order_id=?"
            params.append(order_id)
        sql += " ORDER BY order_id, channel, refund_id"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
