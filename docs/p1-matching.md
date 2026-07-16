# P1 实体归一与三阶段链路匹配

P1 在 P0 的不可变文档和事件库之上，增加招标—中标—合同的可审计匹配层。P0 表不删除、不覆盖，旧数据库运行一次即可增加 P1 表，`schema_metadata` 中会保留版本 1 和版本 2。

## 新增数据表

- `organization`：采购人、供应商和配置中的上市公司实体；
- `organization_alias`：原始名称和配置别名；
- `document_organization_role`：实体在某个文档版本中的采购人/供应商角色；
- `document_match_feature`：每个文档版本的匹配特征，支持新增公告与历史批次回配；
- `document_chain_candidate`：每个后续公告最多三个候选前序公告及评分证据；
- `document_chain_link`：当前最优的招标—中标或中标—合同链路；
- `match_review_queue`：无候选、低分或歧义链路的人工复核队列；
- `match_gold_label`：人工标注的匹配/不匹配样本。

## 企业实体归一

P1 只自动处理空格、全半角括号和常见标点差异，不会删除“有限公司”“集团”等法律实体后缀。原因是激进清洗可能把母公司、子公司或同名企业错误合并。

`config.json` 中的 `companies` 可以提供人工确认的别名：

```json
{
  "name": "示例科技股份有限公司",
  "aliases": ["示例科技", "示例科技股份"],
  "listed_code": "600000"
}
```

配置别名归到同一个 `LISTED_COMPANY` 实体；未配置名称按照规范化后的完整法定名称建实体。

## 匹配优先级

匹配严格遵循时间方向，只允许：

```text
招标公告 → 中标结果 → 采购合同
```

核心规则：

1. 两侧项目编号均存在且不一致：直接排除；
2. 两侧标包号均存在且不一致：直接排除；
3. 中标必须晚于招标且不超过365天；合同必须晚于前序公告且不超过730天；
4. 项目编号完全相同是主证据；
5. 标包号、采购人、标题、供应商和时间接近度用于加分；
6. 每个目标公告最多保留三个候选，防止只保存最终答案而无法审计。

自动接受要求：

- 分数不低于0.85；
- 项目编号完全相同；
- 第一、第二候选分差不小于0.05。

其余情况进入 `match_review_queue`。标题和采购人即使高度相似，也不会在缺少项目编号时直接自动接受。

## 输出文件

每次运行额外输出：

- `organizations.csv`：归一后的采购人、供应商和上市公司；
- `chain_links.csv`：招标—中标—合同链路、分数、方法及证据；
- `match_review_queue.csv`：需要人工确认的链路；
- `gold_labels_template.csv`：使用 `--write-gold-template` 时生成的标注模板。

## 人工标注与评估

生成模板：

```powershell
python procurement_crawler.py --config config.json --output-dir data --rebuild-from-csv --write-gold-template
```

标注字段：

- `link_type`：`TENDER_TO_AWARD` 或 `AWARD_TO_CONTRACT`；
- `source_url`：前序公告URL；
- `target_url`：后序公告URL；
- `is_match`：同一标包填1，否则填0；
- `package_same`：标包一致填1，不一致填0，无法确认留空；
- `annotator` / `note`：标注人和判断依据。

导入标注并评估自动接受链路：

```powershell
python procurement_crawler.py --config config.json --output-dir data --rebuild-from-csv --gold-labels data/gold_labels.csv
```

精确率、召回率和 F1 写入 `run_summary.json` 的 `database_changes.matching_evaluation`。在达到300—500条人工标注前，评分阈值只能视为保守的工程初值，不能宣称已完成统计校准。

## 尚未包含

P1 不处理采购意向模糊匹配、验收公告、附件表格、多供应商金额拆分、品牌归因和上市公司时点股权关系。这些应在三阶段链路经过人工评估后继续扩展。
