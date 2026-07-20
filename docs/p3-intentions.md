# P3 采购意向与需求侧信号

P3 将采购意向纳入项目事件流，用于观察未来需求，但不把意向预算提前归因给供应商或上市公司。

## 数据输入

支持两种合规输入：

- `intent_urls`：配置公开、无需验证码的采购意向详情页；
- `intent_seed_csv`：导入已取得的结构化意向数据，也可只填写详情页 URL 交由程序解析。

示例配置：

```json
{
  "categories": ["采购意向", "采购公告", "中标（成交）公告", "采购合同公告"],
  "intent_urls": [],
  "intent_seed_csv": "intent_seed.example.csv"
}
```

程序不会绕过验证码或其他访问控制。公开检索页无法自动访问时，应使用公开详情页 URL 或结构化种子文件。

## 主要字段

- 采购人、意向项目名称、采购品目；
- 需求概况、预计采购时间；
- 意向预算、备注、发布时间和来源地址。

同一详情页中的多条意向会拆成独立条目，并保留来源页关系。

## 数据模型

数据库版本为 P3（schema version 4），新增：

- `procurement_intent_item`：采购意向条目；
- `intent_tender_candidate`：意向—招标候选匹配；
- `intent_tender_link`：当前有效匹配；
- `intent_match_review_queue`：低置信度人工复核队列；
- `intent_match_gold_label`：人工标注和评估结果。

P0 事件层同步生成 `INTENT_PUBLISHED` 和 `INTENT_BUDGET`。意向只连接项目根，不生成采购轮次、标包或供应商关系；归因状态固定为 `DEMAND_SIDE_ONLY`。

## 意向—招标匹配

匹配只允许按时间从意向指向后续招标，综合采购人、项目名称、品类/需求文本和时间间隔计算候选分数。

自动确认要求：综合分数不低于 `0.90`、采购人规范名称完全一致、标题相似度不低于 `0.90`，且第一、第二候选分差不低于 `0.08`。其余候选进入人工复核；采购人明显冲突或招标早于意向时不建立关系。

## 输出文件

- `intent_items.csv`：采购意向明细；
- `intent_tender_links.csv`：已确认的意向—招标关系；
- `intent_match_review_queue.csv`：待人工复核候选；
- `demand_signals.csv`：按月份、采购人和品类汇总的需求侧信号，不含供应商维度。

可生成意向匹配标注模板：

```powershell
python procurement_crawler.py --config config.json --write-intent-gold-template intent_match_gold_labels.csv
```

## 当前边界

采购意向缺少统一项目编号且常发生改名、拆分或合并，因此匹配策略有意偏保守。P3 不把未转化意向计作订单，也不将意向预算与招标、中标、合同金额跨阶段相加。
