# Public Resources Transaction Relationship Diagram

政府采购、公共资源交易及央国企采购关系研究项目的初版代码。

当前版本首先实现中国政府采购网公开公告的低频采集，已完成 P0 数据正确性改造，并在其上增加 P1 企业实体归一、招标—中标—合同分层匹配、候选证据和人工复核队列。代码仅使用 Python 标准库。

## 当前能力

- 采集公开招标、中标、成交、合同、更正和终止公告；
- 提取项目编号、采购人、供应商、预算、中标金额和履约期等字段；
- 保存公告原文、来源 URL、内容哈希和抓取时间；
- 按项目编号优先、标题与采购人辅助的规则关联公告；
- 区分取消/终止、废标/流标和延期事件；
- 输出公告、项目链路、供应商汇总、指标和追加式 SQLite 事件数据库。
- 相同内容重复入库保持幂等，网页内容变化时保留新旧版本。
- 所有关联记录匹配方法、分数、置信度和复核状态。
- 归一采购人、供应商和配置别名，不使用激进的企业名称模糊合并；
- 项目编号或标包号冲突时阻断链路，歧义匹配进入人工复核；
- 支持导入人工金标并计算自动匹配的精确率、召回率和 F1。

## 快速开始

复制示例配置：

```powershell
Copy-Item config.example.json config.json
```

运行采集：

```powershell
python procurement_crawler.py --config config.json --output-dir data
```

测试时限制详情数量：

```powershell
python procurement_crawler.py --config config.json --output-dir data --max-details 10
```

使用已有 `notices.csv` 的原文重新计算结构化字段，不访问网站：

```powershell
python procurement_crawler.py --config config.json --output-dir data --rebuild-from-csv
```

生成 P1 人工标注模板：

```powershell
python procurement_crawler.py --config config.json --output-dir data --rebuild-from-csv --write-gold-template
```

## 输出文件

- `notices.csv`：一行一条公告，是事件证据，不等同于订单；
- `projects.csv`：公告关联后的项目级初步视图；
- `supplier_summary.csv`：供应商金额、客户集中度和地区覆盖；
- `metrics.csv`：项目及供应商指标长表；
- `procurement.sqlite`：P0 追加式事件数据库；
- `organizations.csv`：P1 企业实体和上市公司别名归一结果；
- `chain_links.csv`：招标—中标—合同的最优链路及匹配证据；
- `match_review_queue.csv`：无候选、低分或歧义链路的复核队列；
- `run_summary.json`：运行参数与质量摘要。

## 配置说明

- `scope`：`zygg` 为中央公告，`dfgg` 为地方公告；
- `categories`：需要采集的公告类别；
- `pages_per_category`：每类列表页数量；
- `start_date` / `end_date`：公告日期过滤；
- `keywords`：公司名称、采购人或主题关键词；
- `companies`：上市公司别名及上年营业收入，用于金额/收入指标；
- `metric_period_start`：新增采购人等指标的分析期起点。

## 重要口径

1. 一条公告不等于一笔新增订单。同一经济需求可能产生招标、中标、更正、终止和合同等多份公告。
2. 采购意向和招标属于需求侧，不能提前归因给最终中标供应商。
3. 预算、中标、合同和验收金额不能跨阶段相加。
4. 空字段表示源网页没有披露或当前未识别，不等同于数值零。
5. 新增采购人和首次合作指标需要分析期之前的历史数据；历史不足时保持为空。

## P0 数据库

P0 新增以下核心表：

- `source_document` / `source_document_version`
- `root_project` / `procurement_attempt` / `package`
- `contract` / `acceptance`
- `procurement_event`
- `amount_observation`
- `document_object_link`

当前状态通过 `current_event` 和 `current_object_state` 视图计算，不覆盖历史事件。详细结构和查询示例见 [P0 数据模型](docs/p0-schema.md)。

## P1 匹配层

P1 在同一个 SQLite 中追加以下表，不修改 P0 历史：

- `organization` / `organization_alias`
- `document_organization_role`
- `document_match_feature`
- `document_chain_candidate` / `document_chain_link`
- `match_review_queue` / `match_gold_label`

详细评分规则、复核流程和金标格式见 [P1 实体与链路匹配](docs/p1-matching.md)。

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 当前限制

当前版本仍是验证型实现：

- 尚未抓取附件、采购意向、验收、候选供应商和统一社会信用代码；
- P1 评分阈值仍是保守的工程初值，必须用300—500条人工标注链校准；
- 标包号提取已阻止明确冲突，但复杂多包表格仍需附件解析；
- 多供应商总金额在正式数据库中标记为未分配；旧版供应商CSV仍采用均分，仅可用于初筛；
- 尚未实现供应商、品牌、上市公司和时点有效股权关系图。

下一步应使用 `match_review_queue.csv` 建立300—500条人工标注链，按采购方式和公告模板分别评估阈值，然后再加入更正、终止和二次采购的完整状态机匹配。

## 合规

仅采集公开页面。使用时请遵守目标网站的 robots.txt、使用条款和适用法律，保持低频请求，不绕过验证码、登录或访问控制。
