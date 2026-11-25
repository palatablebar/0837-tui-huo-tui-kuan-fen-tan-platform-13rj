from app.refunds.ledger import RefundLedger

# 命名兼容：部分设计/测试可能把持久化对象称为 RefundStore。
RefundStore = RefundLedger

__all__ = ["RefundLedger", "RefundStore"]
