# 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| `order_id` | text | 业务字段 |
| `line_id` | text | 主键 |
| `line_amount` | money | 业务字段 |
| `discount_total` | money | 业务字段 |
| `return_qty` | int | 业务字段 |
| `qty` | int_positive | 业务字段 |
| `unit_refund` | money | 业务字段 |
| `freight` | money | 业务字段 |
| `payment` | money | 业务字段 |

退款拆分补充字段：`channel`（`gift_card`/`balance`/`bank_card` 或其他原始渠道）、`amount`/`amount_cents`（该渠道原实付，整数分）、`line_id`（可选，表示该笔支付只适用指定商品行）、`requested_amount`（按构成计算出的理想份额）、`prior_amount`（该渠道已签发累计退款）、`pending_amount`（超过可退额度、留待处理的金额）、`refund_key`（退款幂等键）、`parent_id` 与 `kind=SUPPLEMENT`（差额单关联）。

空字符串、`-`、`NA`、`null` 和 `未提供` 按空值处理。金额字段在整理层转为整数分或整数最小单位；日期按 ISO 日期解析；带时间的字段按本地时间解析。标识字段保持为文本，前导零不会被去除。
