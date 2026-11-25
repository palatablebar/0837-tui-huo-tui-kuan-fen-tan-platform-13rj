from flask import jsonify


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    "ORDER_ID_REQUIRED": "缺少订单号",
    "PAYMENTS_REQUIRED": "缺少原付款构成",
    "PAYMENTS_NOT_FOUND": "尚未补录该订单的付款构成",
    "REFUND_AMOUNT_REQUIRED": "缺少订单应退金额",
    "REFUND_ID_REQUIRED": "缺少退款单号或幂等键",
    "REFUND_NOT_FOUND": "退款单不存在",
    "SPLIT_STALE": "补数据后拆分已失效，请重新计算",
    "STALE_PAYMENT_REVISION": "付款构成版本已更新，请刷新后重试",
    "PARENT_REFUND_NOT_ISSUED": "原退款尚未签发，不能另出差额单",
    "DIFFERENCE_AMOUNT_REQUIRED": "缺少补退差额或最新应退总额",
    "NO_POSITIVE_DIFFERENCE": "没有需要补退的正差额",
}


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None):
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(code)


def error_response(exc: DomainIssue):
    return jsonify(error=exc.detail or MESSAGES.get(exc.code, "请求无法处理"), code=exc.code), exc.status
