# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 退款渠道拆分

金额默认使用整数分；请求也可传 `"unit": "yuan"` 按元接收。

- `POST|PUT /api/orders/{order_id}/payments`：补录订单原付款构成，正文为 `{"payments":[{"channel":"gift_card","amount":5000,"line_id":"O1L1"}]}`。
- `GET /api/orders/{order_id}/payments`：读取当前付款构成版本。
- `POST /api/refunds/preview`：只试算，不保存退款单。
- `POST /api/refunds`（别名 `/api/refund-splits`）：创建或按 `refund_key` 重算未签发拆分。
- `GET /api/refunds?order_id=...`：查看每笔退款的渠道份额、待处理金额和流水汇总。
- `GET /api/refunds/{id}`：查看退款单及渠道流水。
- `POST /api/refunds/{id}/issue`：签发并幂等写入渠道流水。
- `POST /api/refunds/{id}/rerun`（别名 `/recalculate`）：已签发返回原金额；未签发按最新补数据重算。
- `POST /api/refunds/{id}/supplement`：创建关联原单的 `SUPPLEMENT` 差额单；可传 `difference_amount`、`total_refund_amount`，或用 `settle_pending=true` 处理原单待处理余额。
- `POST /api/runs/{id}/rerun`：重跑批次；已签发批次的退款记录保持不变且不重复出款。
