# P0 数据模型

P0 的目标是修复数据正确性，不扩充数据源。公告是事件证据，不是订单。

## 写入路径

```text
Notice
  ├─ source_document
  │    └─ source_document_version（只追加）
  ├─ root_project
  │    └─ procurement_attempt
  │          └─ package
  │                └─ contract
  │                      └─ acceptance
  ├─ document_object_link（方法、分数、置信度、复核状态）
  └─ procurement_event（只追加）
         └─ amount_observation（金额类型）
```

## 原始文档与版本

`source_document` 表示一个来源URL对应的逻辑文档，保存首次发现时间和最后发现时间。

`source_document_version` 按内容哈希保存不可变版本：

- 相同URL、相同内容再次抓取，不产生新版本；
- 相同URL、内容哈希变化，增加版本号；
- 历史版本不覆盖、不删除；
- `available_time = max(publish_time, first_seen_at)`，用于点时回测。

当前爬虫只保留正文文本；`raw_html_path` 和附件路径已预留，后续阶段写入。

## 五层业务对象

- `root_project`：真实经济需求；
- `procurement_attempt`：首次、二次或重新采购等独立轮次；
- `package`：标包，金额、供应商和失败事件尽量落在这一层；
- `contract`：一个标包可对应多份合同；
- `acceptance`：一份合同可对应多次验收。

未披露标包号时使用 `UNSPECIFIED`，表示未知，而不是断言项目只有一个标包。

## 事件

P0 支持的主要事件：

- `TENDER_OPENED`
- `AWARD_PUBLISHED`
- `CONTRACT_SIGNED`
- `CORRECTION_PUBLISHED`
- `DEADLINE_EXTENDED`
- `PACKAGE_FAILED`
- `PROJECT_TERMINATED`

更正事件的 `state_after` 为空，因此不会把对象状态错误改成“已更正”。

`current_event` 视图选择每个逻辑事件的最新文档版本；`current_object_state` 只根据会改变状态的事件物化当前状态。

## 金额

金额存入 `amount_observation`，必须携带类型：

- `TENDER_BUDGET`
- `AWARD_AMOUNT`
- `CONTRACT_AMOUNT`
- 以及为后续预留的其他阶段类型。

不同类型不能跨阶段相加。多供应商而未披露金额分配时，只保存一条总金额，并标记：

```text
allocation_status = UNALLOCATED_MULTI_SUPPLIER
```

正式数据库不会把总金额均分给每个供应商。旧版供应商CSV仍保留初筛口径，后续阶段会迁移到实体和标包关系表。

## 关联置信度

每个文档版本到项目、轮次和标包的关联写入 `document_object_link`，解析结果变化时保留新旧关联版本：

- 有项目编号：`project_no_exact`，0.98，自动接受；
- 无项目编号：`title_buyer_normalized`，0.72，需要复核。

P0 先建立可审计接口，P1 再加入标注集和更完整的匹配评分模型。

## 幂等性

相同 `notices.csv` 重复迁移时，所有新增计数均应为0。运行摘要中的 `database_changes` 可直接检查这一点。

## 查询示例

```sql
-- 当前对象状态
SELECT current_state, COUNT(*)
FROM current_object_state
GROUP BY current_state;

-- 按阶段分开的金额
SELECT amount_type, SUM(amount)
FROM current_amount_observation
GROUP BY amount_type;

-- 需要人工复核的关联
SELECT *
FROM document_object_link
WHERE review_status = 'NEEDS_REVIEW';
```
