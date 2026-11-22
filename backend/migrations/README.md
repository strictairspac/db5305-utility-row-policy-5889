# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 存储切换引入的表与列

`src/models/migrations.py` 在启动时幂等补齐以下结构，不改动已有数据：

- 新表 `business_case_orders`（逐单入库，唯一键 `case_id + order_id`）、`business_intake_receipts`（每端每次受理一条回执，唯一键 `source + receipt_key`）、`business_settings`（当前接单模式）。
- `business_cases` 新增 `storage_version`（默认 `v1`）与 `updated_at`。
- `business_runs` 新增 `payload_snapshot`（受理时的完整档案）与 `storage_version`。
- `business_evidence` 新增 `storage_version`。

升级既有部署时，先运行一次 `python -m scripts.seed_demo`（内部调用 `ensure_schema`）或启动后端，再按 `BUSINESS.md` 的切换顺序先迁移、后切到 `v2`。
